"""Motor 'api': resuelve una TareaIA con la API de Claude (SDK oficial `anthropic`).

- Imágenes: se mandan en base64 (los cuadros, hojas y miniaturas ya están en el volumen).
- Salida: structured outputs con el modelo Pydantic del tipo de pedido (messages.parse): el JSON viene garantizado.
- Tendencias: herramienta de búsqueda web del lado de Anthropic (web_search). Si la API no acepta búsqueda web junto
  con salida estructurada, se hace en dos pasos (buscar y después ordenar el resultado en el esquema).
- Rechazos: se usa el fallback del servidor (`fallbacks: "default"`); si igual se niega, la tarea queda en error con el
  motivo y no se reintenta.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re

from .. import almacen
from ..config import config
from ..modelos import TareaIA
from .esquemas import para_api
from .prompts import sistema_de

log = logging.getLogger("hookline.ia.api")
BETA_FALLBACK = "server-side-fallback-2026-07-01"
LADO_MAX = 1568          # más grande no suma (la API lo achica igual) y cuesta más subirlo
_cliente = None
_cliente_clave = ""


class ErrorIA(RuntimeError):
    """Error definitivo: no tiene sentido reintentar."""


def cliente():
    """Cliente de la API. Se rehace solo si cambian la clave en Ajustes (no hace falta reiniciar nada)."""
    global _cliente, _cliente_clave
    from ..comun import clave_anthropic
    clave = clave_anthropic()
    if not clave:
        raise ErrorIA("Falta la API key de Claude: cargala en Ajustes → Cómo trabaja Claude.")
    if _cliente is None or clave != _cliente_clave:
        import anthropic
        _cliente, _cliente_clave = anthropic.Anthropic(api_key=clave, max_retries=2, timeout=600), clave
    return _cliente


def probar_conexion() -> dict:
    """Botón 'Probar conexión' de Ajustes: un pedido mínimo al modelo rápido (fracción de centavo)."""
    import time
    import anthropic
    cfg = config()
    t0 = time.time()
    try:
        r = cliente().messages.create(model=cfg.modelo_rapido, max_tokens=64, output_config={"effort": "low"},
                                      messages=[{"role": "user", "content": "Respondé solo con la palabra: listo"}])
    except ErrorIA:
        raise ValueError("Falta la API key de Claude: pegala primero.")
    except anthropic.AuthenticationError:
        raise ValueError("Anthropic rechazó la clave guardada: fijate si la borraste en la consola.")
    except anthropic.PermissionDeniedError:
        raise ValueError("La clave no tiene permiso para usar la API.")
    except anthropic.RateLimitError:
        raise ValueError("Anthropic está limitando los pedidos (o no hay saldo): revisá Gasto y uso en la consola.")
    except anthropic.APIConnectionError:
        raise ValueError("No hay conexión con Anthropic desde el servidor.")
    except anthropic.APIStatusError as ex:
        raise ValueError(f"Anthropic respondió {ex.status_code}: {str(ex.message)[:150]}")
    texto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
    return {"ok": True, "modelo": r.model, "ms": int((time.time() - t0) * 1000), "respuesta": texto[:60]}


def probar_clave(clave: str) -> None:
    """Antes de guardar una clave: ¿Anthropic la acepta? ValueError si la rechaza. Si no hay red, no frena."""
    import anthropic
    try:
        anthropic.Anthropic(api_key=clave, max_retries=0, timeout=15).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise ValueError("Anthropic rechazó esa clave: fijate que esté bien copiada y que no la hayas borrado.")
    except anthropic.PermissionDeniedError:
        raise ValueError("Esa clave no tiene permiso para usar la API (revisá el espacio de trabajo en la consola).")
    except anthropic.APIError as ex:
        log.info("no se pudo probar la clave (%s): se guarda igual", str(ex)[:120])


def _imagen(rel: str) -> dict:
    from PIL import Image
    im = Image.open(almacen.ruta(rel)).convert("RGB")
    im.thumbnail((LADO_MAX, LADO_MAX))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def _contenido(t: TareaIA) -> list[dict]:
    partes = [_imagen(r) for r in (t.entrada.get("imagenes") or [])]
    return partes + [{"type": "text", "text": t.entrada["instrucciones"]}]


def _uso(r) -> dict:
    u = getattr(r, "usage", None)
    if not u:
        return {}
    stu = getattr(u, "server_tool_use", None)
    return {"modelo": getattr(r, "model", ""), "entrada": getattr(u, "input_tokens", 0) or 0,
            "salida": getattr(u, "output_tokens", 0) or 0, "cache": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_escrita": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "busquedas": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0}


PREFIJO_RECHAZO = "Claude no quiso hacer este pedido"


def es_rechazo(motivo) -> bool:
    """¿La tarea falló porque Claude se negó (y no por red, clave o formato)? Lo usan los que deciden qué hacer con
    lo rechazado: descartarlo, nunca saltear el control."""
    return str(motivo or "").startswith(PREFIJO_RECHAZO)


def _revisar_rechazo(r) -> None:
    if getattr(r, "stop_reason", "") == "refusal":
        det = getattr(r, "stop_details", None)
        cat = getattr(det, "category", None) if det else None
        raise ErrorIA(f"{PREFIJO_RECHAZO} (motivo: {cat or 'seguridad'}). Revisalo a mano.")
    if getattr(r, "stop_reason", "") == "max_tokens":
        raise ErrorIA("La respuesta de Claude quedó cortada (max_tokens).")


def _comunes(t: TareaIA, cfg) -> dict:
    return {"model": cfg.modelo_rapido if t.entrada.get("rapido") else cfg.modelo,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": sistema_de(t.tipo), "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": "low" if t.entrada.get("rapido") else cfg.esfuerzo}}


def resolver(t: TareaIA) -> tuple[dict, dict]:
    import anthropic
    esquema, desde_api = para_api(t.tipo)
    try:
        resultado, uso = _resolver(t, esquema)
    except anthropic.BadRequestError as ex:      # 400: el pedido está mal armado; reintentar no lo arregla
        raise ErrorIA(f"La API de Claude rechazó el pedido: {_motivo(ex)}") from None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as ex:
        raise ErrorIA(f"La API de Claude rechazó la clave: {_motivo(ex)}. Revisala en Ajustes.") from None
    return (desde_api(resultado) if desde_api else resultado), uso


def _motivo(ex) -> str:
    try:
        return str(((ex.body or {}).get("error") or {}).get("message") or ex.message)[:300]
    except AttributeError:
        return str(ex)[:300]


def _resolver(t: TareaIA, esquema) -> tuple[dict, dict]:
    cfg = config()
    c = cliente()
    kw = _comunes(t, cfg)
    mensajes = [{"role": "user", "content": _contenido(t)}]
    fallback = {} if t.entrada.get("rapido") else {"betas": [BETA_FALLBACK], "fallbacks": "default"}
    if t.entrada.get("web"):
        return _con_web(c, kw, mensajes, esquema, fallback)
    r = c.beta.messages.parse(messages=mensajes, output_format=esquema, **kw, **fallback) if fallback else \
        c.messages.parse(messages=mensajes, output_format=esquema, **kw)
    _revisar_rechazo(r)
    if r.parsed_output is None:
        raise ErrorIA("Claude no devolvió el JSON esperado.")
    return r.parsed_output.model_dump(), _uso(r)


def _con_web(c, kw, mensajes, esquema, fallback) -> tuple[dict, dict]:
    import anthropic
    herramientas = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
                    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4}]
    try:
        r, u = _bucle_pausa(lambda m: c.beta.messages.parse(messages=m, output_format=esquema, tools=herramientas,
                                                             **kw, **fallback), mensajes)
        _revisar_rechazo(r)
        if r.parsed_output is not None:
            return r.parsed_output.model_dump(), u
    except anthropic.BadRequestError as ex:
        log.info("web + structured outputs no aceptado (%s): voy en dos pasos", str(ex)[:120])
    # 1) buscar libre  2) ordenar en el esquema
    kw_libre = {k: v for k, v in kw.items() if k != "output_config"}
    r1, u1 = _bucle_pausa(lambda m: c.beta.messages.create(messages=m, tools=herramientas, **kw_libre, **fallback),
                          mensajes)
    _revisar_rechazo(r1)
    texto = "\n".join(b.text for b in r1.content if getattr(b, "type", "") == "text")
    r2 = c.beta.messages.parse(messages=[{"role": "user", "content": "Pasá esto al formato pedido, sin inventar nada:\n\n"
                                                                     + texto[:30000]}],
                               output_format=esquema, **kw, **fallback)
    _revisar_rechazo(r2)
    if r2.parsed_output is None:
        raise ErrorIA("Claude no devolvió el JSON esperado.")
    return r2.parsed_output.model_dump(), sumar_uso(u1, _uso(r2))


def sumar_uso(*usos: dict) -> dict:
    """Suma los tokens (y búsquedas) de varias llamadas; el modelo es el de la primera que lo diga."""
    total: dict = {}
    for u in usos:
        for k, v in (u or {}).items():
            if k == "modelo":
                total.setdefault("modelo", v)
            else:
                total[k] = (total.get(k, 0) or 0) + (v or 0)
    return total


def _bucle_pausa(llamar, mensajes, max_vueltas: int = 5):
    """Las herramientas del servidor (búsqueda web) pueden pausar el turno: se continúa hasta que termine.
    Devuelve la última respuesta y el uso de TODAS las vueltas (cada vuelta se paga)."""
    m = list(mensajes)
    r = llamar(m)
    usos = [_uso(r)]
    for _ in range(max_vueltas):
        if getattr(r, "stop_reason", "") != "pause_turn":
            break
        m = m + [{"role": "assistant", "content": r.content}]
        r = llamar(m)
        usos.append(_uso(r))
    return r, sumar_uso(*usos)


def extraer_json(texto: str) -> dict:
    """Para respuestas en texto libre (no se usa con structured outputs, queda para el motor Claude Code)."""
    m = re.search(r"\{.*\}", texto or "", re.S)
    return json.loads(m.group(0)) if m else {}
