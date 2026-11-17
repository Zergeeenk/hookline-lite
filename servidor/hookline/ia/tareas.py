"""Pedidos a Claude, con motor intercambiable.

  crear("jueces", datos, referencia="p123", imagenes=[rel, ...])
    → guarda una TareaIA con las instrucciones ya armadas (prompts.py) y el esquema de salida (esquemas.py).
  Motor "api":         el worker la manda a la API de Claude (trabajo 'ia') y la completa sola.
  Motor "claude_code": queda en trabajo/ia/<id>/pedido.json; `hookline puente esperar` se la muestra a Claude Code,
                       que escribe resultado.json; el worker la recoge (recoger_claude_code).
  Cuando termina: se valida contra el esquema y se llama a la función registrada con @al_terminar(tipo).
  Si falla: @al_fallar(tipo).

El motor se elige con HOOKLINE_MOTOR_IA o, en caliente, desde Ajustes (ajuste 'motor_ia').
"""
from __future__ import annotations

import json
import logging
import secrets
import shutil
from datetime import timedelta
from typing import Callable

from pydantic import ValidationError
from sqlalchemy import select, update

from .. import almacen
from ..comun import ajuste
from ..config import config
from ..db import ahora, sesion
from ..modelos import TareaIA
from ..trabajos import encolar, manejador
from . import prompts
from .esquemas import ESQUEMAS

log = logging.getLogger("hookline.ia")
MOTORES = ("api", "claude_code")
_TERMINAR: dict[str, Callable[[TareaIA, dict], None]] = {}
_FALLAR: dict[str, Callable[[TareaIA, str], None]] = {}
ESPERA_CLAUDE_CODE = timedelta(hours=3)


def al_terminar(tipo: str):
    def deco(f):
        _TERMINAR[tipo] = f
        return f
    return deco


def al_fallar(tipo: str):
    def deco(f):
        _FALLAR[tipo] = f
        return f
    return deco


def motor_actual() -> str:
    m = ajuste("motor_ia") or config().motor_ia
    return m if m in MOTORES else "api"


def dir_tarea(tid: str):
    return almacen.dir_de("ia", tid)


def crear(tipo: str, datos: dict, referencia: str = "", imagenes: list[str] | None = None) -> str:
    if tipo not in prompts.CONSTRUCTORES:
        raise ValueError(f"Tipo de pedido desconocido: {tipo}")
    tid = f"{tipo}-{secrets.token_hex(5)}"
    motor = motor_actual()
    entrada = {"instrucciones": prompts.CONSTRUCTORES[tipo](datos), "imagenes": list(imagenes or []),
               "web": tipo in prompts.WEB, "rapido": tipo in prompts.RAPIDO, "datos": datos}
    with sesion() as s:
        s.add(TareaIA(id=tid, tipo=tipo, motor=motor, referencia=referencia, entrada=entrada))
    if motor == "api":
        encolar("ia", {"tarea": tid}, clave=f"ia:{tid}", max_intentos=4)
    else:
        _materializar(tid, tipo, entrada)
    return tid


def _materializar(tid: str, tipo: str, entrada: dict) -> None:
    """Para Claude Code: un pedido.json que se entiende solo (rutas absolutas a las imágenes y el JSON Schema)."""
    d = dir_tarea(tid)
    pedido = {"id": tid, "tipo": tipo, "sistema": prompts.sistema_de(tipo), "instrucciones": entrada["instrucciones"],
              "imagenes": [str(almacen.ruta(r)) for r in entrada["imagenes"]],
              "buscar_en_internet": entrada["web"], "esquema": ESQUEMAS[tipo].model_json_schema(),
              "escribir_en": str(d / "resultado.json")}
    (d / "pedido.json").write_text(json.dumps(pedido, ensure_ascii=False, indent=1), encoding="utf-8")


def completar(tid: str, resultado: dict, uso: dict | None = None) -> None:
    with sesion() as s:
        t = s.get(TareaIA, tid)
        if not t or t.estado == "hecha":
            return
        tipo = t.tipo
    try:
        valido = ESQUEMAS[tipo].model_validate(resultado).model_dump()
    except ValidationError as ex:
        return fallar(tid, f"La respuesta no tiene la forma esperada: {str(ex)[:300]}")
    with sesion() as s:   # atómico: si dos procesos completan la misma tarea, uno solo aplica el resultado
        r = s.execute(update(TareaIA).where(TareaIA.id == tid, TareaIA.estado.in_(("pendiente", "enviada")))
                      .values(estado="hecha", resultado=valido, terminado=ahora(), error="", **({"uso": uso} if uso else {})))
        if r.rowcount != 1:
            return
    with sesion() as s:
        t = s.get(TareaIA, tid)
    f = _TERMINAR.get(tipo)
    if f:
        try:
            f(t, valido)
        except Exception as ex:
            log.exception("al_terminar %s: %s", tipo, ex)
            with sesion() as s:
                s.get(TareaIA, tid).error = f"Error al aplicar el resultado: {str(ex)[:300]}"


