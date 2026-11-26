"""Material de trabajo de cada modelo (no se publica): fotos de la modelo, ejemplos y música. Portado de Lite (material.py).

Vive en material/<modelo>/<sección>/ (la sección "general", compartida, en material/_general/general/).
Las etiquetas y datos de cada archivo (link, autor, tema...) van a la tabla material_meta.
Los ejemplos de TikTok se bajan pegando el link (yt-dlp, trabajo "bajar_material") y la música se saca con ffmpeg.
Las multicuentas usan el material de su cuenta main (servicios.cuentas.modelo_de).
"""
from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import tempfile
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
log = logging.getLogger(__name__)
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
    """Logger para yt-dlp: no ensucia la salida pero se queda con los avisos (ej. «cookies are no longer valid»)."""
    def __init__(self):
        self.avisos: list[str] = []

    def debug(self, m): pass
    def info(self, m): pass

    def warning(self, m):
        self.avisos.append(str(m))

    def error(self, m):
        self.avisos.append(str(m))


@contextlib.contextmanager
def cookies_ytdlp():
    """Una COPIA de la sesión «para mirar» para yt-dlp. yt-dlp reescribe el cookiefile al terminar y, si Instagram
    le dice que la sesión no vale, le borra el sessionid: con la copia, el archivo de ella queda como lo cargó."""
    original = almacen.raiz() / COOKIES_REL
    if not original.is_file():
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="hl-ck-") as d:
        copia = Path(d) / "cookies.txt"
        shutil.copyfile(original, copia)
        copia.chmod(0o600)
        yield str(copia)


def _sesion_guardada(red: str) -> str:
    """El valor de la cookie de sesión de esa red en cookies.txt («» si no hay)."""
    try:
        from .buscador import SESION_DE, _red, cookies_guardadas
        return next((c["value"] for c in cookies_guardadas() if _red(c["domain"]) == red
                     and c["name"] == SESION_DE[red]), "")
    except Exception:
        return ""


def _hay_sesion(link: str) -> bool:
    from .buscador import estado_sesion
    try:
        est = estado_sesion()
    except Exception:
        return False
    return bool(est.get("tiktok" if "tiktok.com" in link else "instagram"))


def motivo_ytdlp(ex: Exception, link: str, avisos: list[str] | None = None) -> str:
    """El error de yt-dlp en criollo, diciendo qué hacer."""
    txt = (str(ex) + " " + " ".join(avisos or [])).lower()
    ig = "instagram.com" in str(link)
    red = "Instagram" if ig else "TikTok" if "tiktok.com" in str(link) else ""
    if "no longer valid" in txt:
        if ig:
            return ("La sesión de Instagram venció (la cerraron o expiró). Si guardaste usuario y contraseña en Memes → "
                    "«Sesión de Instagram», se vuelve a iniciar sola; si no, iniciá sesión ahí.")
        return (f"La sesión de {red or 'la cuenta para mirar'} venció (la cerraron o expiró). Cargá las cookies de nuevo "
                "en Memes → «Sesión de Instagram» → «Avanzado».")
    if "photo" in txt:
        return "Ese link es de fotos (no video): sacales captura y subilas."
    if "pinterest." in str(link) or "pin.it/" in str(link):
        if any(k in txt for k in ("no video", "not available", "unable to extract", "no formats", "404")):
            return "Ese pin no tiene video (es una imagen) o lo borraron: probá con otro o subí el video con «Subir video»."
        return "No se pudo bajar ese pin de Pinterest. Fijate que sea un pin de video público."
    if red and any(k in txt for k in ("login", "cookie", "rate", "empty media", "no video in this post",
                                       "registered users", "not available")):
        if _hay_sesion(link):
            return (f"{red} no dio ese video ni con la sesión cargada: puede ser de fotos, privado o borrado. Abrilo en "
                    "el navegador; si es un video público, subilo con «Subir video».")
        if ig:
            return ("Instagram no da los videos sin sesión. Iniciá sesión con la cuenta «para mirar» en Memes → «Sesión "
                    "de Instagram», o subilo con el botón «Subir video».")
        return (f"{red} no da los videos sin sesión. Cargá sus cookies en Memes → «Sesión de Instagram» → «Avanzado», o "
                "subilo con el botón «Subir video».")
    if "private" in txt:
        return "Ese video es privado: subilo con el botón «Subir video»."
    return "No se pudo bajar. Fijate que el link sea de un video público."


