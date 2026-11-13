"""Fotos y videos de la bandeja: miniaturas, cuadros para los jueces y JPEG listos para Instagram.

ffmpeg sale del paquete imageio-ffmpeg (trae su propio ffmpeg: no hay que instalar nada aparte).
Si no está, se prueba con un ffmpeg del PATH; si tampoco, los videos se muestran sin miniatura.
"""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

FOTOS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".gif"}
VIDEOS = {".mp4", ".mov", ".m4v"}
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def es_foto(p):
    return Path(p).suffix.lower() in FOTOS


def es_video(p):
    return Path(p).suffix.lower() in VIDEOS


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _abrir_imagen(p):
    from PIL import Image, ImageOps
    if Path(p).suffix.lower() == ".heic":
        try:
            import pillow_heif  # opcional
            pillow_heif.register_heif_opener()
        except Exception:
            raise RuntimeError("Las fotos .heic del iPhone necesitan 'pillow-heif' (o exportalas como JPG).")
    im = Image.open(p)
    return ImageOps.exif_transpose(im).convert("RGB")


def clave(p):
    st = Path(p).stat()
    return hashlib.sha1(f"{Path(p).resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]


def duracion(p):
    exe = ffmpeg()
    if not exe:
        return None
    r = subprocess.run([exe, "-hide_banner", "-i", str(p)], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=SIN_VENTANA)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr or "")
    return round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 1) if m else None


def cuadro(p, segundo, destino, ancho=720):
    exe = ffmpeg()
    if not exe:
        return False
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, segundo):.2f}", "-i", str(p),
                    "-frames:v", "1", "-vf", f"scale={ancho}:-2", "-q:v", "3", str(destino)],
                   capture_output=True, creationflags=SIN_VENTANA, timeout=120)
    return destino.exists()


def miniatura(p, carpeta, ancho=480):
    """Miniatura JPEG (cacheada por archivo+tamaño+fecha). Devuelve la ruta o None."""
    p = Path(p)
    destino = Path(carpeta) / f"{clave(p)}.jpg"
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        if es_foto(p):
            im = _abrir_imagen(p)
            im.thumbnail((ancho, ancho * 2))
            im.save(destino, "JPEG", quality=82)
        elif es_video(p):
            d = duracion(p) or 2
            if not cuadro(p, min(1.0, d / 3), destino, ancho):
                return None
        return destino if destino.exists() else None
    except Exception:
        return None


def cuadros_jueces(p, carpeta, n=5):
    """Para un video: n cuadros repartidos (el primero en 0,3 s para juzgar el gancho). Para una foto: ella misma
    achicada. Devuelve [rutas] y la duración."""
    p, carpeta = Path(p), Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    if es_foto(p):
        destino = carpeta / f"{p.stem}_jueces.jpg"
        im = _abrir_imagen(p)
        im.thumbnail((1080, 1350 * 2))
        im.save(destino, "JPEG", quality=85)
        return [destino], None
    d = duracion(p) or 0
    if d <= 0:
        return [], None
    marcas = [0.3, 2.0] + [d * k for k in (0.4, 0.65, 0.9)][: max(0, n - 2)]
    out = []
    for i, s in enumerate(marcas):
        s = min(s, max(0, d - 0.2))
        destino = carpeta / f"{p.stem}_cuadro{i + 1}_{s:.1f}s.jpg"
        if cuadro(p, s, destino):
            out.append(destino)
    return out, d


def jpeg_para_instagram(p, destino, tipo, proporcion=None):
    """Instagram por API acepta SOLO JPEG. Feed (foto/carrusel): proporción entre 4:5 y 1,91:1; si se sale, se completa
    con un fondo desenfocado de la misma foto (no se recorta nada). Historia: 9:16 con el mismo fondo.
    proporcion (ancho/alto) fuerza la misma para todas las fotos de un carrusel."""
    from PIL import Image, ImageFilter
    im = _abrir_imagen(p)
    w, h = im.size
    r = w / h
    if tipo == "historia":
        objetivo = 9 / 16
    elif proporcion:
        objetivo = proporcion
    else:
        objetivo = min(1.91, max(0.8, r))
    if abs(r - objetivo) > 0.01:
        if r > objetivo:  # más ancha: completar arriba y abajo
            W, H = w, round(w / objetivo)
        else:
            W, H = round(h * objetivo), h
        fondo = im.resize((max(1, W // 8), max(1, H // 8))).filter(ImageFilter.GaussianBlur(6)).resize((W, H))
        fondo.paste(im, ((W - w) // 2, (H - h) // 2))
        im = fondo
    if im.width > 1440:
        im = im.resize((1440, round(im.height * 1440 / im.width)))
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    im.save(destino, "JPEG", quality=92)
    return destino


def proporcion_carrusel(rutas):
    """La de la primera foto, dentro de lo que permite Instagram (todas las fotos del carrusel van iguales)."""
    for p in rutas:
        if es_foto(p):
            try:
                im = _abrir_imagen(p)
                return min(1.91, max(0.8, im.width / im.height))
            except Exception:
                pass
    return 0.8
