"""El worker: ejecuta la cola de trabajos y las tareas periódicas. Se corre con `hookline worker`.

Puede haber más de un worker (con Postgres): cada trabajo lo toma uno solo. Las tareas periódicas usan trabajos con
clave por ventana de tiempo, así que aunque haya dos workers no se duplican.

Grupos (cuántos trabajos de cada tipo corren a la vez en ESTE worker):
  publicar  → HOOKLINE_MAX_PUBLICANDO (2)     render → HOOKLINE_MAX_RENDER (2: ffmpeg es pesado)
  descargas → HOOKLINE_MAX_DESCARGAS (3)      general → el resto
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from . import comun, eventos, trabajos
from .config import config
from .db import ahora, sesion
from .trabajos import encolar, manejador

log = logging.getLogger("hookline.worker")

GRUPOS = {"publicar": ("publicar",), "render": ("render", "preparar_copia"),
          "descargas": ("descargar_ejemplo", "bajar_material", "fichas", "pyro_material"), "buscador": ("buscar",)}
PERIODO_RAPIDO = 4       # recoger resultados de Claude Code, avanzar encargos, escanear la bandeja
PERIODO_COLA = 30


def _importar_manejadores() -> None:
    """Registra todos los @manejador (los módulos de servicios se importan acá)."""
    from .ia import tareas  # noqa: F401
    from .servicios import comentarios, cuentas, jueces, noticias, piezas  # noqa: F401
    for mod in ("material", "fabrica", "buscador", "repartir", "pyro"):
        try:
            __import__(f"hookline.servicios.{mod}")
        except ImportError as ex:
            log.warning("módulo %s no disponible: %s", mod, ex)


@manejador("sincronizar")
def _sincronizar(d: dict) -> None:
    from .servicios import cuentas
    cuentas.sincronizar(d["cuenta"])


@manejador("refrescar_token")
def _refrescar(d: dict) -> None:
    from .servicios import cuentas
    cuentas.refrescar_token(d["cuenta"])


@manejador("tendencias_semanales")
def _tendencias(d: dict) -> None:
    from .ia.tareas import estado_puente
    from .servicios import jueces
    if estado_puente()["puente"] != "apagado":
        jueces.tendencias_semanales()


@manejador("mantenimiento")
def _mantenimiento(d: dict) -> None:
    from .ia import tareas
    trabajos.limpiar_viejos()
    tareas.limpiar_viejas()
    comun.recortar_tablas()


def periodicas() -> None:
    """Una vuelta del 'reloj': encola lo que corresponde según la hora (claves por ventana: no se duplica)."""
    from .modelos import CredencialIG
    from sqlalchemy import select
    t = ahora()
    hora, dia = t.strftime("%Y%m%d%H"), t.strftime("%Y%m%d")
    with sesion() as s:
        conectadas = [c for (c,) in s.execute(select(CredencialIG.cuenta_id).where(CredencialIG.vencido.is_(False)))]
    for cid in conectadas:
        encolar("sincronizar", {"cuenta": cid}, clave=f"sincronizar:{cid}:{hora}")
        encolar("refrescar_token", {"cuenta": cid}, clave=f"refrescar:{cid}:{dia}")
    encolar("tendencias_semanales", {}, clave=f"tendencias:{hora}")
    encolar("mantenimiento", {}, clave=f"mantenimiento:{dia}")
    try:
        from .servicios import noticias
        noticias.periodicas()
    except Exception as ex:
        log.warning("noticias: %s", ex)
    try:
        from .servicios import pyro
        pyro.periodicas(hora, dia)
    except Exception as ex:
        log.warning("pyro: %s", ex)


def _rapido() -> None:
    from .ia import tareas
    from .servicios import piezas
    pasos = [piezas.escanear, tareas.recoger_claude_code, eventos.enviar_pendientes]
    for mod, fn in (("fabrica", "recoger"), ("buscador", "recoger"), ("repartir", "seguir_encargo")):
        try:
            m = __import__(f"hookline.servicios.{mod}", fromlist=[fn])
            pasos.append(getattr(m, fn))
        except (ImportError, AttributeError):
            pass
    for f in pasos:
        try:
            f()
        except Exception as ex:
            log.warning("bucle rápido %s: %s", getattr(f, "__qualname__", f), ex)


class Worker:
    def __init__(self, nombre: str | None = None):
        self.nombre = nombre or f"{socket.gethostname()}:{os.getpid()}"
        self.parar = threading.Event()
        cfg = config()
        self.cupos = {"publicar": cfg.max_publicando, "render": cfg.max_render, "descargas": cfg.max_descargas,
                      "buscador": 1, "general": max(1, cfg.worker_hilos)}
        self.ocupados = {k: 0 for k in self.cupos}
        self._lock = threading.Lock()

    def _grupo(self, tipo: str) -> str:
        return next((g for g, tipos in GRUPOS.items() if tipo in tipos), "general")

    def _tipos_libres(self) -> list[str]:
        libres = []
        with self._lock:
            for tipo in trabajos.MANEJADORES:
                g = self._grupo(tipo)
                if self.ocupados[g] < self.cupos[g]:
                    libres.append(tipo)
        return libres

    def _correr(self, t) -> None:
        g = self._grupo(t.tipo)
        try:
            trabajos.correr(t)
        finally:
            with self._lock:
                self.ocupados[g] -= 1

    def vuelta(self) -> bool:
        """Toma y lanza un trabajo si hay. Devuelve True si lanzó algo."""
        tipos = self._tipos_libres()
        if not tipos:
            return False
        t = trabajos.tomar(self.nombre, tipos)
        if not t:
            return False
        with self._lock:
            self.ocupados[self._grupo(t.tipo)] += 1
        threading.Thread(target=self._correr, args=(t,), daemon=True, name=f"trabajo-{t.id}").start()
        return True

    def correr(self) -> None:
        _importar_manejadores()
        from .servicios import piezas
        liberados = trabajos.liberar_de_procesos_muertos(socket.gethostname())
        if liberados:
            log.warning("se liberaron %d trabajos de un worker anterior que se cortó", liberados)
        piezas.al_arrancar()
        for mod in ("fabrica", "buscador"):
            try:
                getattr(__import__(f"hookline.servicios.{mod}", fromlist=["al_arrancar"]), "al_arrancar")()
            except (ImportError, AttributeError):
                pass
            except Exception as ex:
                log.warning("al_arrancar %s: %s", mod, ex)
        for p in config().problemas():
            log.warning("CONFIG: %s", p)
        from .ia.tareas import motor_actual
        log.info("worker %s andando (motor IA: %s)", self.nombre, motor_actual())
        ult_rapido = ult_cola = ult_reloj = 0.0
        while not self.parar.is_set():
            ahora_s = time.time()
            if ahora_s - ult_cola >= PERIODO_COLA:
                ult_cola = ahora_s
                try:
                    piezas.revisar_cola()
                except Exception as ex:
                    log.warning("cola: %s", ex)
            if ahora_s - ult_reloj >= 60:
                ult_reloj = ahora_s
                try:
                    periodicas()
                except Exception as ex:
                    log.warning("periódicas: %s", ex)
            if ahora_s - ult_rapido >= PERIODO_RAPIDO:
                ult_rapido = ahora_s
                _rapido()
            if not self.vuelta():
                self.parar.wait(1.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    w = Worker()
    signal.signal(signal.SIGTERM, lambda *_: w.parar.set())
    signal.signal(signal.SIGINT, lambda *_: w.parar.set())
    w.correr()

