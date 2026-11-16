"""Cosas chicas que usa todo: ajustes, chat, actividad, eventos para Pyro, horas locales."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy import delete, select

from . import seguridad
from .config import config
from .db import ahora, sesion
from .modelos import Actividad, Ajuste, Evento, MensajeChat

log = logging.getLogger("hookline")


# --- horas: en la base todo es UTC; para la dueña, hora local -----------------------------------------------
def local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(config().tz) if dt else None


def iso_local(dt: datetime | None) -> str:
    """'AAAA-MM-DDTHH:MM' en hora local (el formato que usa el panel)."""
    return local(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def desde_local(texto: str) -> datetime:
    """'AAAA-MM-DDTHH:MM' (hora local, como lo manda el panel) o ISO con zona ('...Z', '...-03:00') → datetime UTC."""
    dt = datetime.fromisoformat(str(texto).strip().replace("Z", "+00:00"))   # respeta la zona si viene (Pyro en UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config().tz)
    return dt.astimezone(timezone.utc)


def hoy_local() -> date:
    return datetime.now(config().tz).date()


# --- ajustes (clave/valor, con cifrado opcional) --------------------------------------------------------------
def ajuste(clave: str, defecto=None):
    with sesion() as s:
        a = s.get(Ajuste, clave)
        if not a or a.valor is None:
            return defecto
        return seguridad.descifrar(a.valor) if a.cifrado else a.valor


def guardar_ajuste(clave: str, valor, cifrado: bool = False) -> None:
    with sesion() as s:
        a = s.get(Ajuste, clave) or Ajuste(clave=clave)
        a.valor = seguridad.cifrar(str(valor)) if (cifrado and valor) else valor
        a.cifrado = bool(cifrado and valor)
        s.add(a)


def config_id_meta() -> str:
    """El ID de configuración del inicio de sesión unificado (vacío = el inicio de sesión con Instagram de siempre)."""
    import os
    return os.environ.get("HOOKLINE_META_CONFIG_ID", "").strip() or str(ajuste("meta_config_id", "") or "")


def app_meta() -> tuple[str, str]:
    """(app_id, app_secret): manda la variable de entorno; si no, lo cargado en Ajustes (cifrado)."""
    cfg = config()
    return (cfg.meta_app_id or str(ajuste("meta_app_id", "") or ""),
            cfg.meta_app_secret or str(ajuste("meta_app_secret", "") or ""))


def clave_anthropic() -> str:
    """La API key de Claude: la variable de entorno ANTHROPIC_API_KEY manda; si no, la cargada en Ajustes (cifrada)."""
    import os
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or str(ajuste("anthropic_api_key", "") or "").strip()


def hay_clave_anthropic() -> bool:
    try:
        return bool(clave_anthropic())
    except Exception:
        return False


# --- chat del panel ---------------------------------------------------------------------------------------------
def chat_agregar(de: str, texto: str, extra: dict | None = None, autor: str = "") -> dict:
    with sesion() as s:
        m = MensajeChat(de=de, texto=str(texto)[:6000], extra=extra, autor=autor)
        s.add(m)
        s.flush()
        return mensaje_dict(m)


def mensaje_dict(m: MensajeChat) -> dict:
    return {"id": m.id, "de": m.de, "texto": m.texto, "hora": local(m.creado).strftime("%d/%m %H:%M"),
            **(m.extra or {})}


def aviso(texto: str, **extra) -> None:
    """Mensaje del sistema en el chat (lo que antes era de='panel')."""
    chat_agregar("panel", texto, extra or None)


# --- actividad de la fábrica (Inicio) ----------------------------------------------------------------------------
def actividad(tipo: str, texto: str, img: str = "", cuenta: str = "", copia: str = "") -> None:
    try:
        with sesion() as s:
            s.add(Actividad(tipo=tipo, texto=str(texto)[:200], img=img or "", cuenta=cuenta or "", copia=copia or ""))
    except Exception as ex:  # el registro nunca tiene que romper lo que se está haciendo
        log.warning("actividad: %s", ex)


def recortar_tablas() -> None:
    """Mantenimiento: la actividad y el chat no crecen sin límite."""
    with sesion() as s:
        for modelo, n in ((Actividad, 2000), (MensajeChat, 4000)):
            ids = s.scalars(select(modelo.id).order_by(modelo.id.desc()).offset(n).limit(1)).all()
            if ids:
                s.execute(delete(modelo).where(modelo.id <= ids[0]))


# --- eventos para Pyro (outbox; los manda el worker con reintentos) -----------------------------------------------
def evento(nombre: str, /, **datos) -> None:
    if not config().webhook_url:
        return
    with sesion() as s:
        if datos.get("cuenta") and "pyro_modelo_id" not in datos:      # Pyro filtra por su modelo
            from .modelos import Cuenta
            c = s.get(Cuenta, str(datos["cuenta"]))
            datos["pyro_modelo_id"] = c.pyro_modelo_id if c else None
        s.add(Evento(tipo=nombre, datos=datos))


# --- candados entre procesos (API y worker comparten el volumen de datos) -------------------------------------------------------
import threading as _threading

_hilos: dict = {}
_local = _threading.local()


@contextmanager
def candado(nombre: str, espera: float = 120):
    """Exclusión mutua entre procesos e hilos: un archivo bloqueado con flock en el volumen de datos.
    Sirve para lo que en Lite protegía un lock en memoria (había un solo proceso): repartir, escanear la bandeja,
    leer-modificar-escribir los datos de una copia. Reentrante dentro del mismo hilo."""
    import time as _t
    tomados = getattr(_local, "tomados", None)
    if tomados is None:
        tomados = _local.tomados = {}
    if tomados.get(nombre):            # ya lo tiene este hilo
        tomados[nombre] += 1
        try:
            yield
        finally:
            tomados[nombre] -= 1
        return
    lock = _hilos.setdefault(nombre, _threading.Lock())
    if not lock.acquire(timeout=espera):
        raise TimeoutError(f"El candado '{nombre}' está ocupado hace mucho.")
    try:
        from . import almacen
        with open(almacen.raiz() / f".candado-{nombre}", "a+") as f:
            try:
                import fcntl
            except ImportError:            # Windows: alcanza con el lock de hilos (un solo proceso)
                fcntl = None
            if fcntl:
                fin = _t.time() + espera
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if _t.time() > fin:
                            raise TimeoutError(f"El candado '{nombre}' está ocupado hace mucho.")
                        _t.sleep(0.05)
            tomados[nombre] = 1
            try:
                yield
            finally:
                tomados[nombre] = 0
    finally:
        lock.release()