ESQUEMAS_VIDEO = ("https://",)   # de dónde se acepta un video directo guardado en el banco (los tests suman el local)


def video_valido(url) -> bool:
    return isinstance(url, str) and len(url) <= 1000 and url.startswith(ESQUEMAS_VIDEO)


def _url_descarga(link: str) -> str:
    """Los pines que encontró el buscador sin sesión tienen un id codificado que yt-dlp no entiende: para esos se baja el
    video directo (CDN público de Pinterest) que se guardó en el banco junto con el pin."""
    if "pinterest." not in str(link):
        return link
    from ..modelos import LinkBanco
    try:
        with sesion() as s:
            x = s.get(LinkBanco, str(link))
            v = str(((x.datos or {}).get("video") if x else "") or "")
    except Exception:
        return link
    return v if video_valido(v) else link


def bajar_video(link: str, carpeta_destino: Path, nombre: str = "%(uploader).40B_%(id)s") -> tuple[Path, dict]:
    """yt-dlp. Devuelve (ruta, info). ValueError con el motivo en criollo."""
    try:
        import yt_dlp
    except ImportError:
        raise ValueError("Falta yt-dlp en el servidor.")
    carpeta_destino = Path(carpeta_destino)
    carpeta_destino.mkdir(parents=True, exist_ok=True)
    log_y = _Callado()
    opts = {"outtmpl": str(carpeta_destino / f"{nombre}.%(ext)s"), "restrictfilenames": True,
            # un archivo con video y audio; si no hay (el HLS de Pinterest los trae por separado), el mejor video + el
            # mejor audio, unidos con ffmpeg en un MP4
            "format": "b[vcodec^=h264]/b[vcodec^=avc]/b/bv*+ba/bv*", "merge_output_format": "mp4",
            "noplaylist": True, "quiet": True,
            "no_warnings": True, "noprogress": True, "logger": log_y, "ffmpeg_location": medios.ffmpeg(),
            "socket_timeout": 20, "retries": 2}
    red = "tiktok" if "tiktok.com" in link else "instagram"
    sesion_antes = _sesion_guardada(red)
    try:
        with cookies_ytdlp() as ck:
            if ck:
                opts["cookiefile"] = ck
            capturado: list[dict] = []
            with yt_dlp.YoutubeDL(opts) as y:
                _capturar_datos_ig(y, link, capturado)
                info = y.extract_info(_url_descarga(link), download=True)
                hecho = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
                video = Path(hecho[0]) if hecho else Path(y.prepare_filename(info))
            if not video.is_file():
                raise RuntimeError("no se guardó")
            if not tiene_audio(video):
                video = _sumar_audio_aparte(yt_dlp, info, video, opts, log_y) or video
            if not tiene_audio(video):
                video = _sumar_audio_de_instagram(yt_dlp, info, video, opts,
                                                  capturado[-1] if capturado else None) or video
                if not tiene_audio(video):
                    log.warning("el video %s (%s) quedó sin sonido: no hubo audio en ningún lado", video.name, link)
    except Exception as ex:
        log.info("yt-dlp no pudo con %s: %s | %s", link, ex, " | ".join(log_y.avisos)[:500])
        # solo si la sesión que se usó sigue siendo la guardada: si mientras tanto cargaron o iniciaron otra, el
        # error es de la vieja y la nueva no venció
        if any("no longer valid" in a.lower() for a in log_y.avisos) and _sesion_guardada(red) == sesion_antes:
            try:
                from .buscador import marcar_sesion
                marcar_sesion(red, True, "Venció al bajar un video (la cerraron o expiró)")
            except Exception:
                pass
        raise ValueError(motivo_ytdlp(ex, link, log_y.avisos)) from None
    return video, info


