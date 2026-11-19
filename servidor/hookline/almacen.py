"""Archivos: todo vive en el volumen HOOKLINE_DATOS (ffmpeg necesita archivos locales).

Rutas relativas (lo que se guarda en la base) → absolutas con `ruta()`, que impide salir del volumen.
Para que Instagram baje una foto o un video hace falta una URL pública https:
  - almacen=local: este mismo servicio la sirve en /publico/<ruta>?v=<vence>&f=<firma> (firmada, vence en minutos).
  - almacen=s3: se sube al bucket y se usa una URL prefirmada.
Así ya no hace falta el túnel de cloudflared de Hookline Lite.
"""
from __future__ import annotations

import shutil
import time
import urllib.parse
from pathlib import Path

from . import seguridad
from .config import config

SECCIONES = {
    "subir": "para-subir",        # bandeja: para-subir/<cuenta>/
    "material": "material",       # material/<modelo>/<seccion>/
    "copias": "copias",           # copias/<id>/
    "jueces": "trabajo/jueces",
    "salida": "trabajo/salida",
    "ia": "trabajo/ia",
    "minis": "cache/miniaturas",
    "resultados": "resultados",
    "candidatos": "trabajo/candidatos",
    "navegador": "navegador",
    "fuentes": "fuentes",         # fuentes/<clave>.ttf|otf (las subidas en la sección Fuentes)
    "stickers": "stickers",       # stickers/<nombre>.png (emojis de iPhone, logos...: se pegan sobre el video)
}


def raiz() -> Path:
    r = config().datos.resolve()
    r.mkdir(parents=True, exist_ok=True)
    return r


def dir_de(seccion: str, *partes: str) -> Path:
    d = raiz() / SECCIONES[seccion]
    for p in partes:
        d = d / _limpio(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _limpio(parte: str) -> str:
    parte = str(parte)
    if parte in ("", ".", "..") or "/" in parte or "\\" in parte or "\x00" in parte:
        raise ValueError("Nombre de carpeta inválido.")
    return parte


def ruta(rel: str) -> Path:
    """Relativa → absoluta, sin poder salir del volumen (protección contra ../ y links simbólicos)."""
    if "\x00" in str(rel):
        raise ValueError("Ruta inválida.")
    p = (raiz() / str(rel)).resolve()
    if not p.is_relative_to(raiz()):
        raise ValueError("Ruta fuera del almacenamiento.")
    return p


def relativa(p: Path | str) -> str:
    return Path(p).resolve().relative_to(raiz()).as_posix()


def libre(p: Path) -> Path:
    """Un nombre que no exista (foto.jpg → foto_2.jpg ...)."""
    p = Path(p)
    if not p.exists():
        return p
    for i in range(2, 9999):
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
    return p.with_name(f"{p.stem}_{int(time.time())}{p.suffix}")


def nombre_seguro(nombre: str, largo: int = 90) -> str:
    import re
    limpio = re.sub(r"[^\w.\- ]+", "_", Path(str(nombre)).name).strip(" .")[-largo:]
    return limpio or "archivo"


def mover(src: Path, carpeta: Path) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    dst = libre(carpeta / src.name)
    shutil.move(str(src), str(dst))
    return dst


# --- URLs públicas para Meta ----------------------------------------------------------------------------------
def url_publica(rel: str, minutos: int = 60) -> str:
    cfg = config()
    if cfg.almacen == "s3":
        return _s3_url(rel, minutos)
    vence = int(time.time()) + minutos * 60
    f = seguridad.firmar(rel, vence)
    return f"{cfg.url_publica}/publico/{urllib.parse.quote(rel)}?v={vence}&f={f}"


def verificar_publica(rel: str, vence: str, firma: str) -> Path | None:
    try:
        v = int(vence)
    except (TypeError, ValueError):
        return None
    if not seguridad.firma_valida(rel, v, str(firma or "")):
        return None
    try:
        p = ruta(rel)
    except ValueError:
        return None
    # solo lo que se le ofrece a Meta: la bandeja y las fotos convertidas (nunca la base, candados, etc.)
    if not any(p.is_relative_to(raiz() / SECCIONES[s]) for s in ("subir", "salida")) or p.name.startswith(".candado"):
        return None
    return p if p.is_file() else None


def _s3():
    import boto3
    cfg = config()
    kw = {}
    if cfg.s3_region:
        kw["region_name"] = cfg.s3_region
    if cfg.s3_endpoint:
        kw["endpoint_url"] = cfg.s3_endpoint
    return boto3.client("s3", **kw)


def _s3_url(rel: str, minutos: int) -> str:
    cfg = config()
    key = cfg.s3_prefijo + rel
    cliente = _s3()
    tipo = "video/mp4" if rel.lower().endswith((".mp4", ".mov", ".m4v")) else "image/jpeg"
    cliente.upload_file(str(ruta(rel)), cfg.s3_bucket, key, ExtraArgs={"ContentType": tipo})
    return cliente.generate_presigned_url("get_object", Params={"Bucket": cfg.s3_bucket, "Key": key},
                                          ExpiresIn=minutos * 60)


def borrar_publica(rel: str) -> None:
    """Después de publicar, se borra la copia del bucket (en local no hay nada que borrar)."""
    cfg = config()
    if cfg.almacen != "s3":
        return
    try:
        _s3().delete_object(Bucket=cfg.s3_bucket, Key=cfg.s3_prefijo + rel)
    except Exception:
        pass
