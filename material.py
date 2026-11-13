"""Material de trabajo de cada cuenta (no se publica): fotos de la modelo, ejemplos y música.

Vive en Material/<cuenta>/{modelo,ejemplos,musica}/; lo General (memes y capturas de chat para todas las cuentas)
en Material/_general/general/. Los ejemplos de TikTok se bajan pegando el link (yt-dlp) y la
música se saca del mismo video con ffmpeg (el de imageio-ffmpeg) y queda en musica/. Los datos de cada link
(link, autor, nombre del tema) se guardan en Material/<cuenta>/material.json.
"""
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import medios

AUDIOS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}
SECCIONES = {"modelo": medios.FOTOS | medios.VIDEOS, "ejemplos": medios.FOTOS | medios.VIDEOS, "musica": AUDIOS | medios.VIDEOS,
             "general": medios.FOTOS | medios.VIDEOS}
GENERAL = "_general"  # dueño de la sección general (compartida entre cuentas)


def tipo_de(p):
    s = Path(p).suffix.lower()
    return "foto" if s in medios.FOTOS else "video" if s in medios.VIDEOS else "audio" if s in AUDIOS else ""


class _Callado:  # yt-dlp no tiene que escribir en la consola (con pythonw no hay)
    def debug(self, m): pass
    def info(self, m): pass
    def warning(self, m): pass
    def error(self, m): pass


COOKIES = None   # cookies.txt con la sesión del buscador (la pone el panel): así Instagram deja bajar los reels


def bajar_video(link, carpeta, nombre="%(uploader).40B_%(id)s"):
    """Baja un video de TikTok / Instagram / etc. con yt-dlp. Devuelve (ruta, info). ValueError con el motivo en criollo."""
    try:
        import yt_dlp
    except ImportError:
        raise ValueError("Falta el programa para bajar videos: corré Instalar.bat de nuevo.")
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    opts = {"outtmpl": str(carpeta / f"{nombre}.%(ext)s"), "restrictfilenames": True,
            "format": "b[vcodec^=h264]/b[vcodec^=avc]/b", "noplaylist": True, "quiet": True,
            "no_warnings": True, "noprogress": True, "logger": _Callado(), "ffmpeg_location": medios.ffmpeg()}
    if COOKIES and Path(COOKIES).is_file():
        opts["cookiefile"] = str(COOKIES)
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
        if "instagram" in link and ("login" in txt or "cookie" in txt or "rate" in txt or "empty media" in txt):
            raise ValueError("Instagram no dejó bajar ese video sin iniciar sesión. Bajalo con el celu (o una página "
                             "de descargas) y subilo con el botón «Subir video».")
        raise ValueError("No se pudo bajar. Fijate que el link sea de un video público.")
    return video, info