def tiene_audio(video: Path) -> bool:
    ff = medios.ffmpeg()
    if not ff:
        return True
    try:
        r = subprocess.run([ff, "-hide_banner", "-i", str(video)], capture_output=True, text=True, errors="ignore",
                           timeout=60)
    except Exception:
        return True
    return "Audio:" in r.stderr


def _sumar_audio_aparte(yt_dlp, info: dict, video: Path, opts: dict, log_y) -> Path | None:
    """Instagram (y algunos HLS) sirven el video sin sonido y el audio aparte, sin decir que el video no lo tiene:
    yt-dlp baja solo el video. Si quedó mudo y hay una pista de audio sola, se baja y se une (si no, queda como está)."""
    solos = [f for f in info.get("formats") or [] if f.get("vcodec") == "none" and f.get("acodec") != "none"
             and f.get("format_id") and (f.get("url") or f.get("manifest_url") or f.get("fragments"))]
    if not solos:
        log.info("el video %s vino sin sonido y no hay audio aparte", video.name)
        return None
    mejor = max(solos, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
    oculta = _temporales(video)
    tmp = oculta / (video.stem + ".audio")
    try:
        o = {**opts, "format": mejor["format_id"], "outtmpl": str(tmp) + ".%(ext)s"}
        o.pop("merge_output_format", None)
        with yt_dlp.YoutubeDL(o) as y:
            y.process_ie_result(dict(info), download=True)
        audio = next(iter(sorted(oculta.glob(tmp.name + ".*"))), None)
        if not audio:
            return None
        junto = oculta / (video.stem + ".con-audio.mp4")
        r = subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0",
                            "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(junto)],
                           capture_output=True, text=True, errors="ignore", timeout=600)
        if r.returncode or not junto.is_file() or not tiene_audio(junto):
            log.info("no pude unir el audio de %s: %s", video.name, r.stderr[-200:])
            junto.unlink(missing_ok=True)
            return None
        final = video.with_suffix(".mp4")
        junto.replace(final)
        if video != final:
            video.unlink(missing_ok=True)
        return final
    except Exception as ex:
        log.info("no pude bajar el audio aparte de %s: %s", video.name, ex)
        return None
    finally:
        shutil.rmtree(oculta, ignore_errors=True)


TEMPORALES_VIEJOS = 3600   # segundos: una carpeta temporal más vieja que esto ya no es de una descarga en curso


def _temporales(video: Path) -> Path:
    """Una carpeta oculta y propia de esta descarga, al lado del video: si el worker se corta a mitad, lo que quede
    no aparece en Ejemplos ni en la vista previa (las listas saltean lo oculto y las carpetas). De paso barre las de
    más de una hora que dejó una descarga cortada por un reinicio (las recientes pueden ser de otra que sigue)."""
    import time
    limite = time.time() - TEMPORALES_VIEJOS
    for vieja in video.parent.glob(".temporal-*"):
        try:
            if vieja.is_dir() and not vieja.is_symlink() and vieja.stat().st_mtime < limite:
                shutil.rmtree(vieja)
        except Exception as ex:
            log.info("no pude barrer %s: %s", vieja.name, ex)
    d = video.parent / f".temporal-{uuid.uuid4().hex[:10]}"
    d.mkdir()
    return d


def max_volumen(p: Path) -> float | None:
    """El pico del audio en dB (-91 = silencio puro). None si no se pudo medir."""
    ff = medios.ffmpeg()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-hide_banner", "-nostats", "-i", str(p), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, errors="ignore", timeout=300)
    except Exception:
        return None
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
    if not m:
        return None
    return -91.0 if m.group(1) == "-inf" else float(m.group(1))


