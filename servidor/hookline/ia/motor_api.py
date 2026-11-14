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
from .esquemas import ESQUEMAS
from .prompts import SISTEMA

log = logging.getLogger("hookline.ia.api")
BETA_FALLBACK = "server-side-fallback-2026-07-01"
LADO_MAX = 1568          # más grande no suma (la API lo achica igual) y cuesta más subirlo
_cliente = None


class ErrorIA(RuntimeError):
    """Error definitivo: no tiene sentido reintentar."""


def cliente():
    global _cliente
    if _cliente is None:
        if not config().anthropic_api_key_presente:
            raise ErrorIA("Falta ANTHROPIC_API_KEY: cargala o cambiá el motor de IA a Claude Code en Ajustes.")
        import anthropic
        _cliente = anthropic.Anthropic(max_retries=2, timeout=600)
    return _cliente


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
    return {"modelo": getattr(r, "model", ""), "entrada": getattr(u, "input_tokens", 0),
            "salida": getattr(u, "output_tokens", 0), "cache": getattr(u, "cache_read_input_tokens", 0) or 0}


def _revisar_rechazo(r) -> None:
    if getattr(r, "stop_reason", "") == "refusal":
        det = getattr(r, "stop_details", None)
        cat = getattr(det, "category", None) if det else None
        raise ErrorIA(f"Claude no quiso hacer este pedido (motivo: {cat or 'seguridad'}). Revisalo a mano.")
    if getattr(r, "stop_reason", "") == "max_tokens":
        raise ErrorIA("La respuesta de Claude quedó cortada (max_tokens).")


def _comunes(t: TareaIA, cfg) -> dict:
    return {"model": cfg.modelo_rapido if t.entrada.get("rapido") else cfg.modelo,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": SISTEMA, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": "low" if t.entrada.get("rapido") else cfg.esfuerzo}}


def resolver(t: TareaIA) -> tuple[dict, dict]:
    cfg = config()
    esquema = ESQUEMAS[t.tipo]
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
        r = _bucle_pausa(lambda m: c.beta.messages.parse(messages=m, output_format=esquema, tools=herramientas,
                                                          **kw, **fallback), mensajes)
        _revisar_rechazo(r)
        if r.parsed_output is not None:
            return r.parsed_output.model_dump(), _uso(r)
    except anthropic.BadRequestError as ex:
        log.info("web + structured outputs no aceptado (%s): voy en dos pasos", str(ex)[:120])
    # 1) buscar libre  2) ordenar en el esquema
    kw_libre = {k: v for k, v in kw.items() if k != "output_config"}
    r1 = _bucle_pausa(lambda m: c.beta.messages.create(messages=m, tools=herramientas, **kw_libre, **fallback), mensajes)
    _revisar_rechazo(r1)
    texto = "\n".join(b.text for b in r1.content if getattr(b, "type", "") == "text")
    r2 = c.beta.messages.parse(messages=[{"role": "user", "content": "Pasá esto al formato pedido, sin inventar nada:\n\n"
                                                                     + texto[:30000]}],
                               output_format=esquema, **kw, **fallback)
    _revisar_rechazo(r2)
    if r2.parsed_output is None:
        raise ErrorIA("Claude no devolvió el JSON esperado.")
    u1, u2 = _uso(r1), _uso(r2)
    return r2.parsed_output.model_dump(), {k: (u1.get(k, 0) or 0) + (u2.get(k, 0) or 0) if k != "modelo" else u1.get(k)
                                            for k in u1 | u2}


def _bucle_pausa(llamar, mensajes, max_vueltas: int = 5):
    """Las herramientas del servidor (búsqueda web) pueden pausar el turno: se continúa hasta que termine."""
    m = list(mensajes)
    r = llamar(m)
    for _ in range(max_vueltas):
        if getattr(r, "stop_reason", "") != "pause_turn":
            return r
        m = m + [{"role": "assistant", "content": r.content}]
        r = llamar(m)
    return r


def extraer_json(texto: str) -> dict:
    """Para respuestas en texto libre (no se usa con structured outputs, queda para el motor Claude Code)."""
    m = re.search(r"\{.*\}", texto or "", re.S)
    return json.loads(m.group(0)) if m else {}
