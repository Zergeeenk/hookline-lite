"""Fábrica de reels ("Copiar video"). Portado de Hookline Lite (copiar.py).

Toma un video de ejemplo (link de Instagram/TikTok o archivo), pone a la modelo en la primera parte con el mismo texto y
sigue con el remate del ejemplo. Lo arma ffmpeg; no se publica nada.

Cada copia vive en copias/<id>/ (ejemplo.*, hoja.jpg, modelo-*.ext, audio-*.ext, remate-*.mp4, resultado-<ts>.mp4,
revision.jpg) y sus datos en la tabla copias (columna `datos` = lo que en Lite era copia.json).

Pipeline (todo en trabajos del worker):
  descargar_ejemplo → preparar_copia (hoja de cuadros) → tarea IA "lectura" → elegir foto → render → tarea IA "juez"
  → aprobar, o rehacer (otra foto / texto / música) hasta REINTENTOS.

Diferencia con Lite: NO se borran marcas de agua del video ajeno. Si la lectura dice que el remate tiene marca de agua,
la copia queda frenada para que la dueña la descarte o suba un remate propio (subir_remate).
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from .. import almacen, chat_img, fuentes, medios
from ..comun import actividad, candado
from ..db import ahora, sesion
from ..ia import tareas
from ..modelos import Copia, LinkBanco
from ..trabajos import encolar, manejador
from . import material

ESTILOS = ("sombra", "caja", "contorno", "nada")
COLORES = {"blanco": "ffffff", "negro": "111111", "amarillo": "ffe14d", "rosa": "ff5fa2", "rojo": "ff3b3b",
           "celeste": "5cc8ff"}
AUDIOS = ("ejemplo", "modelo", "musica", "ninguno")
VIEJOS_TAM = {"chico": 0.034, "medio": 0.042, "grande": 0.052}
VIEJAS_POS = {"arriba": 0.17, "medio": 0.45, "abajo": 0.72}
ANCHO_MAX = 0.72      # el texto se parte en renglones para no pasar el 72 % del ancho
INTERLINEA = 1.25
CAJA_PAD = 0.28
MAX_VERSIONES = 15
MAX_STICKERS = 8
STICKER_LADO = 512        # se guardan a 512 px como mucho (un emoji no necesita más)
IG_ARRIBA, IG_ABAJO = 0.11, 0.81     # lo que tapa Instagram en un reel: usuario arriba, texto y botones abajo
ESCALA_TEXTO = 1.2                   # la letra un 20 % más grande que la del ejemplo
NOTA_MIN, REINTENTOS = 3, 2          # juez: nota 1-5; menos de 3 se rehace (hasta 2 veces)
ACTIVOS = ("bajando", "leyendo", "armando")
_lock = threading.RLock()

log = logging.getLogger("hookline.fabrica")


# --- utilidades ffmpeg / texto ------------------------------------------------------------------------------------------------
def _info_video(p: Path) -> tuple[int, int, float, bool]:
    """(ancho, alto, duración, tiene_audio)."""
    r = subprocess.run([medios.ffmpeg(), "-i", str(p)], capture_output=True, text=True, errors="ignore")
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (720, 1280)
    rot = re.search(r"rotation of (-?\d+)", r.stderr) or re.search(r"rotate\s*:\s*(-?\d+)", r.stderr)
    if rot and abs(int(rot.group(1))) in (90, 270):
        w, h = h, w
    return w - w % 2, h - h % 2, medios.duracion(p) or 0, "Audio:" in r.stderr


# --- emojis en el texto: la letra no los tiene, así que se dibujan con el pack (Fluent 3D) en su lugar del renglón ---
PUA = 0xE000          # cada emoji del texto se cambia por un carácter privado (U+E000…) mientras se arman los renglones
VS16, TONOS = "\ufe0f", tuple(chr(c) for c in range(0x1F3FB, 0x1F400))


def _glifos() -> tuple[dict, int]:
    return _glifos_de(_version_pack())


@functools.lru_cache(maxsize=4)
def _glifos_de(_version: str) -> tuple[dict, int]:
    m = {}
    for e in _indice_emojis().values():
        g = str(e.get("e") or "").replace(VS16, "")
        if g:
            m.setdefault(g, e["k"])
    return m, max((len(g) for g in m), default=1)


def emojis_del_texto(texto: str) -> tuple[str, dict]:
    """Cambia cada emoji que está en el pack por un marcador (U+E000, U+E001…) y devuelve el texto y {marcador: clave}.
    Los tonos de piel se ignoran (el pack trae el tono amarillo)."""
    m, largo = _glifos()
    out, mapa, i, t = [], {}, 0, str(texto or "")
    while i < len(t):
        hallado = None
        for n in range(min(largo + 6, len(t) - i), 0, -1):
            pedazo = t[i:i + n]
            g = "".join(ch for ch in pedazo if ch != VS16 and ch not in TONOS)
            if g in m and pedazo[0] not in (VS16,) + TONOS:
                hallado = (n, m[g])
                break
        if hallado and len(mapa) < 64:
            n, k = hallado
            marca = chr(PUA + len(mapa))
            mapa[marca] = k
            out.append(marca)
            i += n
            while i < len(t) and (t[i] == VS16 or t[i] in TONOS):
                i += 1
        else:
            out.append(t[i])
            i += 1
    return "".join(out), mapa


def _espacios_emoji(medir, tam_px) -> tuple[int, float]:
    """Cuántos espacios ocupa un emoji en el renglón (≈ el alto de la letra) y el ancho de un espacio."""
    import math
    esp = max(1.0, float(medir(" ")) or tam_px * 0.25)
    return max(1, math.ceil(tam_px * 1.05 / esp)), esp


def _renglones(texto, fuente_p, tam_px, max_px) -> list[str]:
    from PIL import ImageFont
    try:
        base = ImageFont.truetype(str(fuente_p), tam_px).getlength
    except Exception:
        base = lambda s: len(s) * tam_px * 0.55
    texto, mapa = emojis_del_texto(str(texto or "").replace("\r", ""))
    n_esp, _ = _espacios_emoji(base, tam_px)
    medir = (lambda s: base("".join(" " * n_esp if ch in mapa else ch for ch in s))) if mapa else base
    out = []
    texto = fuentes.dibujable(texto, fuente_p, conservar="".join(mapa))      # sin cuadraditos; los emojis quedan
    for parrafo in texto.split("\n"):
        linea = ""
        for palabra in parrafo.split():
            prueba = f"{linea} {palabra}".strip()
            if linea and medir(prueba) > max_px:
                out.append(linea)
                linea = palabra
            else:
                linea = prueba
        out.append(linea)
    while out and not out[-1]:
        out.pop()
    return out[:8]


def _juntar(cajas) -> list:
    """Une las cajas de caras que se superponen."""
    cajas = [list(b) for b in cajas]
    unidas = True
    while unidas:
        unidas = False
        for i in range(len(cajas)):
            for j in range(i + 1, len(cajas)):
                a, b = cajas[i], cajas[j]
                inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
                menor = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
                if inter / menor > 0.3:
                    cajas[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del cajas[j]
                    unidas = True
                    break
            if unidas:
                break
    return [[round(v, 4) for v in b] for b in cajas]


def _num(v, defecto, lo, hi):
    try:
        return max(lo, min(hi, float(str(v).replace(",", "."))))
    except (TypeError, ValueError):
        return defecto


def _esc_ff(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def normalizar(c: dict) -> dict:
    if isinstance(c.get("texto_tam"), str):
        c["texto_tam"] = VIEJOS_TAM.get(c["texto_tam"], 0.042)
    if c.get("texto_y") is None:
        c["texto_y"] = VIEJAS_POS.get(c.get("texto_pos"), 0.17)
    c.setdefault("texto_x", 0.5)
    # estilo y color: los que eligieron (borde, sombra, caja...); por defecto letra blanca sin nada, como Instagram.
    # La fuente: la elegida (a mano o por Claude) si sigue existiendo; si no, la predeterminada de la sección Fuentes
    if not isinstance(c.get("estilo"), str) or c["estilo"] not in ESTILOS:
        c["estilo"] = "nada"
    if not isinstance(c.get("color"), str) or c["color"] not in COLORES:
        c["color"] = "blanco"
    st = c.get("stickers")
    c["stickers"] = [x for x in st if isinstance(x, dict) and isinstance(x.get("archivo"), str)] if isinstance(st, list) else []
    if not c.get("fuente") or not fuentes.ruta(c["fuente"]):
        c["fuente"] = fuentes.predeterminada()
    c.setdefault("audio", "ejemplo")
    c.setdefault("audio_archivo", "")
    c.setdefault("audio_desde", 0)
    c.setdefault("ubicacion_auto", True)
    c.setdefault("marca_agua", False)
    c.setdefault("remate", "")
    c.setdefault("versiones", [{"archivo": c["resultado"], "armado": c.get("armado", ""), "texto": c.get("texto", "")}]
                 if c.get("resultado") else [])
    return c


# --- datos ---------------------------------------------------------------------------------------------------------------------
def _hora_local() -> str:
    from datetime import datetime
    from ..config import config
    return datetime.now(config().tz).strftime("%Y-%m-%dT%H:%M:%S")


def _raiz() -> Path:
    return almacen.dir_de("copias")


def _dir(cid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8}", str(cid or "")):
        raise ValueError("Copia desconocida.")
    return _raiz() / cid   # la carpeta la crea _nueva: si la copia se borró, no se vuelve a crear sola


def _datos(cid: str, **cambios) -> dict:
    """Lee (y si hay cambios, actualiza) los datos de una copia. El candado evita que el API y el worker se pisen."""
    if not cambios:
        with sesion() as s:
            x = s.get(Copia, str(cid))
            if x is None:
                raise ValueError("Esa copia ya no está.")
            return normalizar(dict(x.datos or {}))
    with candado("copias"), sesion() as s:
        x = s.get(Copia, str(cid))
        if x is None:
            raise ValueError("Esa copia ya no está.")
        d = normalizar(dict(x.datos or {}))
        if cambios:
            d.update(cambios)
            x.datos = d
            x.estado = d.get("estado") or x.estado
            x.cuenta_id = d.get("cuenta") or ""
            x.link = d.get("link") or ""
        return d


def listar() -> list[dict]:
    with sesion() as s:
        filas = s.scalars(select(Copia).order_by(Copia.creado.desc())).all()
    out = []
    for f in filas:
        c = normalizar(dict(f.datos or {}))
        d = _raiz() / f.id
        for k in ("ejemplo", "modelo", "resultado", "hoja", "audio_archivo", "revision", "remate"):
            if c.get(k):
                c[k + "_url"] = almacen.relativa(d / c[k]) if (d / c[k]).exists() else ""
        for v in c["versiones"]:
            v["url"] = almacen.relativa(d / v["archivo"]) if (d / v["archivo"]).exists() else ""
        sin_emojis = "".join(ch for ch in emojis_del_texto(c.get("texto"))[0] if not PUA <= ord(ch) < PUA + 64)
        c["faltan"] = fuentes.faltan(sin_emojis, c["fuente"])     # lo que esa letra no dibuja (los emojis sí salen)
        out.append(c)
    return out


def opciones() -> dict:
    return {"fuentes": fuentes.disponibles(), "colores": COLORES, "ancho_max": ANCHO_MAX, "interlinea": INTERLINEA,
            "caja_pad": CAJA_PAD, "escala_texto": ESCALA_TEXTO, "estilos": list(ESTILOS), "stickers": stickers()}


# --- stickers (emojis de iPhone, logos...): imágenes PNG que se pegan sobre la parte de la modelo -----------------------------
EMOJIS_DIR = Path(__file__).resolve().parents[1] / "emojis"


@functools.lru_cache(maxsize=1)
def _indice_base() -> dict:
    try:
        lista = json.loads((EMOJIS_DIR / "indice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["k"]: e for e in lista if isinstance(e, dict) and re.fullmatch(r"[0-9a-z-]{1,60}", str(e.get("k")))}


# --- el pack propio: el equipo sube un ZIP con sus emojis (ej. los de iPhone) y reemplazan a los 3D ---------------------------
# Van a datos/emojis-propios/<clave>.png con un pack.json {version, n, extra:[{k,n,g,e,p}]}. Los que no estaban en el
# pack 3D (emojis más nuevos) entran como "extra" en el grupo «nuevos». API y worker son procesos distintos: el índice
# se relee cuando cambia la versión del pack.json, no hace falta reiniciar nada.
EMOJI_LADO = 160
MAX_PACK = 400 * 1024 * 1024            # el ZIP
MAX_ARCHIVOS_PACK = 80_000
MAX_EMOJI_BYTES = 6 * 1024 * 1024


def _dir_pack() -> Path:
    return almacen.dir_de("emojis")


def _leer_pack() -> dict:
    f = _dir_pack() / "pack.json"
    try:
        return _leer_pack_de(str(f), f.stat().st_mtime_ns)
    except OSError:
        return {}


@functools.lru_cache(maxsize=4)
def _leer_pack_de(ruta: str, _mtime: int) -> dict:
    try:
        d = json.loads(Path(ruta).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _version_pack() -> str:
    return str(_leer_pack().get("version") or "")


def _indice_emojis() -> dict:
    return _indice_de(_version_pack())


@functools.lru_cache(maxsize=4)
def _indice_de(_version: str) -> dict:
    idx = dict(_indice_base())
    for e in _leer_pack().get("extra") or []:
        if isinstance(e, dict) and re.fullmatch(r"[0-9a-f-]{1,60}", str(e.get("k"))) and e["k"] not in idx:
            idx[e["k"]] = {"k": e["k"], "n": str(e.get("n") or "")[:80], "g": "nuevos", "e": str(e.get("e") or "")[:20],
                           "p": [str(x)[:40] for x in (e.get("p") or [])][:8]}
    return idx


def pack_emojis() -> dict:
    p = _leer_pack()
    return {"propio": int(p.get("n") or 0), "nuevos": len(p.get("extra") or []), "version": str(p.get("version") or ""),
            "nombre": str(p.get("nombre") or "")}


def emojis() -> list[dict]:
    """El pack de emojis: [{k, n, g, e, p}]. La imagen se sirve en /emoji/<k>.webp (la del pack propio si la hay)."""
    return list(_indice_emojis().values())


def ruta_emoji(k: str) -> Path | None:
    k = str(k or "")
    if k not in _indice_emojis():
        return None
    if _version_pack():
        p = _dir_pack() / f"{k}.png"
        if p.is_file():
            return p
    p = EMOJIS_DIR / "3d" / f"{k}.webp"
    return p if p.is_file() else None


def _sin_fe0f(k: str) -> str:
    """Clave comparable: sin FE0F y sin ceros a la izquierda («0023-fe0f-20e3» y «23-20e3» son el mismo #️⃣)."""
    return "-".join(f"{int(x, 16):x}" for x in k.split("-") if x and x != "fe0f")


def _es_emoji(cps: list[int]) -> bool:
    """Para los que no están en el pack 3D: solo códigos de los bloques de emojis de Unicode (así «cafe.png» o
    «logo.png» no entran como emoji)."""
    def ok(c):
        return (0x1F000 <= c <= 0x1FAFF or 0x2190 <= c <= 0x2BFF or 0xE0020 <= c <= 0xE007F
                or c in (0x200D, 0xFE0F, 0x20E3, 0x3030, 0x303D, 0x3297, 0x3299))
    return bool(cps) and all(ok(c) for c in cps) and any(c not in (0x200D, 0xFE0F, 0x20E3) for c in cps)


def _clave_de_archivo(nombre: str, por_codigo: dict, por_nombre: dict) -> tuple[str, str]:
    """De un nombre de archivo del ZIP saca (clave, emoji). Entiende «1f600.png», «emoji_u1f600.png» (Noto),
    «U+1F600.png», «1f468_200d_1f469.png», «😀.png» y el nombre en inglés («grinning face.png»)."""
    crudo = Path(nombre).stem.strip().lower()
    if crudo in por_nombre:
        return por_nombre[crudo], ""
    base = re.sub(r"[@_-]?\d+x$|[_-](\d{2,3})$", "", crudo) or crudo        # «1f600@2x», «1f600_64»
    if base in por_nombre:
        return por_nombre[base], ""
    t = re.sub(r"^(emoji_u|emoji-u|u\+|u)", "", base)
    partes = [x for x in re.split(r"[\s_+-]+|u\+", t) if x]
    if partes and all(re.fullmatch(r"[0-9a-f]{1,6}", x) for x in partes):
        cps = [int(x, 16) for x in partes]
    else:
        cps = [ord(ch) for ch in Path(nombre).stem.strip()]
    if not cps or len(cps) > 12 or any(c > 0x10FFFF or 0xD800 <= c <= 0xDFFF for c in cps):
        return "", ""
    k = "-".join(f"{c:x}" for c in cps)
    if _sin_fe0f(k) in por_codigo:
        return por_codigo[_sin_fe0f(k)], ""
    if not _es_emoji(cps) or any(0x1F3FB <= c <= 0x1F3FF for c in cps):   # tonos de piel: el texto usa el amarillo
        return "", ""
    k = _sin_fe0f(k)
    return k, "".join(chr(c) for c in cps if c != 0xFE0F)


def importar_pack_emojis(datos: bytes, nombre_pack: str = "") -> dict:
    """Un ZIP con imágenes de emojis (PNG/WebP/JPG) nombradas por su código. Reemplaza el pack propio anterior."""
    import io
    import zipfile
    from PIL import Image, UnidentifiedImageError
    if not datos or len(datos) > MAX_PACK:
        raise ValueError("Subí un ZIP de menos de 400 MB con las imágenes de los emojis.")
    try:
        z = zipfile.ZipFile(io.BytesIO(datos))
        miembros = z.infolist()
    except (zipfile.BadZipFile, OSError, ValueError):
        raise ValueError("Eso no es un ZIP: comprimí la carpeta de los emojis y subí el .zip.") from None
    if len(miembros) > MAX_ARCHIVOS_PACK:
        raise ValueError(f"El ZIP tiene más de {MAX_ARCHIVOS_PACK:,} archivos.".replace(",", "."))
    base = _indice_base()
    por_codigo = {_sin_fe0f(k): k for k in base}
    por_nombre = {e["n"].lower(): k for k, e in base.items() if e.get("n")}
    elegidos: dict[str, tuple[int, zipfile.ZipInfo, str]] = {}      # clave → (lado, archivo, emoji si es nuevo)
    for m in miembros:
        n = m.filename.replace("\\", "/")
        if m.is_dir() or "__MACOSX/" in n or Path(n).name.startswith(".") or m.file_size > MAX_EMOJI_BYTES \
                or Path(n).suffix.lower() not in (".png", ".webp", ".jpg", ".jpeg"):
            continue
        k, e = _clave_de_archivo(Path(n).name, por_codigo, por_nombre)
        if not k:
            continue
        lado = max((int(x) for x in re.findall(r"(?<![0-9a-f])(\d{2,4})(?![0-9a-f])", n) if 16 <= int(x) <= 1024),
                   default=0)                                       # carpetas 64/, 160/, 72x72/: se queda la más grande
        if k not in elegidos or lado > elegidos[k][0]:
            elegidos[k] = (lado, m, e)
    if not elegidos:
        raise ValueError("No encontré emojis en ese ZIP: los archivos tienen que llamarse por su código "
                         "(1f600.png, emoji_u1f600.png) o por el emoji (😀.png).")
    import shutil as _sh
    import time as _t
    final = _dir_pack()
    tmp = final.with_name(final.name + ".nuevo")
    _sh.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    hechos, extra = 0, []
    try:
        for k, (_, m, e) in elegidos.items():
            try:
                with z.open(m) as fh:
                    im = Image.open(io.BytesIO(fh.read(MAX_EMOJI_BYTES + 1)))
                    if im.width * im.height > 16_000_000:
                        continue
                    im.load()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError, zipfile.BadZipFile):
                continue
            im = im.convert("RGBA")
            im.thumbnail((EMOJI_LADO, EMOJI_LADO))
            im.save(tmp / f"{k}.png", "PNG")
            hechos += 1
            if k not in base:
                extra.append({"k": k, "n": f"nuevo {e}", "g": "nuevos", "e": e, "p": ["nuevo", "new"]})
        if not hechos:
            raise ValueError("Ninguna imagen del ZIP se pudo abrir.")
        (tmp / "pack.json").write_text(json.dumps({"version": f"{_t.time():.0f}-{hechos}", "n": hechos, "extra": extra,
                                                    "nombre": str(nombre_pack or "")[:80]}, ensure_ascii=False),
                                       encoding="utf-8")
        viejo = final.with_name(final.name + ".viejo")
        _sh.rmtree(viejo, ignore_errors=True)
        final.rename(viejo)
        tmp.rename(final)
        _sh.rmtree(viejo, ignore_errors=True)
    finally:
        _sh.rmtree(tmp, ignore_errors=True)
    return {"n": hechos, "reemplazados": hechos - len(extra), "nuevos": len(extra)}