# El sonido de un reel cuando el video viene mudo y no hay pista de audio suelta: está en los datos del reel
# (api/v1/media/<pk>/info → items[0].clips_metadata), como audio original o como la canción con licencia.
ESQUEMAS_AUDIO = ("https://",)                        # los tests suman el servidor local
HOSTS_AUDIO_IG = (".cdninstagram.com", ".fbcdn.net")  # de dónde se acepta bajar ese audio
MAX_AUDIO = 60 * 1024 * 1024
SUENA_DB = -60.0                                      # pico mínimo para contar como «con sonido» (silencio = -91)
_LETRAS_IG = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_RUTA_DATOS_IG = re.compile(r"/api/v1/media/\d+/info/?$")


def _pk_instagram(codigo: str) -> int:
    """El número del reel a partir de su código del link (lo mismo que hace yt-dlp; los privados traen 28 de más)."""
    codigo = codigo[:-28] if len(codigo) > 28 else codigo
    n = 0
    for c in codigo:
        n = n * 64 + _LETRAS_IG.index(c)
    return n


def _capturar_datos_ig(y, link: str, capturado: list) -> None:
    """Con la sesión, yt-dlp ya pide los datos del reel (media/<pk>/info) para sacar el video. Se guarda esa respuesta
    para no pedirla otra vez con la cuenta «para mirar» si después hay que buscar el sonido."""
    if "instagram.com" not in str(link):
        return
    try:
        ie = y.get_info_extractor("Instagram")
        original = ie._download_json
    except Exception:
        return

    def _guardando(url, *a, **kw):
        res = original(url, *a, **kw)
        try:
            import urllib.parse
            u = url if isinstance(url, str) else getattr(url, "url", "")
            if isinstance(res, dict) and _RUTA_DATOS_IG.search(urllib.parse.urlsplit(str(u)).path):
                capturado.append(res)
        except Exception:
            pass
        return res

    ie._download_json = _guardando


def _audio_permitido(url) -> bool:
    if not isinstance(url, str) or len(url) > 4000 or not url.startswith(ESQUEMAS_AUDIO):
        return False
    import urllib.parse
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == h.lstrip(".") or host.endswith(h) for h in HOSTS_AUDIO_IG)