def fallar(tid: str, motivo: str) -> None:
    with sesion() as s:
        r = s.execute(update(TareaIA).where(TareaIA.id == tid, TareaIA.estado.in_(("pendiente", "enviada")))
                      .values(estado="error", error=str(motivo)[:1000], terminado=ahora()))
        if r.rowcount != 1:
            return
        t = s.get(TareaIA, tid)
    f = _FALLAR.get(t.tipo)
    if f:
        try:
            f(t, str(motivo))
        except Exception as ex:
            log.exception("al_fallar %s: %s", t.tipo, ex)


@manejador("ia")
def correr_api(datos: dict) -> None:
    from .motor_api import ErrorIA, resolver
    tid = datos["tarea"]
    with sesion() as s:
        t = s.get(TareaIA, tid)
        if not t or t.estado in ("hecha", "error"):
            return
        t.estado, t.enviada, t.intentos = "enviada", ahora(), t.intentos + 1
    try:
        resultado, uso = resolver(t)
    except ErrorIA as ex:          # definitivo (rechazo, sin API key, respuesta inválida): no se reintenta
        return fallar(tid, str(ex))
    except Exception as ex:        # red, 429, 5xx: el trabajo se reintenta con espera creciente
        if t.intentos >= 3:
            return fallar(tid, f"La API de Claude no respondió después de 3 intentos: {str(ex)[:200]}")
        with sesion() as s:
            s.get(TareaIA, tid).estado = "pendiente"
        raise
    completar(tid, resultado, uso)


# --- motor Claude Code (puente) ----------------------------------------------------------------------------------------------
def pendientes_claude_code() -> list[TareaIA]:
    with sesion() as s:
        return s.scalars(select(TareaIA).where(TareaIA.motor == "claude_code", TareaIA.estado == "pendiente")
                         .order_by(TareaIA.creado)).all()


def marcar_enviadas(ids: list[str]) -> None:
    with sesion() as s:
        for tid in ids:
            t = s.get(TareaIA, tid)
            if t and t.estado == "pendiente":
                t.estado, t.enviada = "enviada", ahora()


def recoger_claude_code() -> int:
    """Worker, cada pocos segundos: lo que Claude Code ya escribió en resultado.json."""
    n = 0
    with sesion() as s:
        abiertas = s.scalars(select(TareaIA).where(TareaIA.motor == "claude_code",
                                                   TareaIA.estado.in_(("pendiente", "enviada")))).all()
    for t in abiertas:
        f = dir_tarea(t.id) / "resultado.json"
        if f.exists():
            try:
                texto = f.read_bytes().decode("utf-8", errors="replace")
                f.unlink()
            except OSError:          # otro worker lo tomó primero
                continue
            try:
                datos = json.loads(texto)
            except ValueError as ex:
                fallar(t.id, f"resultado.json no es JSON válido: {str(ex)[:200]}")
                continue
            if not isinstance(datos, dict):
                fallar(t.id, "resultado.json tiene que ser un objeto JSON.")
                continue
            completar(t.id, datos)
            n += 1
        elif ahora() - (t.enviada or t.creado) > ESPERA_CLAUDE_CODE:
            fallar(t.id, "Claude Code no atendió el pedido a tiempo (¿está corriendo `hookline puente esperar`?).")
    return n


def limpiar_viejas(dias: int = 7) -> None:
    with sesion() as s:
        viejas = s.scalars(select(TareaIA.id).where(TareaIA.terminado < ahora() - timedelta(days=dias))).all()
    for tid in viejas:
        shutil.rmtree(almacen.raiz() / almacen.SECCIONES["ia"] / tid, ignore_errors=True)


def estado_puente() -> dict:
    """Lo que muestra el panel abajo a la izquierda ('Claude conectado')."""
    motor = motor_actual()
    if motor == "api":
        from ..comun import hay_clave_anthropic
        ok = hay_clave_anthropic()
        return {"motor": "api", "instalado": ok, "cli": ok, "extension": False,
                "puente": "escuchando" if ok else "apagado", "ultimo": ""}
    p = ajuste("puente", {}) or {}
    import time
    t = time.time()
    if t - float(p.get("escuchando") or 0) < 60:
        estado = "escuchando"
    elif t - float(p.get("entregado") or 0) < 30 * 60:
        estado = "trabajando"
    else:
        estado = "apagado"
    return {"motor": "claude_code", "instalado": True, "cli": True, "extension": False, "puente": estado,
            "ultimo": ""}