def borrar_pack_emojis() -> None:
    """Vuelve al pack 3D de Microsoft."""
    import shutil as _sh
    d = _dir_pack()
    _sh.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)


def _ruta_sticker(archivo: str) -> Path | None:
    """Un sticker es un PNG subido (nombre.png) o un emoji del pack ("emoji:<clave>")."""
    a = str(archivo or "")
    if a.startswith("emoji:"):
        p = ruta_emoji(a[6:])
        return p if p and p.is_file() else None
    p = _dir_stickers() / Path(a).name
    return p if p.suffix == ".png" and p.is_file() else None


def _dir_stickers() -> Path:
    return almacen.dir_de("stickers")


def stickers() -> list[dict]:
    d = _dir_stickers()
    return [{"archivo": p.name, "nombre": p.stem, "url": almacen.relativa(p)}
            for p in sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime)]


def subir_sticker(nombre: str, datos: bytes) -> dict:
    """PNG/WebP/JPG/GIF → PNG con transparencia, 512 px como mucho. Para emojis de iPhone: sacales captura o bajalos en PNG."""
    from io import BytesIO
    from PIL import Image, UnidentifiedImageError
    if not datos or len(datos) > 8 * 1024 * 1024:
        raise ValueError("El sticker tiene que ser una imagen de menos de 8 MB.")
    try:
        im = Image.open(BytesIO(datos))
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise ValueError("Eso no es una imagen: subí un PNG (mejor con fondo transparente).") from None
    if im.width * im.height > 40_000_000:
        raise ValueError("La imagen es demasiado grande.")
    im = im.convert("RGBA")
    caja = im.getbbox()                     # sin bordes transparentes de sobra
    if caja:
        im = im.crop(caja)
    im.thumbnail((STICKER_LADO, STICKER_LADO))
    base = re.sub(r"[^0-9A-Za-z_-]+", "-", Path(str(nombre or "sticker")).stem).strip("-")[:40] or "sticker"
    d = _dir_stickers()
    destino = d / f"{base}.png"
    i = 2
    while destino.exists():
        destino = d / f"{base}-{i}.png"
        i += 1
    tmp = destino.with_suffix(".tmp")
    im.save(tmp, "PNG")
    tmp.replace(destino)
    return {"archivo": destino.name, "nombre": destino.stem, "url": almacen.relativa(destino)}