def _ms(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 36e5 else None


def _audios_en_datos(medio) -> list[tuple[str, float]]:
    """Los sonidos del reel en sus datos, en orden: el audio original y después la canción entera
    (fast_start_progressive_download_url). Cada uno con el segundo en que arranca en el reel. Si se sabe cuánto dura y
    el arranque cae al final o después, ese no sirve. Cualquier forma rara → lista vacía."""
    cm = medio.get("clips_metadata") if isinstance(medio, dict) else None
    if not isinstance(cm, dict):
        return []
    orig = cm.get("original_sound_info")
    mus = cm.get("music_info")
    asset = mus.get("music_asset_info") if isinstance(mus, dict) else None
    consumo = mus.get("music_consumption_info") if isinstance(mus, dict) else None
    crudos = []
    if isinstance(orig, dict):
        crudos.append((orig.get("progressive_download_url"), orig.get("audio_asset_start_time_in_ms"),
                       orig.get("duration_in_ms")))
    if isinstance(asset, dict):
        desde = consumo.get("audio_asset_start_time_in_ms") if isinstance(consumo, dict) else None
        for k in ("fast_start_progressive_download_url", "progressive_download_url"):
            crudos.append((asset.get(k), desde, asset.get("duration_in_ms")))
    out, vistos = [], set()
    for url, desde, dura in crudos:
        if not _audio_permitido(url) or url in vistos:
            continue
        d, total = _ms(desde) or 0.0, _ms(dura)
        if total is not None and d >= total:
            log.info("el sonido del reel arranca en %.0f ms y dura %.0f ms: no sirve", d, total)
            continue
        vistos.add(url)
        out.append((url, d / 1000))
    return out


def _bajar_audio(y, url: str, destino: Path) -> bool:
    """Lo baja entero o nada: si el servidor dijo el tamaño y no llegó todo (corte), no sirve."""
    total = 0
    with y.urlopen(url) as r, open(destino, "wb") as f:
        esperado = None
        try:
            cl = (r.headers.get("Content-Length") if r.headers is not None else None)
            esperado = int(cl) if cl not in (None, "") else None
        except (TypeError, ValueError, AttributeError):
            esperado = None
        while bloque := r.read(1 << 16):
            total += len(bloque)
            if total > MAX_AUDIO:
                log.info("el audio del reel pasa de %d MB: no se baja", MAX_AUDIO // (1024 * 1024))
                return False
            f.write(bloque)
    if esperado is not None and total != esperado:
        log.info("el audio del reel llegó cortado (%d de %d bytes)", total, esperado)
        return False
    return total > 0


def _sumar_audio_de_instagram(yt_dlp, info: dict, video: Path, opts: dict, datos: dict | None = None) -> Path | None:
    """Reel mudo sin pista de audio suelta: el sonido sale de los datos del reel (los que ya bajó yt-dlp; si no los
    bajó, se piden una vez con la sesión «para mirar» por su extractor de Instagram). Se baja el audio original o la
    canción, se une al video desde el segundo en que arranca y al largo del video, y solo se usa si de verdad suena.
    Sin sesión, o si nada sirve, queda como está."""
    if info.get("extractor_key") != "Instagram" or not isinstance(info.get("id"), str) \
            or not re.fullmatch(r"[\w-]{5,80}", info["id"]):
        return None
    oculta = None
    try:
        o = {k: v for k, v in opts.items() if k not in ("merge_output_format",)}
        with yt_dlp.YoutubeDL(o) as y:
            if datos is None:
                ie = y.get_info_extractor("Instagram")
                if not ie._is_logged_in:
                    log.info("el video %s vino sin sonido; sin la sesión «para mirar» no se pueden pedir los datos "
                             "del reel", video.name)
                    return None
                datos = ie._download_json(f"{ie._API_BASE_URL}/media/{_pk_instagram(info['id'])}/info/", info["id"],
                                          note=False, errnote=False, fatal=False, headers=ie._api_headers)
            items = datos.get("items") if isinstance(datos, dict) else None
            audios = _audios_en_datos(items[0]) if isinstance(items, list) and items else []
            if not audios:
                log.info("el video %s vino sin sonido y los datos del reel no traen audio", video.name)
                return None
            oculta = _temporales(video)
            for n, (url, desde) in enumerate(audios):
                tmp, junto = oculta / f"musica-{n}", oculta / f"con-musica-{n}.mp4"
                try:
                    if not _bajar_audio(y, url, tmp):
                        continue
                except Exception as ex:
                    log.info("no pude bajar el audio del reel para %s: %s", video.name, ex)
                    continue
                r2 = subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-i", str(video), "-ss", f"{desde:.3f}",
                                     "-i", str(tmp), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                                     "-b:a", "192k", "-af", "apad", "-shortest", str(junto)],
                                    capture_output=True, text=True, errors="ignore", timeout=600)
                if r2.returncode or not junto.is_file() or not tiene_audio(junto):
                    log.info("no pude unir el audio del reel a %s: %s", video.name, r2.stderr[-200:])
                    continue
                pico = max_volumen(junto)
                if pico is None or pico <= SUENA_DB:
                    log.info("el audio del reel unido a %s quedó en silencio (pico %s dB): no sirve", video.name, pico)
                    continue
                final = video.with_suffix(".mp4")
                junto.replace(final)
                if video != final:
                    video.unlink(missing_ok=True)
                return final
            return None
    except Exception as ex:
        log.info("no pude bajar el audio del reel para %s: %s", video.name, ex)
        return None
    finally:
        if oculta is not None:
            shutil.rmtree(oculta, ignore_errors=True)


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
