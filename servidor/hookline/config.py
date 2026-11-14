"""Configuración por variables de entorno (nada de secretos en archivos del repo).

Todas empiezan con HOOKLINE_ salvo las de terceros (ANTHROPIC_API_KEY, AWS_*). Ver .env.ejemplo.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def _bool(v: str | None, defecto: bool = False) -> bool:
    if v is None or v == "":
        return defecto
    return v.strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def _int(v: str | None, defecto: int) -> int:
    try:
        return int(v) if v not in (None, "") else defecto
    except ValueError:
        return defecto


@dataclass(frozen=True)
class Config:
    # --- general -----------------------------------------------------------------------------------
    entorno: str = "desarrollo"                       # desarrollo | produccion
    url_publica: str = "http://localhost:8000"        # cómo se llega al servicio desde afuera (https en producción)
    zona_horaria: str = "America/Argentina/Buenos_Aires"
    datos: Path = Path("./datos-hookline")            # volumen persistente: medios, trabajo de la fábrica, perfil del buscador
    base_datos: str = ""                              # postgresql+psycopg://... ; vacío = SQLite en datos/hookline.db
    clave_secreta: str = ""                           # Fernet (32 bytes base64) para cifrar tokens y el App Secret
    clave_sesion: str = ""                            # firma de cookies de sesión y URLs públicas (cualquier texto largo)
    cookie_segura: bool = False                       # True en producción (solo https)
    origenes_permitidos: tuple[str, ...] = ()         # CORS para Pyro (vacío = ninguno)

    # --- Meta / Instagram --------------------------------------------------------------------------
    meta_app_id: str = ""
    meta_app_secret: str = ""                         # si está acá, manda sobre lo guardado en la base
    ig_version: str = "v26.0"

    # --- almacenamiento para que Meta baje los archivos ---------------------------------------------
    almacen: str = "local"                            # local (sirve /publico/<firma> desde este servicio) | s3
    s3_bucket: str = ""
    s3_prefijo: str = "hookline/"
    s3_region: str = ""
    s3_endpoint: str = ""                             # para MinIO / R2 / etc.

    # --- IA ------------------------------------------------------------------------------------------
    motor_ia: str = "api"                             # api | claude_code
    modelo: str = "claude-opus-5-5"                   # jueces, lectura de memes, juez, filtro, tendencias, chat
    modelo_rapido: str = "claude-haiku-5-5"           # respuestas a comentarios
    esfuerzo: str = "medium"
    anthropic_api_key_presente: bool = False

    # --- worker -------------------------------------------------------------------------------------
    worker_hilos: int = 6
    max_render: int = 2                               # videos armándose a la vez (ffmpeg es pesado)
    max_descargas: int = 3
    max_publicando: int = 2

    # --- fábrica / buscador ------------------------------------------------------------------------
    dir_fuentes: tuple[Path, ...] = field(default_factory=tuple)
    chrome: str = ""                                  # ruta a Chrome/Chromium para el buscador (vacío = se busca)
    buscador_activo: bool = True

    # --- webhooks hacia Pyro -----------------------------------------------------------------------
    webhook_url: str = ""
    webhook_secreto: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zona_horaria)

    @property
    def url_bd(self) -> str:
        return self.base_datos or f"sqlite:///{(self.datos / 'hookline.db').resolve()}"

    @property
    def redirect_instagram(self) -> str:
        return self.url_publica.rstrip("/") + "/instagram/callback"

    def problemas(self) -> list[str]:
        """Lo que falta para producción (se muestra en /api/salud y al arrancar)."""
        p = []
        if not self.clave_secreta:
            p.append("Falta HOOKLINE_CLAVE_SECRETA: los tokens no se pueden cifrar.")
        if not self.clave_sesion:
            p.append("Falta HOOKLINE_CLAVE_SESION.")
        if self.entorno == "produccion":
            if not self.url_publica.startswith("https://"):
                p.append("HOOKLINE_URL_PUBLICA tiene que ser https en producción (Instagram lo exige).")
            if not self.cookie_segura:
                p.append("Poné HOOKLINE_COOKIE_SEGURA=1 en producción.")
            if not self.base_datos:
                p.append("En producción usá Postgres (HOOKLINE_BASE_DATOS).")
        if self.motor_ia == "api" and not self.anthropic_api_key_presente:
            p.append("Falta la API key de Claude (ANTHROPIC_API_KEY): cargala en Ajustes → Cómo trabaja Claude.")
        if self.almacen == "s3" and not self.s3_bucket:
            p.append("HOOKLINE_ALMACEN=s3 sin HOOKLINE_S3_BUCKET.")
        return p


def desde_entorno(env: dict | None = None) -> Config:
    e = os.environ if env is None else env
    g = lambda k, d="": e.get(k, d)
    fuentes = tuple(Path(x) for x in g("HOOKLINE_DIR_FUENTES").split(os.pathsep) if x)
    return Config(
        entorno=g("HOOKLINE_ENTORNO", "desarrollo"),
        url_publica=g("HOOKLINE_URL_PUBLICA", "http://localhost:8000").rstrip("/"),
        zona_horaria=g("HOOKLINE_ZONA_HORARIA", "America/Argentina/Buenos_Aires"),
        datos=Path(g("HOOKLINE_DATOS", "./datos-hookline")),
        base_datos=g("HOOKLINE_BASE_DATOS"),
        clave_secreta=g("HOOKLINE_CLAVE_SECRETA"),
        clave_sesion=g("HOOKLINE_CLAVE_SESION"),
        cookie_segura=_bool(g("HOOKLINE_COOKIE_SEGURA")),
        origenes_permitidos=tuple(x.strip() for x in g("HOOKLINE_ORIGENES_PERMITIDOS").split(",") if x.strip()),
        meta_app_id=g("HOOKLINE_META_APP_ID"),
        meta_app_secret=g("HOOKLINE_META_APP_SECRET"),
        ig_version=g("HOOKLINE_IG_VERSION", "v26.0"),
        almacen=g("HOOKLINE_ALMACEN", "local"),
        s3_bucket=g("HOOKLINE_S3_BUCKET"),
        s3_prefijo=g("HOOKLINE_S3_PREFIJO", "hookline/"),
        s3_region=g("HOOKLINE_S3_REGION"),
        s3_endpoint=g("HOOKLINE_S3_ENDPOINT"),
        motor_ia=g("HOOKLINE_MOTOR_IA", "api"),
        modelo=g("HOOKLINE_MODELO", "claude-opus-5-5"),
        modelo_rapido=g("HOOKLINE_MODELO_RAPIDO", "claude-haiku-5-5"),
        esfuerzo=g("HOOKLINE_ESFUERZO", "medium"),
        anthropic_api_key_presente=bool(g("ANTHROPIC_API_KEY")),
        worker_hilos=_int(g("HOOKLINE_WORKER_HILOS"), 6),
        max_render=_int(g("HOOKLINE_MAX_RENDER"), 2),
        max_descargas=_int(g("HOOKLINE_MAX_DESCARGAS"), 3),
        max_publicando=_int(g("HOOKLINE_MAX_PUBLICANDO"), 2),
        dir_fuentes=fuentes,
        chrome=g("HOOKLINE_CHROME"),
        buscador_activo=_bool(g("HOOKLINE_BUSCADOR"), True),
        webhook_url=g("HOOKLINE_WEBHOOK_URL"),
        webhook_secreto=g("HOOKLINE_WEBHOOK_SECRETO"),
    )


@lru_cache
def config() -> Config:
    return desde_entorno()


def recargar() -> Config:
    config.cache_clear()
    return config()