def borrar_sticker(archivo: str) -> None:
    p = _dir_stickers() / Path(str(archivo)).name
    if p.suffix == ".png" and p.is_file():
        p.unlink()


# --- capas del editor: la imagen de cada cosa que se pega (para verla y moverla en la vista previa) -----------------------------
def capa(cid: str, que: str, k: int = 0) -> Path:
    c = _datos(cid)
    d = _dir(cid) / "_capas"
    d.mkdir(exist_ok=True)
    w = int(c.get("ancho") or 720)
    if que == "chat" and isinstance(c.get("chat"), dict) and c["chat"].get("mensajes"):
        ch = c["chat"]
        ancho = max(60, int(w * (_num(ch.get("x1"), 0.82, 0, 1) - _num(ch.get("x0"), 0.18, 0, 1))))
        chat_img.dibujar(ch["mensajes"], ancho, d / "chat.png")
        return d / "chat.png"
    if que == "noti" and isinstance(c.get("noti"), dict) and c["noti"].get("texto"):
        nt = c["noti"]
        ancho = max(60, int(w * (_num(nt.get("x1"), 0.88, 0, 1) - _num(nt.get("x0"), 0.12, 0, 1))))
        chat_img.notificacion(nt["texto"], ancho, d / "noti.png", nt.get("titulo") or "Instagram")
        return d / "noti.png"
    if que == "pegar":
        pg = [b for b in (c.get("pegar") or []) if isinstance(b, dict)]
        if not (0 <= k < len(pg)) or not c.get("ejemplo"):
            raise ValueError("Esa captura ya no está.")
        from PIL import Image
        b, h = pg[k], int(c.get("alto") or 1280)
        dur = float(c.get("duracion") or 1)
        t = _num(b.get("cuadro"), dur * 0.5, 0, max(0.0, dur - 0.1))
        cuadro = d / f"cuadro-{k}.jpg"
        if not medios.cuadro(_dir(cid) / c["ejemplo"], t, cuadro, w):
            raise ValueError("No pude sacar el cuadro del ejemplo.")
        im = Image.open(cuadro).convert("RGB").resize((w, h))
        x0, y0 = int(w * _num(b.get("x0"), 0, 0, 1)), int(h * _num(b.get("y0"), 0, 0, 1))
        x1, y1 = max(x0 + 2, int(w * _num(b.get("x1"), 1, 0, 1))), max(y0 + 2, int(h * _num(b.get("y1"), 1, 0, 1)))
        im.crop((x0, y0, x1, y1)).save(d / f"pegar-{k}.png")
        return d / f"pegar-{k}.png"
    raise ValueError("Esa capa no existe.")


def _nueva(**campos) -> str:
    cid = uuid.uuid4().hex[:8]
    datos = normalizar({"id": cid, "creado": _hora_local(), "estado": "bajando", "error": "",
                        "link": "", "ejemplo": "", "duracion": 0, "ancho": 720, "alto": 1280, "corte": 0, "texto": "",
                        "texto_tam": 0.042, "texto_y": 0.17, "modelo": "", "resultado": "", "leido": False, "notas": "",
                        "cuenta": "", **campos})
    with sesion() as s:
        s.add(Copia(id=cid, cuenta_id=datos["cuenta"], estado=datos["estado"], link=datos["link"], datos=datos))
    _dir(cid).mkdir(parents=True, exist_ok=True)
    return cid


def borrar(cid: str) -> None:
    d = _dir(cid)
    with sesion() as s:
        x = s.get(Copia, cid)
        if x:
            s.delete(x)
        for l in s.scalars(select(LinkBanco).where(LinkBanco.copia_id == cid)):
            l.copia_id = ""   # su link vuelve a "sin usar"
    shutil.rmtree(d, ignore_errors=True)


def cancelar() -> int:
    """Frena lo que todavía no se armó (bajando / esperando a Claude): se borra y su link vuelve a quedar sin usar."""
    fuera = [c["id"] for c in listar() if c.get("estado") in ("bajando", "leyendo")]
    for cid in fuera:
        borrar(cid)
    return len(fuera)


def borrar_version(cid: str, archivo: str) -> None:
    c = _datos(cid)
    vs = [v for v in c["versiones"] if v["archivo"] != archivo]
    (_dir(cid) / Path(str(archivo)).name).unlink(missing_ok=True)
    _datos(cid, versiones=vs, resultado=vs[-1]["archivo"] if vs else "")


def resultado(cid: str, archivo: str = "") -> Path:
    c = _datos(cid)
    nombre = Path(str(archivo)).name if archivo else c.get("resultado")
    if not nombre or not (_dir(cid) / nombre).is_file():
        raise ValueError("Todavía no armaste el video.")
    return _dir(cid) / nombre


def a_bandeja(cid: str, cuenta: str, archivo: str = "") -> str:
    from . import cuentas, piezas
    if not cuentas.existe(cuenta):
        raise ValueError("Elegí una cuenta primero.")
    if _bloqueada_por_marca(_datos(cid)):
        raise ValueError("El remate tiene marca de agua de otra cuenta: subí un remate propio o descartá este meme.")
    pid = piezas.copiar_a_bandeja(cuenta, resultado(cid, archivo), f"copia-{cid}.mp4", copia_id=cid)
    return pid


# --- banco de links ------------------------------------------------------------------------------------------------------------
def _sacar_links(texto) -> list[str]:
    links = list(dict.fromkeys(re.findall(r"https?://[^\s,;|]+", str(texto or ""))))
    if not links:
        raise ValueError("Pegá el link completo (empieza con https://).")
    return links


def _link_dict(l: LinkBanco) -> dict:
    d = {"link": l.link, "agregado": l.agregado.isoformat(timespec="seconds") if l.agregado else "", "copia": l.copia_id,
         "titulo": l.titulo, "cat": l.cat, "voto": l.voto, "motivo": l.motivo, "origen": l.origen, "autor": l.autor,
         **(l.datos or {})}
    if d.get("mini"):
        p = _raiz() / "_minis" / d["mini"]
        d["mini_url"] = almacen.relativa(p) if p.exists() else ""
    return d


def links() -> list[dict]:
    with sesion() as s:
        ls = s.scalars(select(LinkBanco).order_by(LinkBanco.agregado.desc())).all()
    out = [_link_dict(l) for l in ls]
    if any(not x.get("ficha") for x in out):
        encolar("fichas", {}, clave=f"fichas:{int(time.time() // 60)}", max_intentos=1)
    return out


def guardar_links(texto, usados: dict | None = None) -> int:
    """Suma al banco los links nuevos; usados = {link: id de copia}. Acepta renglones "link | título | categoría"."""
    nuevos, usados = _sacar_links(texto), usados or {}
    extra = {}
    for renglon in str(texto or "").splitlines():
        partes = [x.strip() for x in renglon.split("|")]
        m = re.search(r"https?://[^\s,;|]+", partes[0]) if len(partes) > 1 else None
        if m:
            extra[m.group(0)] = {"titulo": partes[1][:120], "cat": (partes[2] if len(partes) > 2 else "")[:30]}
    with sesion() as s:
        for l in nuevos:
            x = s.get(LinkBanco, l[:400])
            if not x:
                x = LinkBanco(link=l[:400], titulo=extra.get(l, {}).get("titulo", ""), cat=extra.get(l, {}).get("cat", ""),
                              datos={})
                s.add(x)
            if usados.get(l):
                x.copia_id = usados[l]
    encolar("fichas", {}, clave=f"fichas:{int(time.time() // 60)}", max_intentos=1)
    return len(nuevos)


@manejador("fichas")
def _fichas(_datos_trabajo: dict) -> None:
    """Título, duración y miniatura de cada link guardado (para verlos en Inspiración). De a uno por vez: si ya hay
    otro trabajo de fichas corriendo, este no hace nada (antes se juntaban varios, repetían los mismos links y
    ocupaban todos los lugares de descargas, así que tampoco se bajaban los ejemplos de Copiar video)."""
    try:
        with candado("fichas", espera=1):
            _hacer_fichas()
    except TimeoutError:
        return


def _opciones_ytdlp() -> dict:
    """Las de las fichas: con tiempo máximo, así un link colgado no frena todo. La sesión «para mirar» (Instagram casi
    nunca da el video sin ella) va aparte, como copia: material.cookies_ytdlp()."""
    return {"quiet": True, "no_warnings": True, "socket_timeout": 20, "retries": 2}


def _hacer_fichas() -> None:
    import urllib.request
    import yt_dlp
    from PIL import Image
    minis = almacen.dir_de("copias", "_minis")
    for _ in range(200):
        with sesion() as s:
            sin = [l for l in s.scalars(select(LinkBanco)) if not (l.datos or {}).get("ficha")]
        if not sin:
            return
        link = sin[0].link
        ficha = {"ficha": "error"}
        try:
            with material.cookies_ytdlp() as ck, \
                    yt_dlp.YoutubeDL({**_opciones_ytdlp(), "skip_download": True, **({"cookiefile": ck} if ck else {})}) as y:
                info = y.extract_info(link, download=False)
            nombre = f"{re.sub(r'[^0-9A-Za-z_-]', '', str(info.get('id') or uuid.uuid4().hex))[:40]}.jpg"
            ficha = {"ficha": "ok", "dur": round(float(info.get("duration") or 0), 1)}
            autor = str(info.get("uploader") or info.get("channel") or "")[:60]
            m = re.search(r"tiktok\.com/@([\w.]{2,40})", link)
            usuario = m.group(1) if m else str((info.get("channel") if "instagram.com" in link else info.get("uploader")) or "")
            if re.fullmatch(r"[\w.]{2,40}", usuario or ""):
                ficha["usuario"] = usuario
            if info.get("title"):
                ficha["titulo_orig"] = str(info["title"])[:160]
            if info.get("thumbnail"):
                req = urllib.request.Request(info["thumbnail"], headers={"User-Agent": "Mozilla/5.0"})
                tmp = minis / (nombre + ".tmp")
                tmp.write_bytes(urllib.request.urlopen(req, timeout=20).read())
                im = Image.open(tmp).convert("RGB")
                im.thumbnail((360, 640))
                im.save(minis / nombre, quality=80)
                tmp.unlink(missing_ok=True)
                ficha["mini"] = nombre
        except Exception as ex:
            ficha["error"], autor = str(ex)[:120], ""
        with sesion() as s:
            x = s.get(LinkBanco, link)
            if x:
                x.datos = {**(x.datos or {}), **ficha}
                if autor and not x.autor:
                    x.autor = autor


def previa(link: str) -> Path:
    """Baja el video del banco para verlo dentro del panel. Queda guardado."""
    with sesion() as s:
        x = s.get(LinkBanco, str(link))
        if not x:
            raise ValueError("Ese link ya no está en el banco.")
        copia = x.copia_id
    if copia:
        try:
            c = _datos(copia)
            if c.get("ejemplo") and (_dir(copia) / c["ejemplo"]).is_file():
                return _dir(copia) / c["ejemplo"]
        except ValueError:
            pass
    carpeta = almacen.dir_de("copias", "_previas")
    nombre = hashlib.md5(link.encode()).hexdigest()[:12]
    # solo el video ya bajado con ese nombre exacto (nunca un temporal que quedó de una descarga cortada)
    ya = next((p for p in (carpeta / (nombre + ext) for ext in sorted(medios.VIDEOS)) if p.is_file()), None)
    if ya:
        return ya
    return material.bajar_video(link, carpeta, nombre)[0]