class Material:
    def __init__(self, raiz, leer, escribir):
        self.raiz, self.leer, self.escribir = Path(raiz), leer, escribir
        self._lock = threading.Lock()
        self.bajando = {}  # id -> {cuenta, link, error}

    def secciones(self, cuenta):
        """Las fijas (modelo, ejemplos, música, general) + las carpetas que ella cree (fotos y videos)."""
        vacia = not any((self.raiz / str(cuenta) / "modelo").glob("*.*"))
        out = {k: v for k, v in SECCIONES.items() if not (k == "modelo" and vacia)}   # la vieja "modelo" se oculta si quedó vacía
        d = self.raiz / str(cuenta)
        if d.is_dir():
            for c in sorted(d.iterdir()):
                if c.is_dir() and c.name not in SECCIONES and not c.name.startswith(("_", ".")):
                    out[c.name] = medios.FOTOS | medios.VIDEOS
        return out

    def carpeta(self, cuenta, sec):
        if sec not in self.secciones(cuenta):
            raise ValueError("Carpeta desconocida.")
        return self.raiz / self._de(cuenta, sec) / sec

    def nueva_carpeta(self, cuenta, nombre):
        nombre = re.sub(r"\s+", " ", re.sub(r"[^\w áéíóúñÁÉÍÓÚÑ\-]", "", str(nombre or ""))).strip()[:40]
        if not nombre or nombre.startswith(("_", ".")):
            raise ValueError("Escribí un nombre para la carpeta.")
        if nombre.lower() in {k.lower() for k in self.secciones(cuenta)}:
            raise ValueError("Ya hay una carpeta con ese nombre.")
        (self.raiz / str(cuenta) / nombre).mkdir(parents=True, exist_ok=True)
        return nombre

    def resumen(self, cuenta):
        """Para la tarjeta de la modelo: cuántos videos y fotos tiene, cuántas carpetas, última fecha y una foto."""
        vid = fot = 0
        ultima, foto = 0.0, None
        secs = [k for k in self.secciones(cuenta) if k != "general"]
        for sec in secs:
            d = self.raiz / str(cuenta) / sec
            for f in (d.iterdir() if d.is_dir() else []):
                if not f.is_file():
                    continue
                if medios.es_video(f):
                    vid += 1
                elif medios.es_foto(f):
                    fot += 1
                    if sec in ("modelo", "Fotos") and (foto is None or f.stat().st_mtime < foto.stat().st_mtime):
                        foto = f
                ultima = max(ultima, f.stat().st_mtime)
        return {"videos": vid, "fotos": fot, "carpetas": len(secs), "ultima": ultima, "foto": foto}

    @staticmethod
    def _de(cuenta, sec):
        return GENERAL if sec == "general" else cuenta

    def _meta_p(self, cuenta):
        return self.raiz / cuenta / "material.json"

    def _meta(self, cuenta, cambiar=None):
        with self._lock:
            m = self.leer(self._meta_p(cuenta), {}) or {}
            if cambiar:
                cambiar(m)
                self.escribir(self._meta_p(cuenta), m)
            return m

    def listar(self, cuenta, rel):
        metas = {cuenta: self._meta(cuenta), GENERAL: self._meta(GENERAL)}
        out = {}
        for sec, exts in self.secciones(cuenta).items():
            meta = metas[self._de(cuenta, sec)]
            d = self.carpeta(cuenta, sec)
            fs = sorted((p for p in d.glob("*") if p.is_file() and p.suffix.lower() in exts),
                        key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
            out[sec] = [{"nombre": p.name, "archivo": rel(p), "tipo": tipo_de(p), "fecha": p.stat().st_mtime,
                         **meta.get(f"{sec}/{p.name}", {})}
                        for p in fs]
        out["bajando"] = [{"id": k, **v} for k, v in self.bajando.items() if v["cuenta"] == cuenta]
        return out

    def _libre(self, p):
        n = 2
        base = p
        while p.exists():
            p = base.with_name(f"{base.stem} ({n}){base.suffix}")
            n += 1
        return p

    def guardar(self, cuenta, sec, nombre, datos):
        limpio = re.sub(r"[^\w.\- ]+", "_", Path(nombre).name)[-90:] or "archivo"
        if Path(limpio).suffix.lower() not in self.secciones(cuenta).get(sec, ()):
            raise ValueError({"modelo": "Acá van fotos o videos de la modelo.", "ejemplos": "Acá van fotos, capturas o videos.",
                              "musica": "Acá van audios (mp3, m4a, wav) o videos de los que se saca la música.",
                              "general": "Acá van fotos, capturas o videos."}
                             .get(sec, "Sección desconocida."))
        destino = self._libre(self.carpeta(cuenta, sec) / limpio)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(datos)
        if sec == "musica" and medios.es_video(destino):  # subió un video a Música: se queda solo con el audio
            audio = self._sacar_audio(destino, destino.with_suffix(".m4a"))
            destino.unlink(missing_ok=True)
            if not audio:
                raise ValueError("No se pudo sacar la música de ese video.")
            destino = audio
        return destino.name

    def borrar(self, cuenta, sec, nombre):
        p = self.carpeta(cuenta, sec) / Path(nombre).name
        if not p.is_file():
            raise ValueError("Ese archivo ya no está.")
        p.unlink()
        self._meta(self._de(cuenta, sec), lambda m: m.pop(f"{sec}/{p.name}", None))

    def etiquetar(self, cuenta, sec, nombre, etiquetas):
        p = self.ruta(cuenta, sec, nombre)
        limpias = []
        for e in etiquetas or []:
            e = re.sub(r"\s+", " ", str(e)).strip().lower()[:30]
            if e and e not in limpias:
                limpias.append(e)
        self._meta(self._de(cuenta, sec), lambda m: m.setdefault(f"{sec}/{p.name}", {}).update(etiquetas=limpias[:12]))
        return limpias[:12]

    def ruta(self, cuenta, sec, nombre):
        p = self.carpeta(cuenta, sec) / Path(nombre).name
        if not p.is_file():
            raise ValueError("Ese archivo ya no está.")
        return p

    # --- TikTok por link ----------------------------------------------------------------------------------
    def bajar(self, cuenta, link):
        link = str(link or "").strip()
        if not re.match(r"https?://", link):
            raise ValueError("Pegá el link completo (empieza con https://).")
        if any(v["link"] == link and v["cuenta"] == cuenta and not v.get("error") for v in self.bajando.values()):
            raise ValueError("Ese link ya se está bajando.")
        k = uuid.uuid4().hex[:8]
        self.bajando[k] = {"cuenta": cuenta, "link": link, "error": ""}
        threading.Thread(target=self._bajar, args=(k, cuenta, link), daemon=True).start()
        return k

    def descartar(self, k):
        self.bajando.pop(k, None)

    def _bajar(self, k, cuenta, link):
        try:
            video, info = bajar_video(link, self.carpeta(cuenta, "ejemplos"))
        except ValueError as ex:
            self.bajando[k]["error"] = str(ex)
            return
        tema = " - ".join(x for x in [info.get("track"), ", ".join(info.get("artists") or []) or info.get("artist")] if x)
        musica = self._sacar_audio(video, self._libre(self.carpeta(cuenta, "musica") / f"{video.stem}.m4a"))
        datos = {"link": link, "autor": info.get("uploader") or "", "titulo": (info.get("description") or info.get("title") or "")[:200],
                 "tema": tema, "musica": musica.name if musica else "", "bajado": time.strftime("%Y-%m-%dT%H:%M:%S")}

        def anotar(m):
            m[f"ejemplos/{video.name}"] = datos
            if musica:
                m[f"musica/{musica.name}"] = {"link": link, "autor": datos["autor"], "tema": tema, "de": video.name}
        self._meta(cuenta, anotar)
        self.bajando.pop(k, None)

    def _sacar_audio(self, video, destino):
        ff = medios.ffmpeg()
        if not ff:
            return None
        destino.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run([ff, "-y", "-v", "error", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "192k", str(destino)],
                               capture_output=True, timeout=180, creationflags=medios.SIN_VENTANA)
        except Exception:
            return None
        if r.returncode or not destino.is_file() or destino.stat().st_size < 1000:
            destino.unlink(missing_ok=True)
            return None
        return destino
