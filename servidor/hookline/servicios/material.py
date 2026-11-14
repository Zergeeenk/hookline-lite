"""Material de trabajo de cada modelo (no se publica): fotos de la modelo, ejemplos y música. Portado de Lite (material.py).

Vive en material/<modelo>/<sección>/ (la sección "general", compartida, en material/_general/general/).
Las etiquetas y datos de cada archivo (link, autor, tema...) van a la tabla material_meta.
Los ejemplos de TikTok se bajan pegando el link (yt-dlp, trabajo "bajar_material") y la música se saca con ffmpeg.
Las multicuentas usan el material de su cuenta main (servicios.cuentas.modelo_de).
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
import uuid
from pathlib import Path

from sqlalchemy import delete, select

from .. import almacen, medios
from ..comun import actividad, ajuste, candado, guardar_ajuste
from ..config import config
from ..db import sesion
from ..modelos import Cuenta, MaterialMeta
from ..trabajos import encolar, manejador
from . import cuentas as svc_cuentas

AUDIOS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}
SECCIONES = {"modelo": medios.FOTOS | medios.VIDEOS, "ejemplos": medios.FOTOS | medios.VIDEOS,
             "musica": AUDIOS | medios.VIDEOS, "general": medios.FOTOS | medios.VIDEOS}
GENERAL = "_general"
CARPETAS_HISTORIAS = ("Salidas y comida", "Día a día", "historias")    # de acá salen las historias
COOKIES_REL = "navegador/cookies.txt"                                   # sesión del buscador (para bajar de Instagram)


def tipo_de(p) -> str:
    s = Path(p).suffix.lower()
    return "foto" if s in medios.FOTOS else "video" if s in medios.VIDEOS else "audio" if s in AUDIOS else ""


def _de(modelo: str, sec: str) -> str:
    return GENERAL if sec == "general" else modelo


def _base(modelo: str) -> Path:
    return almacen.dir_de("material", modelo)


def secciones(modelo: str) -> dict:
    """Las fijas + las carpetas que ella cree. La vieja "modelo" se oculta si quedó vacía."""
    d = _base(modelo)
    vacia = not any((d / "modelo").glob("*.*")) if (d / "modelo").is_dir() else True
    out = {k: v for k, v in SECCIONES.items() if not (k == "modelo" and vacia)}
    for c in sorted(d.iterdir()):
        if c.is_dir() and c.name not in SECCIONES and not c.name.startswith(("_", ".")):
            out[c.name] = medios.FOTOS | medios.VIDEOS
    return out


def carpeta(modelo: str, sec: str) -> Path:
    if sec not in secciones(modelo) and sec not in SECCIONES:
        raise ValueError("Carpeta desconocida.")
    return almacen.dir_de("material", _de(modelo, sec), sec)


def _modelo(cuenta: str) -> str:
    if not svc_cuentas.existe(cuenta):
        raise ValueError("Elegí una cuenta primero.")
    return svc_cuentas.modelo_de(cuenta)


def _metas(modelo: str) -> dict:
    with sesion() as s:
        return {f"{m.seccion}/{m.nombre}": {"etiquetas": m.etiquetas or [], **(m.meta or {})}
                for m in s.scalars(select(MaterialMeta).where(MaterialMeta.modelo == modelo))}


def poner_meta(modelo: str, sec: str, nombre: str, etiquetas=None, **meta) -> None:
    with sesion() as s:
        m = s.scalar(select(MaterialMeta).where(MaterialMeta.modelo == modelo, MaterialMeta.seccion == sec,
                                                MaterialMeta.nombre == nombre))
        if not m:
            m = MaterialMeta(modelo=modelo, seccion=sec, nombre=nombre, etiquetas=[], meta={})
            s.add(m)
        if etiquetas is not None:
            m.etiquetas = list(etiquetas)
        if meta:
            m.meta = {**(m.meta or {}), **meta}


# --- públicas ------------------------------------------------------------------------------------------------------------
def listar(cuenta: str) -> dict:
    modelo = _modelo(cuenta)
    metas = {modelo: _metas(modelo), GENERAL: _metas(GENERAL)}
    out = {}
    for sec, exts in secciones(modelo).items():
        meta = metas[_de(modelo, sec)]
        d = carpeta(modelo, sec)
        fs = sorted((p for p in d.glob("*") if p.is_file() and p.suffix.lower() in exts),
                    key=lambda p: p.stat().st_mtime, reverse=True)
        out[sec] = [{"nombre": p.name, "archivo": almacen.relativa(p), "tipo": tipo_de(p), "fecha": p.stat().st_mtime,
                     "etiquetas": [], **meta.get(f"{sec}/{p.name}", {})} for p in fs]
    bj = ajuste("material_bajando", {}) or {}
    out["bajando"] = [{"id": k, **v} for k, v in bj.items() if v.get("cuenta") == modelo]
    return out


def guardar(cuenta: str, sec: str, nombre: str, datos: bytes) -> str:
    modelo = _modelo(cuenta)
    limpio = almacen.nombre_seguro(nombre)
    if Path(limpio).suffix.lower() not in secciones(modelo).get(sec, ()):
        raise ValueError({"modelo": "Acá van fotos o videos de la modelo.", "ejemplos": "Acá van fotos, capturas o videos.",
                          "musica": "Acá van audios (mp3, m4a, wav) o videos de los que se saca la música.",
                          "general": "Acá van fotos, capturas o videos."}.get(sec, "Sección desconocida."))
    destino = almacen.libre(carpeta(modelo, sec) / limpio)
    destino.write_bytes(datos)
    if sec == "musica" and medios.es_video(destino):   # subió un video a Música: se queda solo con el audio
        audio = sacar_audio(destino, destino.with_suffix(".m4a"))
        destino.unlink(missing_ok=True)
        if not audio:
            raise ValueError("No se pudo sacar la música de ese video.")
        destino = audio
    return destino.name


def nueva_carpeta(cuenta: str, nombre: str) -> str:
    modelo = _modelo(cuenta)
    nombre = re.sub(r"\s+", " ", re.sub(r"[^\w áéíóúñÁÉÍÓÚÑ\-]", "", str(nombre or ""))).strip()[:40]
    if not nombre or nombre.startswith(("_", ".")):
        raise ValueError("Escribí un nombre para la carpeta.")
    if nombre.lower() in {k.lower() for k in secciones(modelo)}:
        raise ValueError("Ya hay una carpeta con ese nombre.")
    (_base(modelo) / nombre).mkdir(parents=True, exist_ok=True)
    return nombre


def ruta(cuenta: str, sec: str, nombre: str) -> Path:
    modelo = _modelo(cuenta)
    p = carpeta(modelo, sec) / Path(str(nombre)).name
    if not p.is_file():
        raise ValueError("Ese archivo ya no está.")
    return p


def borrar(cuenta: str, sec: str, nombre: str) -> None:
    modelo = _modelo(cuenta)
    p = ruta(cuenta, sec, nombre)
    p.unlink()
    with sesion() as s:
        s.execute(delete(MaterialMeta).where(MaterialMeta.modelo == _de(modelo, sec), MaterialMeta.seccion == sec,
                                             MaterialMeta.nombre == p.name))


def etiquetar(cuenta: str, sec: str, nombre: str, etiquetas) -> list:
    modelo = _modelo(cuenta)
    p = ruta(cuenta, sec, nombre)
    limpias = []
    for e in etiquetas or []:
        e = re.sub(r"\s+", " ", str(e)).strip().lower()[:30]
        if e and e not in limpias:
            limpias.append(e)
    poner_meta(_de(modelo, sec), sec, p.name, etiquetas=limpias[:12])
    return limpias[:12]


def a_bandeja(cuenta: str, sec: str, nombre: str) -> str:
    src = ruta(cuenta, sec, nombre)
    if not (medios.es_foto(src) or medios.es_video(src)):
        raise ValueError("A la bandeja van solo fotos o videos.")
    from . import piezas
    return piezas.copiar_a_bandeja(cuenta, src, src.name)


def resumen_modelos() -> list[dict]:
    """Las cuentas main (cada una es una modelo) con el resumen de su material (formato /api/modelos de Lite)."""
    from ..comun import iso_local
    from datetime import datetime, timezone
    with sesion() as s:
        cs = s.scalars(select(Cuenta).order_by(Cuenta.id)).all()
    out = []
    for c in cs:
        if c.main_id:
            continue
        vid = fot = 0
        ultima, foto = 0.0, None
        secs = [k for k in secciones(c.id) if k != "general"]
        for sec in secs:
            d = carpeta(c.id, sec)
            for f in d.iterdir():
                if not f.is_file():
                    continue
                if medios.es_video(f):
                    vid += 1
                elif medios.es_foto(f):
                    fot += 1
                    if sec in ("modelo", "Fotos") and (foto is None or f.stat().st_mtime < foto.stat().st_mtime):
                        foto = f
                ultima = max(ultima, f.stat().st_mtime)
        out.append({"id": c.id, "usuario": c.usuario_ig or c.id, "videos": vid, "fotos": fot, "carpetas": len(secs),
                    "multicuentas": sum(1 for h in cs if h.main_id == c.id),
                    "ultima": iso_local(datetime.fromtimestamp(ultima, timezone.utc)) if ultima else "",
                    "foto": almacen.relativa(foto) if foto else ""})
    return out


def carpetas_reels(cuenta: str) -> list[str]:
    """Para los reels: todas las carpetas de la modelo menos historias, ejemplos, música y general."""
    if not svc_cuentas.existe(cuenta):
        return []
    fuera = set(CARPETAS_HISTORIAS) | {"ejemplos", "musica", "general"}
    return [k for k in secciones(svc_cuentas.modelo_de(cuenta)) if k not in fuera]


def material_en(cuenta: str, carpetas) -> list[tuple[Path, list, str, dict]]:
    """[(ruta, etiquetas, carpeta, item)] de la modelo de esa cuenta, en esas carpetas."""
    if not svc_cuentas.existe(cuenta):
        return []
    modelo = svc_cuentas.modelo_de(cuenta)
    todo = listar(cuenta)
    return [(carpeta(modelo, k) / x["nombre"], x.get("etiquetas") or [], k, x)
            for k in carpetas if k in todo for x in todo[k]]


def musicas(cuenta: str) -> list[Path]:
    if not svc_cuentas.existe(cuenta):
        return []
    d = carpeta(svc_cuentas.modelo_de(cuenta), "musica")
    return [p for p in d.glob("*.*") if p.suffix.lower() in AUDIOS]


def guardar_musica(video: Path, cuenta: str, nombre: str) -> None:
    if not svc_cuentas.existe(cuenta):
        return
    destino = carpeta(svc_cuentas.modelo_de(cuenta), "musica") / f"{nombre}.m4a"
    if not destino.exists():
        sacar_audio(Path(video), destino)


# --- bajar de TikTok / Instagram ---------------------------------------------------------------------------------------------
class _Callado:
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


def bajar_video(link: str, carpeta_destino: Path, nombre: str = "%(uploader).40B_%(id)s") -> tuple[Path, dict]:
    """yt-dlp. Devuelve (ruta, info). ValueError con el motivo en criollo."""
    try:
        import yt_dlp
    except ImportError:
        raise ValueError("Falta yt-dlp en el servidor.")
    carpeta_destino = Path(carpeta_destino)
    carpeta_destino.mkdir(parents=True, exist_ok=True)
    opts = {"outtmpl": str(carpeta_destino / f"{nombre}.%(ext)s"), "restrictfilenames": True,
            "format": "b[vcodec^=h264]/b[vcodec^=avc]/b", "noplaylist": True, "quiet": True,
            "no_warnings": True, "noprogress": True, "logger": _Callado(), "ffmpeg_location": medios.ffmpeg()}
    cookies = almacen.raiz() / COOKIES_REL
    if cookies.is_file():
        opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(link, download=True)
            video = Path(y.prepare_filename(info))
        if not video.is_file():
            raise RuntimeError("no se guardó")
    except Exception as ex:
        txt = str(ex).lower()
        if "photo" in txt:
            raise ValueError("Ese link es de fotos (no video): sacales captura y subilas.")
        if "instagram" in link and any(k in txt for k in ("login", "cookie", "rate", "empty media")):
            raise ValueError("Instagram no dejó bajar ese video sin iniciar sesión. Cargá las cookies del buscador en "
                             "Ajustes o subilo con el botón «Subir video».")
        raise ValueError("No se pudo bajar. Fijate que el link sea de un video público.")
    return video, info


def sacar_audio(video: Path, destino: Path) -> Path | None:
    ff = medios.ffmpeg()
    if not ff:
        return None
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([ff, "-y", "-v", "error", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "192k", str(destino)],
                           capture_output=True, timeout=180)
    except Exception:
        return None
    if r.returncode or not destino.is_file() or destino.stat().st_size < 1000:
        destino.unlink(missing_ok=True)
        return None
    return destino


def _bajando(cambiar) -> dict:
    with candado("material_bajando"):
        bj = dict(ajuste("material_bajando", {}) or {})
        cambiar(bj)
        guardar_ajuste("material_bajando", bj)
        return bj


def bajar(cuenta: str, link: str) -> str:
    modelo = _modelo(cuenta)
    link = str(link or "").strip()
    if not re.match(r"https?://", link):
        raise ValueError("Pegá el link completo (empieza con https://).")
    bj = ajuste("material_bajando", {}) or {}
    if any(v["link"] == link and v["cuenta"] == modelo and not v.get("error") for v in bj.values()):
        raise ValueError("Ese link ya se está bajando.")
    k = uuid.uuid4().hex[:8]
    _bajando(lambda b: b.__setitem__(k, {"cuenta": modelo, "link": link, "error": ""}))
    encolar("bajar_material", {"id": k, "modelo": modelo, "link": link}, clave=f"bajar_material:{k}", max_intentos=1)
    return k


def descartar(k: str) -> None:
    _bajando(lambda b: b.pop(str(k), None))


@manejador("bajar_material")
def _bajar(datos: dict) -> None:
    k, modelo, link = datos["id"], datos["modelo"], datos["link"]
    try:
        video, info = bajar_video(link, carpeta(modelo, "ejemplos"))
    except ValueError as ex:
        msg = str(ex)
        _bajando(lambda b: b.get(k, {}).update(error=msg))
        return
    tema = " - ".join(x for x in [info.get("track"), ", ".join(info.get("artists") or []) or info.get("artist")] if x)
    musica = sacar_audio(video, almacen.libre(carpeta(modelo, "musica") / f"{video.stem}.m4a"))
    poner_meta(modelo, "ejemplos", video.name, link=link, autor=info.get("uploader") or "",
               titulo=(info.get("description") or info.get("title") or "")[:200], tema=tema,
               musica=musica.name if musica else "", bajado=datetime.now(config().tz).strftime("%Y-%m-%dT%H:%M:%S"))
    if musica:
        poner_meta(modelo, "musica", musica.name, link=link, autor=info.get("uploader") or "", tema=tema, de=video.name)
    actividad("material", f"Bajado un ejemplo para @{modelo}", img=almacen.relativa(video), cuenta=modelo)
    descartar(k)