def agregar_encontrados(items: list[dict]) -> int:
    """Lo que encontró el buscador y aprobó Claude: entra al banco con su miniatura ya hecha. img = ruta absoluta."""
    minis = almacen.dir_de("copias", "_minis")
    n = 0
    with sesion() as s:
        for it in items:
            if s.get(LinkBanco, it["link"]):
                continue
            nombre = hashlib.md5(it["link"].encode()).hexdigest()[:12] + ".jpg"
            try:
                shutil.copyfile(it["img"], minis / nombre)
            except (OSError, KeyError, TypeError):
                nombre = ""
            s.add(LinkBanco(link=it["link"][:400], titulo=str(it.get("titulo", ""))[:200], cat=str(it.get("cat", ""))[:30],
                            origen=it.get("origen", "buscador"), voto=it.get("voto", ""), autor=it.get("autor", ""),
                            datos={"ficha": "ok", "mini": nombre,
                                   **({"video": it["video"]} if material.video_valido(it.get("video")) else {}),
                                   **({"dur": float(it["dur"])} if isinstance(it.get("dur"), (int, float)) and it["dur"] else {})}))
            n += 1
    return n


def poner_autor(link: str, autor: str) -> None:
    with sesion() as s:
        x = s.get(LinkBanco, link)
        if x:
            x.autor = autor


def votar(link: str, voto, motivo="") -> None:
    voto = voto if voto in ("bueno", "malo", "oro") else ""
    with sesion() as s:
        x = s.get(LinkBanco, str(link))
        if x:
            x.voto, x.motivo = voto, str(motivo or "")[:80]


def borrar_link(link: str) -> None:
    with sesion() as s:
        x = s.get(LinkBanco, str(link))
        if x:
            s.delete(x)


def hacer_guardados(n, cuenta: str = "") -> list[str]:
    """Arma n videos con los links guardados que todavía no se usaron (los más viejos primero)."""
    with sesion() as s:
        libres = s.scalars(select(LinkBanco.link).where(LinkBanco.copia_id == "").order_by(LinkBanco.agregado)).all()
    if not libres:
        raise ValueError("No quedan links sin usar. Pegá más arriba.")
    return desde_links("\n".join(libres[:max(1, int(n or 1))]), cuenta)


def desde_links(texto, cuenta: str = "") -> list[str]:
    links_ = _sacar_links(texto)
    if len(links_) > 150:
        raise ValueError("Son muchos: pegá hasta 150 links por vez.")
    ids = []
    for i, link in enumerate(links_):
        cid = _nueva(link=link, cuenta=cuenta, lote_orden=i)
        ids.append(cid)
        encolar("descargar_ejemplo", {"copia": cid, "link": link}, clave=f"descargar_ejemplo:{cid}", max_intentos=2)
    guardar_links(texto, dict(zip(links_, ids)))
    return ids


@manejador("descargar_ejemplo")
def _descargar(datos: dict) -> None:
    cid = datos["copia"]
    try:
        _datos(cid)
    except ValueError:
        return   # la borraron o cancelaron
    if not _dir(cid).is_dir():
        return
    try:
        video, _ = material.bajar_video(datos["link"], _dir(cid), "ejemplo")
    except ValueError as ex:
        try:
            _datos(cid, estado="error", error=str(ex))
        except ValueError:
            shutil.rmtree(_dir(cid), ignore_errors=True)
        return
    try:
        _datos(cid, ejemplo=video.name)
    except ValueError:   # la cancelaron mientras bajaba: no queda nada suelto
        shutil.rmtree(_dir(cid), ignore_errors=True)
        return
    encolar("preparar_copia", {"copia": cid}, clave=f"preparar_copia:{cid}:{int(time.time())}", max_intentos=1)


def desde_archivo(nombre: str, datos: bytes, cuenta: str = "") -> str:
    if not medios.es_video(nombre):
        raise ValueError("Subí un video (mp4 o mov).")
    cid = _nueva(cuenta=cuenta)
    p = _dir(cid) / ("ejemplo" + Path(nombre).suffix.lower())
    p.write_bytes(datos)
    _datos(cid, ejemplo=p.name)
    encolar("preparar_copia", {"copia": cid}, clave=f"preparar_copia:{cid}:{int(time.time())}", max_intentos=1)
    return cid


