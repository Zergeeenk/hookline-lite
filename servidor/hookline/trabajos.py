"""Cola de trabajos en la base (sin Redis). La usa el worker.

encolar("publicar", {"pieza": "p123"}, clave="publicar:p123:2026-11-20T20:00", cuando=...)
  - `clave` es única: si ya hay un trabajo con esa clave, no se crea otro (así nunca se publica dos veces lo mismo).
tomar(worker, tipos) reclama el próximo trabajo listo de forma atómica (UPDATE ... WHERE estado='pendiente'),
  que funciona igual con varios workers en Postgres y con uno en SQLite.
Si un worker se muere con un trabajo tomado, al vencer `tomado_hasta` vuelve a quedar pendiente.
"""
from __future__ import annotations

import contextvars
import logging
import traceback
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .db import ahora, sesion
from .modelos import Trabajo

log = logging.getLogger("hookline.trabajos")

MANEJADORES: dict[str, Callable[[dict], None]] = {}
# cuánto tiempo un worker "tiene" un trabajo sin dar señales. Los trabajos largos renuevan la toma con latido().
DURACION_TOMA = {"publicar": timedelta(minutes=20), "render": timedelta(minutes=45), "ia": timedelta(minutes=45)}
_actual: contextvars.ContextVar = contextvars.ContextVar("trabajo_actual", default=None)


def latido() -> None:
    """Desde un manejador: 'sigo vivo'. Extiende la toma del trabajo en curso (así no se reabre mientras corre)."""
    t = _actual.get()
    if not t:
        return
    tid, tipo = t
    with sesion() as s:
        s.execute(update(Trabajo).where(Trabajo.id == tid, Trabajo.estado == "corriendo")
                  .values(tomado_hasta=ahora() + DURACION_TOMA.get(tipo, timedelta(minutes=10))))


class Reintentar(Exception):
    """Lanzala desde un manejador para reintentar más tarde sin contar como error."""
    def __init__(self, segundos: float = 30, motivo: str = ""):
        super().__init__(motivo)
        self.segundos = segundos


def manejador(tipo: str):
    def deco(f):
        MANEJADORES[tipo] = f
        return f
    return deco


def encolar(tipo: str, datos: dict | None = None, clave: str | None = None, cuando: datetime | None = None,
            max_intentos: int = 3) -> int | None:
    """Devuelve el id, o None si ya existía uno con esa clave."""
    try:
        with sesion() as s:
            if clave and s.scalar(select(Trabajo.id).where(Trabajo.clave == clave)):
                return None
            t = Trabajo(tipo=tipo, datos=datos or {}, clave=clave, ejecutar_en=cuando or ahora(),
                        max_intentos=max_intentos)
            s.add(t)
            s.flush()
            return t.id
    except IntegrityError:
        return None


def tomar(worker: str, tipos: list[str]) -> Trabajo | None:
    with sesion() as s:
        t0 = ahora()
        # lo que quedó tomado por un worker que se murió: vuelve a la cola si le quedan intentos; si no, error
        s.execute(update(Trabajo).where(Trabajo.estado == "corriendo", Trabajo.tomado_hasta < t0,
                                        Trabajo.intentos < Trabajo.max_intentos)
                  .values(estado="pendiente", tomado_por=""))
        s.execute(update(Trabajo).where(Trabajo.estado == "corriendo", Trabajo.tomado_hasta < t0,
                                        Trabajo.intentos >= Trabajo.max_intentos)
                  .values(estado="error", tomado_por="", terminado=t0, error="El worker se cortó con este trabajo."))
        candidatos = s.scalars(select(Trabajo.id).where(Trabajo.estado == "pendiente", Trabajo.ejecutar_en <= t0,
                                                        Trabajo.tipo.in_(tipos))
                               .order_by(Trabajo.ejecutar_en, Trabajo.id).limit(5)).all()
        for tid in candidatos:
            tipo = s.scalar(select(Trabajo.tipo).where(Trabajo.id == tid))
            hasta = t0 + DURACION_TOMA.get(tipo, timedelta(minutes=10))
            r = s.execute(update(Trabajo).where(Trabajo.id == tid, Trabajo.estado == "pendiente")
                          .values(estado="corriendo", tomado_por=worker, tomado_hasta=hasta,
                                  intentos=Trabajo.intentos + 1))
            if r.rowcount == 1:
                s.commit()
                return s.get(Trabajo, tid)
    return None


def correr(t: Trabajo) -> None:
    f = MANEJADORES.get(t.tipo)
    marca = _actual.set((t.id, t.tipo))
    try:
        if not f:
            raise RuntimeError(f"No hay manejador para {t.tipo}")
        f(t.datos or {})
        _terminar(t.id, "hecho")
    except Reintentar as r:
        with sesion() as s:
            x = s.get(Trabajo, t.id)
            x.estado, x.tomado_por, x.intentos = "pendiente", "", max(0, x.intentos - 1)
            x.ejecutar_en = ahora() + timedelta(seconds=r.segundos)
            x.error = str(r)[:500]
    except Exception as ex:
        log.warning("trabajo %s (%s) falló: %s", t.id, t.tipo, ex)
        with sesion() as s:
            x = s.get(Trabajo, t.id)
            x.error = (f"{type(ex).__name__}: {ex}\n" + traceback.format_exc(limit=3))[:2000]
            if x.intentos >= x.max_intentos:
                x.estado, x.terminado = "error", ahora()
            else:
                x.estado, x.tomado_por = "pendiente", ""
                x.ejecutar_en = ahora() + timedelta(seconds=30 * 2 ** x.intentos)
    finally:
        _actual.reset(marca)


def liberar_de_procesos_muertos(host: str) -> int:
    """Al arrancar un worker: los trabajos 'corriendo' de otro proceso de ESTA máquina que ya no existe se liberan
    enseguida (sin esperar que venza la toma). Publicar no se reabre nunca: queda en error para revisar."""
    import os
    n = 0
    with sesion() as s:
        for x in s.scalars(select(Trabajo).where(Trabajo.estado == "corriendo", Trabajo.tomado_por.like(f"{host}:%"))):
            try:
                pid = int(x.tomado_por.rsplit(":", 1)[1])
                os.kill(pid, 0)
                continue          # el proceso sigue vivo
            except (ValueError, IndexError):
                continue
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            if x.tipo == "publicar" or x.intentos >= x.max_intentos:
                x.estado, x.terminado, x.error = "error", ahora(), "El worker se cortó con este trabajo."
            else:
                x.estado = "pendiente"
            x.tomado_por = ""
            n += 1
    return n


def _terminar(tid: int, estado: str) -> None:
    with sesion() as s:
        x = s.get(Trabajo, tid)
        x.estado, x.terminado, x.tomado_por = estado, ahora(), ""


def limpiar_viejos(dias: int = 14) -> int:
    from sqlalchemy import delete
    with sesion() as s:
        r = s.execute(delete(Trabajo).where(Trabajo.estado == "hecho", Trabajo.terminado < ahora() - timedelta(days=dias)))
        return r.rowcount or 0


def pendientes(tipo: str | None = None) -> int:
    from sqlalchemy import func
    with sesion() as s:
        q = select(func.count()).select_from(Trabajo).where(or_(Trabajo.estado == "pendiente", Trabajo.estado == "corriendo"))
        if tipo:
            q = q.where(Trabajo.tipo == tipo)
        return s.scalar(q) or 0