# --- 1) preparar: hoja de cuadros y pedido de lectura ----------------------------------------------------------------------------
@manejador("preparar_copia")
def _preparar(datos: dict) -> None:
    cid = datos["copia"]
    try:
        c = _datos(cid)
    except ValueError:
        return
    video = _dir(cid) / (c.get("ejemplo") or "")
    try:
        from PIL import Image, ImageDraw
        if not video.is_file():
            raise RuntimeError("no está el video de ejemplo")
        w, h, dur, _ = _info_video(video)
        paso = max(0.5, round(dur / 24 * 2) / 2)
        tiempos = [round(i * paso, 2) for i in range(int(dur / paso) + 1) if i * paso < dur - 0.15]
        tmp = _dir(cid) / "_cuadros"
        tmp.mkdir(exist_ok=True)
        cw, ch = 220, int(220 * h / w) if w else 390
        cols = 8
        hoja = Image.new("RGB", (cw * cols, ch * max(1, (len(tiempos) + cols - 1) // cols)), "white")
        dr = ImageDraw.Draw(hoja)
        for i, t in enumerate(tiempos):
            f = tmp / f"{i}.jpg"
            if not medios.cuadro(video, t, f, 440):
                continue
            im = Image.open(f)
            im.thumbnail((cw, ch))
            x, y = (i % cols) * cw, (i // cols) * ch
            hoja.paste(im, (x, y))
            dr.rectangle((x, y, x + 46, y + 18), fill="black")
            dr.text((x + 4, y + 4), f"{t:g}s", fill="yellow")
        hoja.save(_dir(cid) / "hoja.jpg", quality=82)
        shutil.rmtree(tmp, ignore_errors=True)
        actividad("leyendo", "Claude está mirando un ejemplo nuevo", img=almacen.relativa(video), copia=cid)
        cuenta = c.get("cuenta") or ""
        try:   # siempre se guarda la música del ejemplo: sirve para otros videos
            material.guardar_musica(video, cuenta, f"copia-{cid}")
        except Exception:
            pass
        _datos(cid, estado="leyendo", hoja="hoja.jpg", duracion=round(dur, 2), ancho=w, alto=h,
               corte=round(min(3.0, dur / 2), 1))
        fotos_hoja, fotos = _candidatas(cuenta)
        imagenes = [almacen.relativa(_dir(cid) / "hoja.jpg")]
        usadas_txt = ""
        if fotos_hoja:
            imagenes.append(almacen.relativa(fotos_hoja))
            usadas = _usadas(cuenta)
            usadas_txt = ", ".join(f"{i + 1}×{usadas[r.name]}" for i, (r, _) in enumerate(fotos) if usadas.get(r.name))
        datos_l = {"duracion": dur, "cuenta": cuenta, "hay_fotos": bool(fotos_hoja), "usadas": usadas_txt}
        m = fuentes.muestrario() if fuentes.claude_elige() else None
        if m:                               # la última imagen: cada fuente escrita en sí misma, con su clave
            imagenes.append(almacen.relativa(m[0]))
            datos_l["fuentes"] = [{"clave": x["clave"], "nombre": x["nombre"]} for x in m[1]]
        tareas.crear("lectura", datos_l, referencia=cid, imagenes=imagenes)
    except Exception as ex:
        _datos(cid, estado="error", error=f"No pude leer el video: {str(ex)[:150]}")


def _fotos_de(cuenta: str) -> list[tuple[Path, list]]:
    return [(r, et) for r, et, _, _ in material.material_en(cuenta, material.carpetas_reels(cuenta))]


def _candidatas(cuenta: str):
    """Hoja con las fotos de la modelo numeradas (una por cuenta; se rehace si cambian las fotos)."""
    fotos = [(Path(r), et) for r, et in _fotos_de(cuenta) if medios.es_foto(r) or medios.es_video(r)][:100] if cuenta else []
    if not fotos:
        return None, []
    import json
    firma = "|".join(f"{r.name}:{r.stat().st_mtime_ns}" for r, _ in fotos)
    hoja = _raiz() / f"_fotos_{cuenta}.jpg"
    meta_p = _raiz() / f"_fotos_{cuenta}.json"
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    if meta.get("firma") != firma or not hoja.exists():
        from PIL import Image, ImageDraw
        cw, ch, cols = 150, 188, 10
        img = Image.new("RGB", (cw * cols, ch * ((len(fotos) + cols - 1) // cols)), "white")
        dr = ImageDraw.Draw(img)
        for i, (r, _) in enumerate(fotos):
            try:
                if medios.es_video(r):
                    tmp = _raiz() / f"_cuadro_tmp_{cuenta}.jpg"
                    medios.cuadro(r, 1.0, tmp, 300)
                    im = Image.open(tmp).convert("RGB")
                else:
                    im = medios._abrir_imagen(r)
                im.thumbnail((cw - 4, ch - 4))
            except Exception:
                continue
            x, y = (i % cols) * cw, (i // cols) * ch
            img.paste(im, (x + 2, y + 2))
            dr.rectangle((x, y, x + 30, y + 20), fill="black")
            dr.text((x + 5, y + 4), str(i + 1), fill="yellow")
            if medios.es_video(r):
                dr.rectangle((x + cw - 30, y, x + cw, y + 20), fill="red")
                dr.text((x + cw - 22, y + 4), "V", fill="white")
        img.save(hoja, quality=80)
        meta_p.write_text(json.dumps({"firma": firma, "fotos": [r.name for r, _ in fotos]}), encoding="utf-8")
    return hoja, fotos


def _usadas(cuenta: str) -> dict:
    n = {}
    with sesion() as s:
        for x in s.scalars(select(Copia).where(Copia.cuenta_id == cuenta)):
            o = (x.datos or {}).get("modelo_origen")
            if o:
                n[o] = n.get(o, 0) + 1
    return n


def _limpiar_campos(l: dict, dur) -> dict:
    """Valida lo que manda el panel o lo que leyó Claude. Con cualquier basura: ignora lo que no entiende."""
    if not isinstance(l, dict):
        return {}
    out = {}
    if "noti" in l and not (isinstance(l["noti"], dict) and l["noti"].get("texto")):
        out["noti"] = None
    if "chat" in l and not (isinstance(l["chat"], dict) and l["chat"].get("mensajes")):
        out["chat"] = None
    if isinstance(l.get("noti"), dict) and l["noti"].get("texto"):
        out["noti"] = {"texto": str(l["noti"]["texto"])[:160], "titulo": str(l["noti"].get("titulo") or "Instagram")[:40],
                       **{k: round(_num(l["noti"][k], 0, 0, 1), 4) for k in ("x0", "x1", "y0") if l["noti"].get(k) not in (None, "")}}
    if isinstance(l.get("chat"), dict) and isinstance(l["chat"].get("mensajes"), list):
        ms = [{"de": m.get("de") if m.get("de") in ("yo", "otro", "aviso") else "otro",
               "texto": str(m.get("texto") or "")[:200], "hora": str(m.get("hora") or "")[:8]}
              for m in l["chat"]["mensajes"][:14] if isinstance(m, dict)]
        out["chat"] = {"mensajes": ms, **{k: round(_num(l["chat"][k], 0, 0, 1), 4) for k in ("x0", "x1", "y0")
                                          if l["chat"].get(k) not in (None, "")}, "aparece": 0}
        out["pegar"] = []
    if isinstance(l.get("recorte_remate"), dict):
        out["recorte_remate"] = {k: round(_num(l["recorte_remate"].get(k), 0, 0, 1), 4) for k in ("x0", "y0", "x1", "y1")}
    if isinstance(l.get("textos"), list):
        out["textos"] = [{"texto": str(t.get("texto"))[:80], **{k: round(_num(t.get(k), 0.5, 0, 1), 4) for k in ("x", "y", "tam")
                                                               if t.get(k) not in (None, "")}}
                         for t in l["textos"][:4] if isinstance(t, dict) and t.get("texto")]
    if isinstance(l.get("pegar"), list) and not (out.get("chat") or {}).get("mensajes"):
        out["pegar"] = [{**{k: round(_num(b.get(k), 0, 0, 1), 4) for k in ("x0", "y0", "x1", "y1")},
                         **{k: round(_num(b[k], 0, 0, 600), 2) for k in ("cuadro", "aparece") if b.get(k) not in (None, "")},
                         **({k: round(_num(b[k], 0, -0.5, 1), 4) for k in ("a_x0", "a_y0") if b.get(k) not in (None, "")}),
                         **({"a_ancho": round(_num(b["a_ancho"], 0.5, 0.05, 1.5), 4)} if b.get("a_ancho") not in (None, "") else {})}
                        for b in l["pegar"][:3] if isinstance(b, dict)]
    if l.get("texto") is not None:
        out["texto"] = str(l["texto"]).replace("\\n", "\n")[:300]
    if isinstance(l.get("estilo"), str) and l["estilo"] in ESTILOS:
        out["estilo"] = l["estilo"]
    if isinstance(l.get("color"), str) and l["color"] in COLORES:
        out["color"] = l["color"]
    if isinstance(l.get("stickers"), list):
        hay = {x["archivo"] for x in stickers()} | {f"emoji:{k}" for k in _indice_emojis()}
        out["stickers"] = [{"archivo": str(s["archivo"]), "x0": round(_num(s.get("x0"), 0.4, -0.5, 1), 4),
                            "y0": round(_num(s.get("y0"), 0.4, -0.5, 1), 4),
                            "ancho": round(_num(s.get("ancho"), 0.2, 0.03, 1.5), 4)}
                           for s in l["stickers"][:MAX_STICKERS] if isinstance(s, dict) and isinstance(s.get("archivo"), str)
                           and s["archivo"] in hay]
    if isinstance(l.get("fuente"), str) and l["fuente"] in fuentes.claves_disponibles():
        out["fuente"] = l["fuente"]
    l = {k: v for k, v in (l or {}).items() if isinstance(k, str)}
    txt = lambda v: v if isinstance(v, str) else None   # para buscar en dicts sin TypeError con listas
    if txt(l.get("texto_pos")) in VIEJAS_POS and l.get("texto_y") is None:
        out["texto_y"] = VIEJAS_POS[l["texto_pos"]]
    if txt(l.get("texto_tam")) in VIEJOS_TAM:
        out["texto_tam"] = VIEJOS_TAM[l["texto_tam"]]
    elif l.get("texto_tam") not in (None, ""):
        out["texto_tam"] = round(_num(l["texto_tam"], 0.042, 0.02, 0.1), 4)
    for k, lo, hi in (("texto_y", 0.0, 0.97), ("texto_x", 0.05, 0.95)):
        if l.get(k) not in (None, ""):
            out[k] = round(_num(l[k], 0.5, lo, hi), 4)
    if isinstance(l.get("ubicacion_auto"), bool):
        out["ubicacion_auto"] = l["ubicacion_auto"]
    if txt(l.get("audio")) in AUDIOS:
        out["audio"] = l["audio"]
    if l.get("audio_desde") not in (None, ""):
        out["audio_desde"] = round(_num(l["audio_desde"], 0, 0, 3600), 2)
    if l.get("corte") not in (None, ""):
        try:
            out["corte"] = round(max(0.3, min(float(str(l["corte"]).replace(",", ".")), dur or 60)), 2)
        except (TypeError, ValueError):
            raise ValueError("Los segundos de tu modelo tienen que ser un número (ej.: 3 o 2,5).")
    return out


@tareas.al_terminar("lectura")
def _leido(t, l: dict) -> None:
    cid = t.referencia
    try:
        c = _datos(cid)
    except ValueError:
        return
    if l.get("descartar"):   # en el ejemplo sale alguien que puede ser menor: no se usa
        _datos(cid, estado="error", leido=True, error="Descartado: en el ejemplo sale alguien que puede ser menor de edad.")
        actividad("aviso", "Ejemplo descartado: alguien puede ser menor de edad", copia=cid)
        return
    if not fuentes.claude_elige():          # Claude elige la letra solo si está prendido en Fuentes
        l = {k: v for k, v in l.items() if k != "fuente"}
    cambios = _limpiar_campos({k: v for k, v in l.items() if v is not None}, c.get("duracion"))
    cambios["y_ejemplo"] = cambios.get("texto_y", c.get("texto_y"))
    marca = bool(l.get("marca_agua"))
    cambios.update(leido=True, notas=str(l.get("notas") or "")[:300], marca_agua=marca,
                   estado="lista" if c.get("estado") == "leyendo" else c.get("estado"))
    _datos(cid, **cambios)
    if marca and not c.get("remate"):
        actividad("aviso", "El remate de este meme tiene marca de agua: descartalo o subí un remate propio", copia=cid)
    if l.get("modelo") not in (None, "") and c.get("cuenta"):
        _elegir_foto(cid, c["cuenta"], l["modelo"])
    _auto(cid)


@tareas.al_fallar("lectura")
def _lectura_fallo(t, motivo: str) -> None:
    from ..ia.motor_api import es_rechazo
    try:
        if es_rechazo(motivo):   # Claude se negó (suele ser porque no puede asegurar la edad): se descarta, no se insiste
            _datos(t.referencia, estado="error", leido=True,
                   error="Descartado: Claude no aceptó este ejemplo (suele pasar cuando no puede asegurar que quien "
                         "aparece sea mayor de edad). Probá con otro.")
            actividad("aviso", "Ejemplo descartado: Claude no pudo asegurar la edad de quien aparece", copia=t.referencia)
            return
        _datos(t.referencia, estado="error", error=f"Claude no pudo leer el ejemplo: {motivo[:150]}")
    except ValueError:
        pass


def _elegir_foto(cid: str, cuenta: str, cual) -> None:
    _, fotos = _candidatas(cuenta)
    r = None
    if str(cual).strip().isdigit() and 1 <= int(cual) <= len(fotos):
        r = fotos[int(cual) - 1][0]
    else:
        r = next((f for f, _ in fotos if f.name == str(cual)), None)
    if r and r.is_file():
        subir_modelo(cid, r.name, r.read_bytes(), auto=False)


def _bloqueada_por_marca(c: dict) -> bool:
    """Marca de agua de otra cuenta en el remate y sin remate propio (que exista de verdad): no se arma sola. Si la
    dueña decide usarlo igual («Usarlo igual»: marca_ok), se arma y pasa por el juez como cualquier otro."""
    if c.get("marca_ok"):
        return False
    return bool(c.get("marca_agua")) and not (c.get("remate") and (_dir(c["id"]) / c["remate"]).is_file())


def usar_con_marca(cid: str) -> None:
    """La dueña decide usar el remate con la marca de agua de otra cuenta (Instagram puede darle menos alcance)."""
    c = _datos(cid)
    if not c.get("marca_agua"):
        raise ValueError("Este meme no tiene marca de agua.")
    _datos(cid, marca_ok=True)
    if c.get("ejemplo") and c.get("modelo"):
        armar(cid, {}, juzgar=True)


def _auto(cid: str) -> None:
    """Si ya está todo (ejemplo leído + modelo, y sin marca de agua pendiente), arma el video solo."""
    try:
        c = _datos(cid)
        if c.get("leido") and c.get("modelo") and c.get("ejemplo") and c.get("estado") == "lista" \
                and not _bloqueada_por_marca(c):
            armar(cid, {}, juzgar=True)
    except ValueError:
        pass


# --- 2) la modelo, el audio y el remate propio ------------------------------------------------------------------------------------
def subir_modelo(cid: str, nombre: str, datos: bytes, auto: bool = True) -> str:
    if not (medios.es_foto(nombre) or medios.es_video(nombre)):
        raise ValueError("Subí una foto o un video de la modelo.")
    d = _dir(cid)
    for viejo in d.glob("modelo*.*"):
        viejo.unlink(missing_ok=True)
    p = d / f"modelo-{int(time.time() * 1000)}{Path(nombre).suffix.lower()}"
    p.write_bytes(datos)
    _datos(cid, modelo=p.name, modelo_origen=Path(nombre).name)
    if auto:
        _auto(cid)
    return p.name


def modelo_de(cid: str, ruta: Path) -> str:
    src = Path(ruta)
    if not src.is_file():
        raise ValueError("Ese archivo ya no está.")
    return subir_modelo(cid, src.name, src.read_bytes())


def subir_audio(cid: str, nombre: str, datos: bytes) -> str:
    ext = Path(nombre).suffix.lower()
    if ext not in material.AUDIOS and not medios.es_video(nombre):
        raise ValueError("Subí un audio (mp3, m4a, wav) o un video del que se usa el sonido.")
    d = _dir(cid)
    for viejo in d.glob("audio*.*"):
        viejo.unlink(missing_ok=True)
    p = d / f"audio-{int(time.time() * 1000)}{ext}"
    p.write_bytes(datos)
    _datos(cid, audio="musica", audio_archivo=p.name, audio_nombre=Path(nombre).stem[:80], audio_desde=0)
    return p.name


def audio_de(cid: str, ruta: Path, titulo: str = "") -> str:
    src = Path(ruta)
    if not src.is_file():
        raise ValueError("Ese audio ya no está.")
    n = subir_audio(cid, src.name, src.read_bytes())
    if titulo:
        _datos(cid, audio_nombre=str(titulo)[:80])
    return n


def subir_remate(cid: str, nombre: str, datos: bytes) -> str:
    """Un clip propio que reemplaza el remate del ejemplo (para memes cuyo remate tiene marca de agua)."""
    if not medios.es_video(nombre):
        raise ValueError("El remate tiene que ser un video (mp4 o mov).")
    d = _dir(cid)
    viejos = list(d.glob("remate-*.*"))
    p = d / f"remate-{int(time.time() * 1000)}{Path(nombre).suffix.lower()}"
    p.write_bytes(datos)
    _datos(cid, remate=p.name)            # primero el nuevo y después se borran los viejos (un render en curso no cae
    for viejo in viejos:                  # nunca en el remate del ejemplo)
        viejo.unlink(missing_ok=True)
    actividad("remate", "Remate propio cargado: ya se puede armar", copia=cid)
    _auto(cid)
    return p.name


# --- ubicación automática del texto --------------------------------------------------------------------------------------------------
def _caras(cid: str, c: dict) -> list:
    d = _dir(cid)
    modelo = d / c["modelo"]
    clave = f"{c['modelo']}:{modelo.stat().st_mtime_ns}:{c.get('ancho')}x{c.get('alto')}:{c.get('corte')}"
    if c.get("caras_clave") == clave:
        return c.get("caras") or []
    from PIL import Image, ImageOps
    w, h = int(c.get("ancho") or 720), int(c.get("alto") or 1280)
    ww, hh = 480, max(2, round(480 * h / w))
    imgs = []
    if medios.es_foto(modelo):
        imgs.append(medios._abrir_imagen(modelo))
    else:
        corte = float(c.get("corte") or 2)
        for k, t in enumerate(sorted({0.15, corte / 2, max(0.2, corte - 0.25)})):
            f = d / f"_cara{k}.jpg"
            if medios.cuadro(modelo, t, f, 720):
                im = Image.open(f).convert("RGB")
                im.load()
                imgs.append(im)
                f.unlink(missing_ok=True)
    caras = []
    try:
        import cv2
        import numpy as np
        frente = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        perfil = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        for im in imgs:
            g = cv2.equalizeHist(cv2.cvtColor(np.array(ImageOps.fit(im, (ww, hh))), cv2.COLOR_RGB2GRAY))
            minimo = (int(ww * 0.09), int(ww * 0.09))
            cajas = list(frente.detectMultiScale(g, 1.1, 5, minSize=minimo))
            cajas += list(perfil.detectMultiScale(g, 1.1, 5, minSize=minimo))
            cajas += [(ww - x - bw, y, bw, bh) for x, y, bw, bh in perfil.detectMultiScale(cv2.flip(g, 1), 1.1, 5, minSize=minimo)]
            for x, y, bw, bh in cajas:
                x, y, bw, bh = (float(v) for v in (x, y, bw, bh))
                caras.append([round(max(0, (x - bw * 0.12) / ww), 4), round(max(0, (y - bh * 0.3) / hh), 4),
                              round(min(1, (x + bw * 1.12) / ww), 4), round(min(1, (y + bh * 1.35) / hh), 4)])
    except Exception:
        caras = []
    caras = _juntar(caras)
    _datos(cid, caras=caras, caras_clave=clave)
    return caras


def ubicar(cid: str, campos: dict | None = None) -> dict:
    """La altura (y si hace falta letra más chica) donde el texto no tapa la cara ni lo que tapa Instagram."""
    c = _datos(cid)
    if not c.get("modelo"):
        return {"caras": []}
    c = {**c, **_limpiar_campos(campos or {}, c.get("duracion"))}
    caras = _caras(cid, c)
    w, h = int(c.get("ancho") or 720), int(c.get("alto") or 1280)
    fuente_p = fuentes.ruta_obligatoria(c["fuente"])
    from PIL import ImageFont
    caja = c["estilo"] == "caja"
    preferida = float(c.get("y_ejemplo") if c.get("y_ejemplo") is not None else 0.17)
    tam = float(c["texto_tam"])
    mejor = None
    for _ in range(10):
        px = max(8, int(h * tam * ESCALA_TEXTO))
        lineas = _renglones(c.get("texto"), fuente_p, px, w * ANCHO_MAX) or [""]
        try:
            ancho = max(ImageFont.truetype(str(fuente_p), px).getlength(l) for l in lineas) / w
        except Exception:
            ancho = ANCHO_MAX
        x0, x1 = 0.5 - ancho / 2 - 0.02, 0.5 + ancho / 2 + 0.02
        alto = len(lineas) * tam * INTERLINEA + (len(lineas) + 1) * tam * CAJA_PAD * (1 if caja else 0.3)
        y = IG_ARRIBA + 0.01
        while y + alto <= IG_ABAJO - 0.01:
            solape = sum(max(0, min(x1, b[2]) - max(x0, b[0])) * max(0, min(y + alto, b[3]) - max(y, b[1])) for b in caras)
            puntaje = solape * 1000 + abs(y - preferida)
            if mejor is None or puntaje < mejor[0]:
                mejor = (puntaje, round(float(y), 4), round(float(tam), 4), float(solape))
            y += 0.005
        if mejor and mejor[3] == 0:
            break
        tam = max(0.026, tam * 0.9)
    if not mejor:
        return {"caras": caras}
    return {"texto_y": mejor[1], "texto_x": 0.5, "texto_tam": mejor[2], "tapa_cara": bool(mejor[3] > 0), "caras": caras}


# --- 3) armar --------------------------------------------------------------------------------------------------------------------------
def armar(cid: str, campos: dict, juzgar: bool = False) -> None:
    """juzgar: lo armó la fábrica sola, así que pasa por el juez (si lo ajusta ella a mano, no)."""
    c = _datos(cid)
    if not c.get("ejemplo"):
        raise ValueError("Todavía no está el video de ejemplo.")
    if not c.get("modelo"):
        raise ValueError("Subí primero la foto o el video de tu modelo.")
    if _bloqueada_por_marca(c):
        raise ValueError("El remate tiene marca de agua: subí un remate propio o descartá este meme.")
    cambios = _limpiar_campos(campos or {}, c.get("duracion"))
    if cambios.get("ubicacion_auto", c.get("ubicacion_auto")) and not c.get("pegar") and not c.get("chat") and not c.get("noti"):
        u = ubicar(cid, {**(campos or {}), **cambios})
        for k in ("texto_y", "texto_x", "texto_tam"):
            if k in u:
                cambios[k] = u[k]
    if cambios.get("audio", c.get("audio")) == "musica" and not c.get("audio_archivo"):
        raise ValueError("Elegí o subí la música primero (o cambiá el audio).")
    if not juzgar:
        cambios["juez"] = {}
    n = int(c.get("render_n") or 0) + 1
    _datos(cid, estado="armando", error="", juzgar=juzgar, render_n=n, **cambios)
    encolar("render", {"copia": cid, "n": n}, clave=f"render:{cid}:{n}", max_intentos=1)


def _afinar(ejemplo: Path, b: dict, w: int, h: int, dur: float, tmp: Path) -> dict:
    """Recorte exacto de la captura: compara un cuadro con la captura y uno de antes de que aparezca."""
    aparece = _num(b.get("aparece"), 0, 0, dur)
    if aparece < 0.15:
        return b
    try:
        import numpy as np
        from PIL import Image
        tmp.mkdir(exist_ok=True)
        t = _num(b.get("cuadro"), dur * 0.5, 0, max(0.0, dur - 0.1))
        if not (medios.cuadro(ejemplo, max(0.0, aparece - 0.15), tmp / "a.png", w) and medios.cuadro(ejemplo, t, tmp / "b.png", w)):
            return b
        a = np.asarray(Image.open(tmp / "a.png").convert("L"), dtype=np.int16)
        c = np.asarray(Image.open(tmp / "b.png").convert("L"), dtype=np.int16)
        H, W = a.shape
        x0, x1 = max(0, int(W * (_num(b.get("x0"), 0, 0, 1) - 0.04))), min(W, int(W * (_num(b.get("x1"), 1, 0, 1) + 0.04)))
        y0, y1 = max(0, int(H * (_num(b.get("y0"), 0, 0, 1) - 0.04))), min(H, int(H * (_num(b.get("y1"), 1, 0, 1) + 0.04)))
        m = np.abs(c[y0:y1, x0:x1] - a[y0:y1, x0:x1]) > 22
        filas, cols = np.where(m.mean(axis=1) > 0.35)[0], np.where(m.mean(axis=0) > 0.35)[0]
        if len(filas) < 10 or len(cols) < 10:
            return b
        borde = 2
        nx0, nx1 = (x0 + cols[0] + borde) / W, (x0 + cols[-1] + 1 - borde) / W
        ny0, ny1 = (y0 + filas[0] + borde) / H, (y0 + filas[-1] + 1 - borde) / H
        if nx1 - nx0 < 0.05 or ny1 - ny0 < 0.03:
            return b
        return {**b, "x0": round(nx0, 4), "x1": round(nx1, 4), "y0": round(ny0, 4), "y1": round(ny1, 4)}
    except Exception:
        return b


def _fondo_claro(cid: str, modelo: Path, c: dict, w: int, h: int, corte: float) -> bool:
    """¿El fondo justo detrás del texto es claro? (blanco sobre blanco no se lee)."""
    try:
        from PIL import Image
        if medios.es_foto(modelo):
            im = medios._abrir_imagen(modelo).convert("L")
        else:
            tmp = _dir(cid) / "_fondo.jpg"
            if not medios.cuadro(modelo, max(0.0, corte / 2), tmp, w):
                return False
            im = Image.open(tmp).convert("L")
        r = max(w / im.width, h / im.height)
        im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1))
        x0, y0 = (im.width - w) // 2, (im.height - h) // 2
        im = im.crop((x0, y0, x0 + w, y0 + h))
        tam = h * float(c["texto_tam"]) * ESCALA_TEXTO
        n = max(1, len(str(c.get("texto") or "").split("\n")) + len(str(c.get("texto") or "")) // 28)
        y = int(h * float(c["texto_y"]))
        caja = im.crop((int(w * 0.14), y, int(w * 0.86), min(h, y + int(tam * INTERLINEA * n) + 4)))
        px = list(caja.convert("L").tobytes())
        return sum(1 for v in px if v > 170) / max(1, len(px)) > 0.25
    except Exception:
        return False


def _texto_filtros(c: dict, d: Path, w: int, h: int) -> list[str]:
    """Los drawtext del texto. Los emojis del texto quedan como espacios y van en c["_emojis"] = [(clave, x, y, lado)]
    para pegarlos como imagen encima, en el mismo lugar."""
    from PIL import ImageFont
    tam = int(h * float(c["texto_tam"]) * ESCALA_TEXTO)
    fuente_p = fuentes.ruta_obligatoria(c["fuente"])
    lineas = _renglones(c.get("texto"), fuente_p, tam, w * ANCHO_MAX)
    _, mapa = emojis_del_texto(c.get("texto"))
    try:
        medir = ImageFont.truetype(str(fuente_p), tam).getlength
    except Exception:
        medir = lambda s: len(s) * tam * 0.55
    n_esp, esp = _espacios_emoji(medir, tam)
    lado = max(4, int(round(tam * 1.05)))
    c["_emojis"] = []
    color = COLORES.get(c["color"], "ffffff")
    oscuro = color == "111111"
    estilo = c["estilo"]
    pad = int(tam * CAJA_PAD)
    alto_linea = int(tam * INTERLINEA) + (pad if estilo == "caja" else 0)
    y0, cx = int(h * float(c["texto_y"])), float(c["texto_x"])
    f_esc = _esc_ff(fuente_p)
    filtros = []
    for i, linea in enumerate(lineas):
        if not linea:
            continue
        base_y = y0 + i * alto_linea + (pad if estilo == 'caja' else 0) + int(tam * 0.95)
        if any(ch in mapa for ch in linea):
            plano = "".join(" " * n_esp if ch in mapa else ch for ch in linea)
            tw = medir(plano)
            lx = int(max(pad + 4, min(w * cx - tw / 2, w - tw - pad - 4)))
            for j, ch in enumerate(linea):
                if ch in mapa:
                    antes = medir("".join(" " * n_esp if x in mapa else x for x in linea[:j]))
                    c["_emojis"].append((mapa[ch], int(lx + antes + (n_esp * esp - lado) / 2),
                                         int(base_y - lado * 0.86), lado))
            (d / f"_t{i}.txt").write_text(plano, encoding="utf-8")
            pos_x = f"{lx}"
        else:
            (d / f"_t{i}.txt").write_text(linea, encoding="utf-8")
            pos_x = f"'max({pad + 4},min(w*{cx:.4f}-tw/2,w-tw-{pad + 4}))'"
        if not linea.strip(" "):
            filtros.append("null")
            continue
        f = (f"drawtext=fontfile='{f_esc}':textfile=_t{i}.txt:expansion=none:fontcolor=0x{color}:fontsize={tam}:"
             f"x={pos_x}:y={base_y}-ascent")
        if c.get("_sombra") and estilo == "nada":
            f += f":shadowcolor=black@0.35:shadowx=0:shadowy={max(1, tam // 30)}"
        if estilo == "sombra":
            f += f":shadowcolor={'white@0.55' if oscuro else 'black@0.75'}:shadowx=0:shadowy={max(2, tam // 18)}"
        elif estilo == "caja":
            f += f":box=1:boxcolor={'white@0.95' if oscuro else 'black@0.85'}:boxborderw={pad}"
        elif estilo == "contorno":
            f += f":borderw={max(2, tam // 14)}:bordercolor={'white' if oscuro else 'black'}"
        filtros.append(f)
    return filtros


@manejador("render")
def _render(datos: dict) -> None:
    """Un render por copia a la vez (candado). Si mientras esperaba se pidió otro armado, este queda viejo y no corre."""
    try:
        with candado(f"render-{datos['copia']}", espera=3600):
            _render_ya(datos)
    except ValueError:
        return


def _vigente(cid: str, n) -> bool:
    try:
        c = _datos(cid)
    except ValueError:
        return False
    return n is None or int(c.get("render_n") or 0) == int(n)


def _render_ya(datos: dict) -> None:
    cid, n = datos["copia"], datos.get("n")
    try:
        c = _datos(cid)
    except ValueError:
        return
    if c.get("estado") != "armando" or not _vigente(cid, n):
        return
    d = _dir(cid)
    actividad("editando", f"Editando el reel «{(c.get('texto') or '').splitlines()[0][:60] if c.get('texto') else cid}»",
              img=almacen.relativa(d / c["modelo"]) if c.get("modelo") else "", copia=cid, cuenta=c.get("cuenta", ""))
    ejemplo, modelo = d / c["ejemplo"], d / c["modelo"]
    try:
        w, h, dur, audio_ej = _info_video(ejemplo)
        corte = min(float(c["corte"]), dur)
        es_foto = medios.es_foto(modelo)
        info_mod = None if es_foto else _info_video(modelo)
        audio_mod = bool(info_mod and info_mod[3])
        entradas = ["-i", str(ejemplo), "-i", str(modelo)]
        if info_mod and info_mod[2] < corte - 0.1:
            # el video de la modelo es más corto que su parte: ida y vuelta en loop, nunca congelado
            pp = d / "_idayvuelta.mp4"
            r = subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-i", str(modelo), "-filter_complex",
                                f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,"
                                "split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-an",
                                "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p", str(pp)],
                               capture_output=True, text=True, errors="ignore", timeout=300)
            if not r.returncode and pp.is_file():
                entradas = ["-i", str(ejemplo), "-stream_loop", "-1", "-i", str(pp)]
                audio_mod = False
        if es_foto:   # la foto queda fija, sin zoom ni efectos
            base = (f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                    f"loop=loop=-1:size=1:start=0,setpts=N/30/TB,fps=30,trim=0:{corte:.2f},setpts=PTS-STARTPTS")
        else:
            base = (f"[1:v]trim=0:{corte:.2f},setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},fps=30,setsar=1,tpad=stop_mode=clone:stop_duration={corte:.2f},trim=0:{corte:.2f}")
        # capturas reales del ejemplo: se recortan y se pegan fijas, desde el primer cuadro
        pegar = [_afinar(ejemplo, b, w, h, dur, d / "_afinar") for b in (c.get("pegar") or []) if isinstance(b, dict)]
        shutil.rmtree(d / "_afinar", ignore_errors=True)
        if pegar:
            base += "[m0];"
            for k, b in enumerate(pegar):
                x0, y0 = int(w * _num(b.get("x0"), 0, 0, 1)), int(h * _num(b.get("y0"), 0, 0, 1))
                cw_, ch_ = max(2, int(w * _num(b.get("x1"), 1, 0, 1)) - x0), max(2, int(h * _num(b.get("y1"), 1, 0, 1)) - y0)
                t = _num(b.get("cuadro"), min(dur * 0.5, max(0.0, corte - 0.3)), 0, max(0.0, dur - 0.1))
                # dónde y de qué tamaño se pega (lo que movieron en el editor); si no, en el mismo lugar del ejemplo
                ax = int(w * _num(b.get("a_x0"), x0 / w, -0.5, 1))
                ay = int(h * _num(b.get("a_y0"), y0 / h, -0.5, 1))
                aw = max(2, int(w * _num(b.get("a_ancho"), cw_ / w, 0.02, 1.5))) // 2 * 2
                base += (f"[0:v]trim=start={t:.2f},setpts=PTS-STARTPTS,scale={w}:{h},fps=30,setsar=1,"
                         f"crop={cw_}:{ch_}:{x0}:{y0},scale={aw}:-2,trim=end_frame=1,loop=loop=-1:size=1:start=0,"
                         f"setpts=N/30/TB,trim=0:{corte:.2f}[p{k}];"
                         f"[m{k}][p{k}]overlay={ax}:{ay}[m{k + 1}];")
            base += f"[m{len(pegar)}]null"
        nt = c.get("noti") if isinstance(c.get("noti"), dict) else None
        if nt and nt.get("texto"):
            nx0, nx1 = _num(nt.get("x0"), 0.12, 0, 1), _num(nt.get("x1"), 0.88, 0, 1)
            chat_img.notificacion(nt["texto"], max(60, int(w * (nx1 - nx0))), d / "_noti.png", nt.get("titulo") or "Instagram")
            ni = entradas.count("-i")
            entradas += ["-loop", "1", "-i", str(d / "_noti.png")]
            base += (f"[nb];[{ni}:v]format=rgba,trim=0:{corte:.2f},setpts=PTS-STARTPTS[noti];"
                     f"[nb][noti]overlay={int(w * nx0)}:{int(h * _num(nt.get('y0'), 0.25, 0, 1))},null")
        ch = c.get("chat") if isinstance(c.get("chat"), dict) else None
        if ch and ch.get("mensajes"):
            cx0, cx1 = _num(ch.get("x0"), 0.18, 0, 1), _num(ch.get("x1"), 0.82, 0, 1)
            cy0 = int(h * _num(ch.get("y0"), 0.6, 0, 1))
            chat_img.dibujar(ch["mensajes"], max(60, int(w * (cx1 - cx0))), d / "_chat.png")
            ni = entradas.count("-i")
            entradas += ["-loop", "1", "-i", str(d / "_chat.png")]
            base += (f"[cb];[{ni}:v]format=rgba,trim=0:{corte:.2f},setpts=PTS-STARTPTS[chat];"
                     f"[cb][chat]overlay={int(w * cx0)}:{cy0},null")
        for k, st in enumerate(c.get("stickers") or []):      # emojis del pack o PNG subidos (con transparencia)
            sp = _ruta_sticker(st.get("archivo"))
            if not sp:
                continue
            if sp.suffix == ".webp":                          # a PNG: ffmpeg lee seguro el canal alfa
                tmp = d / "_capas"
                tmp.mkdir(exist_ok=True)
                png = tmp / f"st{k}.png"
                from PIL import Image
                Image.open(sp).convert("RGBA").save(png)
                sp = png
            sw = max(4, int(w * _num(st.get("ancho"), 0.2, 0.03, 1.5))) // 2 * 2
            ni = entradas.count("-i")
            entradas += ["-loop", "1", "-i", str(sp)]
            base += (f"[sb{k}];[{ni}:v]format=rgba,scale={sw}:-2,trim=0:{corte:.2f},setpts=PTS-STARTPTS[st{k}];"
                     f"[sb{k}][st{k}]overlay={int(w * _num(st.get('x0'), 0.4, -0.5, 1))}:"
                     f"{int(h * _num(st.get('y0'), 0.4, -0.5, 1))},null")
        p_cl = fuentes.ruta_obligatoria("clasica")
        f_cl = _esc_ff(p_cl)
        for k, tx in enumerate(t for t in (c.get("textos") or []) if isinstance(t, dict) and t.get("texto")):
            (d / f"_x{k}.txt").write_text(fuentes.dibujable(str(tx["texto"]), p_cl), encoding="utf-8")
            tz = int(h * _num(tx.get("tam"), 0.03, 0.015, 0.08) * ESCALA_TEXTO)
            base += (f",drawtext=fontfile='{f_cl}':textfile=_x{k}.txt:expansion=none:fontcolor=white:fontsize={tz}:"
                     f"x='w*{_num(tx.get('x'), 0.5, 0, 1):.4f}-tw/2':y={int(h * _num(tx.get('y'), 0.1, 0, 1)) + int(tz * 0.95)}-ascent")
        c["_sombra"] = _fondo_claro(cid, modelo, c, w, h, corte)
        for f in _texto_filtros(c, d, w, h):
            base += "," + f
        for k, (clave, ex, ey, lado) in enumerate(c.get("_emojis") or []):   # los emojis del texto, en su lugar
            ep = ruta_emoji(clave)
            if not ep:
                continue
            (d / "_capas").mkdir(exist_ok=True)
            png = d / "_capas" / f"te{k}.png"
            from PIL import Image as ImagenPIL
            ImagenPIL.open(ep).convert("RGBA").resize((lado, lado), ImagenPIL.LANCZOS).save(png)
            ni = entradas.count("-i")
            entradas += ["-loop", "1", "-i", str(png)]
            base += (f"[tb{k}];[{ni}:v]format=rgba,trim=0:{corte:.2f},setpts=PTS-STARTPTS[te{k}];"
                     f"[tb{k}][te{k}]overlay={ex}:{ey},null")
        # el remate: el del ejemplo (sin tocar sus marcas: si las tiene, la copia no llega acá) o uno propio
        remate_p = d / c["remate"] if c.get("remate") and (d / c["remate"]).is_file() else None
        if c.get("marca_agua") and not remate_p and not c.get("marca_ok"):
            raise RuntimeError("el remate tiene marca de agua y no hay remate propio")
        if remate_p:
            ri = entradas.count("-i")
            entradas += ["-i", str(remate_p)]
            dur_rem = _info_video(remate_p)[2] or 3
            total = corte + dur_rem
            remate = (f"[{ri}:v]setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1")
            sigue = True
        else:
            total = dur
            sigue = dur - corte > 0.2
            remate = f"[0:v]trim=start={corte:.2f},setpts=PTS-STARTPTS,scale={w}:{h},setsar=1"
            rr = c.get("recorte_remate") if isinstance(c.get("recorte_remate"), dict) else None
            if rr:   # sacar bandas negras del remate y que llene la pantalla
                rx0, ry0 = int(w * _num(rr.get("x0"), 0, 0, 1)) // 2 * 2, int(h * _num(rr.get("y0"), 0, 0, 1)) // 2 * 2
                rw = max(16, int(w * (_num(rr.get("x1"), 1, 0, 1) - _num(rr.get("x0"), 0, 0, 1)))) // 2 * 2
                rh = max(16, int(h * (_num(rr.get("y1"), 1, 0, 1) - _num(rr.get("y0"), 0, 0, 1)))) // 2 * 2
                remate += f",crop={rw}:{rh}:{rx0}:{ry0},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
        remate += ",fps=30,setsar=1"
        filtro = base + (f"[a];{remate}[b];[a][b]concat=n=2:v=1:a=0[v]" if sigue else "[v]")
        # audio
        fmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
        mapa_audio = []
        modo = c["audio"]
        if modo == "modelo" and not audio_mod:
            modo = "ejemplo"
        if modo == "ejemplo" and audio_ej:
            filtro += f";[0:a]{fmt},apad[aout]"
            mapa_audio = ["-map", "[aout]"]
        elif modo == "musica" and c.get("audio_archivo") and (d / c["audio_archivo"]).is_file():
            na = entradas.count("-i")
            entradas += ["-ss", f"{float(c.get('audio_desde') or 0):.2f}", "-i", str(d / c["audio_archivo"])]
            filtro += f";[{na}:a]{fmt},apad[aout]"
            mapa_audio = ["-map", "[aout]"]
        elif modo == "modelo":
            filtro += f";[1:a]atrim=0:{corte:.2f},asetpts=PTS-STARTPTS,{fmt},apad=whole_dur={corte:.2f}[ma]"
            if sigue and audio_ej and not remate_p:
                filtro += f";[0:a]atrim=start={corte:.2f},asetpts=PTS-STARTPTS,{fmt}[ea];[ma][ea]concat=n=2:v=0:a=1[aout]"
            else:
                filtro += ";[ma]apad[aout]"
            mapa_audio = ["-map", "[aout]"]
        salida = d / f"resultado-{int(time.time())}.mp4"
        cmd = [medios.ffmpeg(), "-y", "-v", "error", *entradas, "-filter_complex", filtro, "-map", "[v]", *mapa_audio]
        if mapa_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k"]
        cmd += ["-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-t", f"{total:.2f}",
                "-movflags", "+faststart", str(salida)]
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, errors="ignore", timeout=900)
        for t in list(d.glob("_t*.txt")) + list(d.glob("_x*.txt")):
            t.unlink(missing_ok=True)
        if r.returncode or not salida.is_file():
            raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg falló")
        actual = _datos(cid)
        vs = (actual.get("versiones") or []) + [{"archivo": salida.name, "armado": _hora_local(),
                                                  "texto": (c.get("texto") or "")[:120]}]
        for v in vs[:-MAX_VERSIONES]:
            (d / v["archivo"]).unlink(missing_ok=True)
        vigente = _vigente(cid, n)   # si pidieron otro armado mientras tanto, este queda en el historial y nada más
        _datos(cid, versiones=vs[-MAX_VERSIONES:], **({"estado": "lista", "resultado": salida.name,
                                                       "armado": vs[-1]["armado"], "duracion_final": round(total, 2)}
                                                      if vigente else {}))
    except Exception as ex:
        if _vigente(cid, n):
            _datos(cid, estado="lista", error=f"No se pudo armar: {str(ex)[:180]}")
        return
    if not vigente:
        return
    actividad("listo", "Reel armado: pasa al juez" if c.get("juzgar") else "Reel armado",
              img=almacen.relativa(salida), copia=cid, cuenta=c.get("cuenta", ""))
    try:
        from ..comun import aviso
        titulo = (c.get("texto") or "").strip().splitlines()[0][:70] if (c.get("texto") or "").strip() else cid
        aviso(f"🎬 Video listo: «{titulo}»" + (f" para @{c['cuenta']}" if c.get("cuenta") else "")
              + (". Ahora lo revisa el juez." if c.get("juzgar") else ". Miralo en Copiar video."), copia=cid)
    except Exception as ex:
        log.warning("aviso de video listo %s: %s", cid, ex)
    try:
        a_resultados(cid)
    except Exception as ex:
        log.warning("resultados %s: %s", cid, ex)
    if c.get("juzgar"):
        try:
            _al_juez(cid, salida, total)
        except Exception as ex:
            c2 = _datos(cid)
            _datos(cid, juez={**(c2.get("juez") or {}), "estado": "sin_juez", "motivo": f"No se pudo pedir el juez: {str(ex)[:150]}"})


# --- 4) el juez --------------------------------------------------------------------------------------------------------------------------
def _al_juez(cid: str, video: Path, dur: float) -> None:
    from PIL import Image, ImageDraw
    d = _dir(cid)
    tiempos = [0.05] + [round(dur * (i + 1) / 8, 2) for i in range(6)] + [round(max(0.1, dur - 0.15), 2)]
    tmp = d / "_rev"
    tmp.mkdir(exist_ok=True)
    cw = 240
    hoja = dr = None
    ch = 0
    for i, t in enumerate(tiempos):
        f = tmp / f"{i}.jpg"
        if not medios.cuadro(video, t, f, 480):
            continue
        im = Image.open(f).convert("RGB")
        im.thumbnail((cw, cw * 3))
        if hoja is None:
            ch = im.height
            hoja = Image.new("RGB", (cw * 4, ch * 2), "white")
            dr = ImageDraw.Draw(hoja)
        x, y = (i % 4) * cw, (i // 4) * ch
        hoja.paste(im, (x, y))
        for z in (IG_ARRIBA, IG_ABAJO):
            dr.line((x, y + int(ch * z), x + cw, y + int(ch * z)), fill="red", width=1)
        dr.rectangle((x, y, x + 46, y + 18), fill="black")
        dr.text((x + 4, y + 4), f"{t:g}s", fill="yellow")
    shutil.rmtree(tmp, ignore_errors=True)
    if hoja is None:
        raise RuntimeError("no se pudieron sacar cuadros del reel armado")
    hoja.save(d / "revision.jpg", quality=82)
    c = _datos(cid)
    _datos(cid, juez={**(c.get("juez") or {}), "estado": "mirando"}, revision="revision.jpg")
    fotos_hoja, _ = _candidatas(c.get("cuenta") or "")
    tareas.crear("juez", {"hay_fotos": bool(fotos_hoja)}, referencia=cid,
                 imagenes=[almacen.relativa(d / "revision.jpg"), almacen.relativa(d / "hoja.jpg")])


@tareas.al_terminar("juez")
def _juzgado(t, j: dict) -> None:
    try:
        _juzgado_ya(t, j)
    except ValueError as ex:   # no se pudo rehacer (ej.: falta la música o la foto): queda rechazado con el motivo
        try:
            c = _datos(t.referencia)
            _datos(t.referencia, juez={**(c.get("juez") or {}), "estado": "rechazado", "motivo": str(ex)[:200]})
        except ValueError:
            pass


def _juzgado_ya(t, j: dict) -> None:
    cid = t.referencia
    try:
        c = _datos(cid)
    except ValueError:
        return
    juez = c.get("juez") or {}
    notas = [int(_num(j.get(k), 0, 0, 5)) for k in ("nota_texto", "nota_tecnica") if j.get(k) not in (None, "")]
    nota = min(notas) if notas else 0
    intentos = int(juez.get("intentos") or 0)
    juez.update(nota=nota, motivo=str(j.get("motivo") or "")[:200])
    if nota >= NOTA_MIN:
        actividad("aprobado", f"El juez aprobó el reel ({nota}/5)", img=almacen.relativa(resultado(cid)), copia=cid)
        _datos(cid, juez={**juez, "estado": "aprobado"})
        return
    if intentos >= REINTENTOS:
        _datos(cid, juez={**juez, "estado": "rechazado"})
        return
    actividad("rehaciendo", f"El juez lo tachó ({nota}/5): {juez.get('motivo', '')[:80]} — rehaciéndolo", copia=cid)
    _datos(cid, juez={**juez, "estado": "rehaciendo", "intentos": intentos + 1})
    if j.get("arreglo") == "musica":
        opciones_ = [m for m in material.musicas(c.get("cuenta") or "") if f"copia-{cid}" not in m.name]
        if opciones_:
            audio_de(cid, random.choice(opciones_))
            actividad("musica", "El juez pidió otra música: le pongo una de las guardadas", copia=cid)
            return armar(cid, {"audio": "musica"}, juzgar=True)
    if j.get("arreglo") == "texto" and j.get("texto_y") not in (None, ""):
        campos = {"texto_y": j["texto_y"], "ubicacion_auto": False}
    else:   # otra foto: la que diga el juez o la menos usada que no sea la actual
        _, fotos = _candidatas(c.get("cuenta") or "")
        cual = j.get("foto")
        if cual in (None, "") and fotos:
            usadas = _usadas(c.get("cuenta") or "")
            libres = [f.name for f, _ in fotos if f.name != c.get("modelo_origen")]
            cual = min(libres, key=lambda n: usadas.get(n, 0)) if libres else ""
        if cual not in (None, "") and c.get("cuenta"):
            _elegir_foto(cid, c["cuenta"], cual)
        campos = {"ubicacion_auto": True}
    armar(cid, campos, juzgar=True)


@tareas.al_fallar("juez")
def _juez_fallo(t, motivo: str) -> None:
    try:
        c = _datos(t.referencia)
        _datos(t.referencia, juez={**(c.get("juez") or {}), "estado": "sin_juez", "motivo": motivo[:200]})
    except ValueError:
        pass


# --- resultados con nombre lindo -----------------------------------------------------------------------------------------------------
def nombre_lindo(c: dict) -> str:
    texto = " ".join(str(c.get("texto") or "").split()) or " ".join(t.get("texto", "") for t in c.get("textos") or [])
    texto = re.sub(r'[\\/:*?"<>|“”«»\n]+', "", texto).strip(" .") or f"video {c['id']}"
    if len(texto) > 60:
        texto = texto[:60].rsplit(" ", 1)[0] + "…"
    return f"{c.get('cuenta') or 'sin modelo'} - {texto} ({c['id'][:4]}).mp4"


def a_resultados(cid: str) -> Path | None:
    try:
        c = _datos(cid)
        src = resultado(cid)
    except ValueError:
        return None
    dst_dir = almacen.dir_de("resultados")
    viejo = c.get("resultado_lindo")
    if viejo:
        (dst_dir / Path(viejo).name).unlink(missing_ok=True)   # el anterior de ESTA copia (no por prefijo)
    dst = dst_dir / almacen.nombre_seguro(nombre_lindo(c), 140)
    shutil.copy2(src, dst)
    _datos(cid, resultado_lindo=dst.name)
    return dst


# --- periódicos ------------------------------------------------------------------------------------------------------------------------
def recoger() -> None:
    """Lo que va dejando el buscador de memes (o Claude Code) en copias/_nuevos_links.txt entra al banco."""
    nuevos = _raiz() / "_nuevos_links.txt"
    if nuevos.exists() and time.time() - nuevos.stat().st_mtime > 5:
        texto = nuevos.read_text(encoding="utf-8", errors="ignore")
        nuevos.unlink(missing_ok=True)
        try:
            guardar_links(texto)
        except ValueError:
            pass


def al_arrancar() -> None:
    """Lo que quedó 'armando' sin trabajo vivo (se cortó el worker) vuelve a 'lista' con aviso."""
    from ..modelos import Trabajo
    with sesion() as s:
        vivos = {(t.datos or {}).get("copia") for t in s.scalars(select(Trabajo).where(
            Trabajo.tipo == "render", Trabajo.estado.in_(("pendiente", "corriendo"))))
            if not (t.estado == "corriendo" and t.tomado_hasta and t.tomado_hasta < ahora())}
        ids = [x.id for x in s.scalars(select(Copia).where(Copia.estado == "armando")) if x.id not in vivos]
    for cid in ids:
        _datos(cid, estado="lista", error="El armado se cortó (se reinició el sistema): tocá Armar de nuevo.")
