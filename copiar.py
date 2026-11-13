"""Copiar video: tomar un video de ejemplo (link de Instagram/TikTok o archivo), poner a la modelo en la primera parte
con el mismo texto y seguir con el resto del ejemplo (el remate del meme). Lo arma ffmpeg; no se publica nada.

Cada copia vive en Material/_copias/<id>/: copia.json, ejemplo.mp4, hoja.jpg (cuadros con segundos para que Claude
lea el texto y el corte), modelo.<ext>, audio.<ext> (si eligió otra música) y resultado-<n>.mp4 (cada armado queda
en el historial: copia.json["versiones"]). Claude escribe lo que leyó en lectura.json y el panel lo incorpora solo.
"""
import hashlib
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import chat
import material
import medios

FUENTES_DIR = Path(r"C:\Windows\Fonts")
# clave -> (archivo, familia CSS, peso CSS, estilo CSS, nombre): la vista previa del panel usa la misma letra
FUENTES = {
    "clasica": ("segoeuib.ttf", "Segoe UI", 700, "normal", "Clásica"),
    "fuerte": ("seguibl.ttf", "Segoe UI", 900, "normal", "Fuerte"),
    "moderna": ("bahnschrift.ttf", "Bahnschrift", 400, "normal", "Moderna"),
    "meme": ("impact.ttf", "Impact", 400, "normal", "Meme"),
    "maquina": ("courbd.ttf", "Courier New", 700, "normal", "Máquina"),
    "elegante": ("georgiaz.ttf", "Georgia", 700, "italic", "Elegante"),
    "manuscrita": ("segoeprb.ttf", "Segoe Print", 700, "normal", "Manuscrita"),
    "redonda": ("trebucbd.ttf", "Trebuchet MS", 700, "normal", "Redonda"),
}
ESTILOS = ("sombra", "caja", "contorno", "nada")
COLORES = {"blanco": "ffffff", "negro": "111111", "amarillo": "ffe14d", "rosa": "ff5fa2", "rojo": "ff3b3b",
           "celeste": "5cc8ff"}
AUDIOS = ("ejemplo", "modelo", "musica", "ninguno")
VIEJOS_TAM = {"chico": 0.034, "medio": 0.042, "grande": 0.052}
VIEJAS_POS = {"arriba": 0.17, "medio": 0.45, "abajo": 0.72}
ANCHO_MAX = 0.72      # el texto se parte en renglones para no pasar el 72 % del ancho (como los reels buenos)
INTERLINEA = 1.25     # alto de renglón / alto de letra
CAJA_PAD = 0.28       # margen de la caja / alto de letra
MAX_VERSIONES = 15
IG_ARRIBA, IG_ABAJO = 0.11, 0.81
ESCALA_TEXTO = 1.2   # la letra un 20 % más grande que la del ejemplo (más fácil de leer en el celu)
NOTA_MIN, REINTENTOS = 3, 2   # el juez: nota 1-5; menos de 3 se rehace (hasta 2 veces)   # lo que tapa Instagram en un reel: usuario arriba, texto y botones abajo


def _info_video(p):
    """(ancho, alto, duración, tiene_audio) leyendo lo que imprime ffmpeg."""
    r = subprocess.run([medios.ffmpeg(), "-i", str(p)], capture_output=True, text=True, errors="ignore",
                       creationflags=medios.SIN_VENTANA)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", r.stderr)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (720, 1280)
    rot = re.search(r"rotation of (-?\d+)", r.stderr) or re.search(r"rotate\s*:\s*(-?\d+)", r.stderr)
    if rot and abs(int(rot.group(1))) in (90, 270):
        w, h = h, w
    return w - w % 2, h - h % 2, medios.duracion(p) or 0, "Audio:" in r.stderr


def _renglones(texto, fuente_p, tam_px, max_px):
    """Parte el texto en renglones midiendo el ancho real de la letra (igual que la vista previa del panel)."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(str(fuente_p), tam_px)
        medir = f.getlength
    except Exception:
        medir = lambda s: len(s) * tam_px * 0.55
    out = []
    for parrafo in str(texto or "").replace("\r", "").split("\n"):
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


def _juntar(cajas):
    """Une las cajas que se superponen (la misma cara vista de frente y de perfil, o en varios cuadros)."""
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


def normalizar(c):
    """Pasa copias viejas (posición/tamaño en palabras) al formato nuevo y completa lo que falte."""
    if isinstance(c.get("texto_tam"), str):
        c["texto_tam"] = VIEJOS_TAM.get(c["texto_tam"], 0.042)
    if c.get("texto_y") is None:
        c["texto_y"] = VIEJAS_POS.get(c.get("texto_pos"), 0.17)
    c.setdefault("texto_x", 0.5)
    # siempre letra blanca, la básica de Instagram (Clásica), sin sombra ni fondo: nada raro
    c.update(fuente="clasica", estilo="nada", color="blanco")
    c.setdefault("audio", "ejemplo")
    c.setdefault("audio_archivo", "")
    c.setdefault("audio_desde", 0)
    c.setdefault("ubicacion_auto", True)
    c.setdefault("versiones", [{"archivo": c["resultado"], "armado": c.get("armado", ""), "texto": c.get("texto", "")}]
                 if c.get("resultado") else [])
    return c


class Copiar:
    def __init__(self, raiz, leer, escribir, pedido, rel, fotos_de=None):
        """fotos_de(cuenta) -> [(ruta, etiquetas)]: las fotos de la modelo en Material, para que Claude elija."""
        self.raiz, self.leer, self.escribir, self.pedido, self.rel = Path(raiz), leer, escribir, pedido, rel
        self.fotos_de = fotos_de or (lambda cuenta: [])
        self.resultados = self.raiz.parent.parent / "Resultados"   # Hookline Lite/Resultados
        self.log = lambda *a, **k: None   # el panel lo reemplaza: registro en vivo de lo que hace la fábrica
        self.guardar_musica = lambda video, cuenta, nombre: None   # el panel: guarda el audio de cada ejemplo en Música
        self.musicas = lambda cuenta: []                          # el panel: la música guardada (para cambiarla)
        self._lock = threading.Lock()
        self._bajando = threading.Semaphore(3)   # muchos links a la vez: de a 3 descargas
        self._armando = threading.Semaphore(2)   # y de a 2 videos armándose (para no colgar la compu)

    # --- datos -------------------------------------------------------------------------------------------
    def _dir(self, cid):
        if not re.fullmatch(r"[0-9a-f]{8}", str(cid or "")):
            raise ValueError("Copia desconocida.")
        return self.raiz / cid

    def _datos(self, cid, **cambios):
        with self._lock:
            p = self._dir(cid) / "copia.json"
            d = self.leer(p, None)
            if d is None:
                raise ValueError("Esa copia ya no está.")
            normalizar(d)
            if cambios:
                d.update(cambios)
                self.escribir(p, d)
            return d

    def listar(self):
        out = []
        if self.raiz.exists():
            for d in self.raiz.iterdir():
                c = self.leer(d / "copia.json", None)
                if not isinstance(c, dict):
                    continue
                normalizar(c)
                for k in ("ejemplo", "modelo", "resultado", "hoja", "audio_archivo", "revision"):
                    if c.get(k):
                        c[k + "_url"] = self.rel(d / c[k])
                for v in c["versiones"]:
                    v["url"] = self.rel(d / v["archivo"])
                out.append(c)
        return sorted(out, key=lambda c: c.get("creado", ""), reverse=True)

    @staticmethod
    def opciones():
        return {"fuentes": {k: {"familia": f[1], "peso": f[2], "estilo": f[3], "nombre": f[4]}
                            for k, f in FUENTES.items() if (FUENTES_DIR / f[0]).exists()},
                "colores": COLORES, "ancho_max": ANCHO_MAX, "interlinea": INTERLINEA, "caja_pad": CAJA_PAD}

    def _nueva(self, **campos):
        cid = uuid.uuid4().hex[:8]
        d = self.raiz / cid
        d.mkdir(parents=True, exist_ok=True)
        datos = normalizar({"id": cid, "creado": time.strftime("%Y-%m-%dT%H:%M:%S"), "estado": "bajando", "error": "",
                            "link": "", "ejemplo": "", "duracion": 0, "ancho": 720, "alto": 1280, "corte": 0,
                            "texto": "", "texto_tam": 0.042, "texto_y": 0.17, "modelo": "", "resultado": "",
                            "leido": False, "notas": "", **campos})
        self.escribir(d / "copia.json", datos)
        return cid

    def cancelar(self):
        """Frena lo que todavía no se armó (bajando / esperando a Claude): se borra y su link vuelve a quedar sin usar."""
        fuera = [c["id"] for c in self.listar() if c.get("estado") in ("bajando", "leyendo")]
        for cid in fuera:
            self.borrar(cid)
        def soltar(ls):
            for x in ls:
                if x.get("copia") in fuera:
                    x["copia"] = ""
        self._banco(soltar)
        return len(fuera)

    def borrar(self, cid):
        shutil.rmtree(self._dir(cid), ignore_errors=True)
        def soltar(ls):   # su link vuelve a "sin usar"
            for x in ls:
                if x.get("copia") == cid:
                    x["copia"] = ""
        self._banco(soltar)

    def borrar_version(self, cid, archivo):
        c = self._datos(cid)
        vs = [v for v in c["versiones"] if v["archivo"] != archivo]
        (self._dir(cid) / Path(archivo).name).unlink(missing_ok=True)
        self._datos(cid, versiones=vs, resultado=vs[-1]["archivo"] if vs else "")

    # --- 1) el ejemplo -----------------------------------------------------------------------------------
    @staticmethod
    def _sacar_links(texto):
        links = list(dict.fromkeys(re.findall(r"https?://[^\s,;]+", str(texto or ""))))
        if not links:
            raise ValueError("Pegá el link completo (empieza con https://).")
        return links

    # --- banco de links: todo lo que pegó alguna vez, para hacer 1, 10 o 30 cuando quiera ----------------
    def _banco(self, cambiar=None):
        with self._lock:
            p = self.raiz / "_links.json"
            b = self.leer(p, None)
            if b is None:   # la primera vez: los links de las copias que ya existen
                b = {"links": [{"link": c["link"], "agregado": c.get("creado", ""), "copia": c["id"]}
                               for c in sorted(self.listar(), key=lambda c: c.get("creado", "")) if c.get("link")]}
                cambiar = cambiar or (lambda ls: None)
            if cambiar:
                cambiar(b["links"])
                self.raiz.mkdir(parents=True, exist_ok=True)
                self.escribir(p, b)
            return b["links"]

    def links(self):
        ls = list(reversed(self._banco()))
        if any(not x.get("ficha") for x in ls):
            self._fichas_ya()
        for x in ls:
            if x.get("mini"):
                x["mini_url"] = self.rel(self.raiz / "_minis" / x["mini"])
        return ls

    def guardar_links(self, texto, usados=None):
        """Suma al banco los links nuevos; usados = {link: id de la copia} los marca como ya hechos.
        Acepta renglones "link | título | categoría" (así vienen los que junta el buscador)."""
        nuevos, usados = self._sacar_links(texto), usados or {}
        extra = {}
        for renglon in str(texto or "").splitlines():
            partes = [x.strip() for x in renglon.split("|")]
            m = re.search(r"https?://[^\s,;|]+", partes[0]) if len(partes) > 1 else None
            if m:
                extra[m.group(0)] = {"titulo": partes[1][:120], "cat": (partes[2] if len(partes) > 2 else "")[:30]}
        def cambiar(ls):
            ya = {x["link"]: x for x in ls}
            for l in nuevos:
                if l not in ya:
                    ya[l] = {"link": l, "agregado": time.strftime("%Y-%m-%dT%H:%M:%S"), "copia": "", **extra.get(l, {})}
                    ls.append(ya[l])
                if usados.get(l):
                    ya[l]["copia"] = usados[l]
        self._banco(cambiar)
        self._fichas_ya()
        return len(nuevos)

    def _fichas_ya(self):
        """En segundo plano: título, duración y miniatura de cada link guardado (para verlos en Inspiración)."""
        with self._lock:
            if getattr(self, "_fichando", False):
                return
            self._fichando = True
        threading.Thread(target=self._fichas, daemon=True).start()

    def _fichas(self):
        try:
            import urllib.request
            import yt_dlp
            from PIL import Image
            minis = self.raiz / "_minis"
            minis.mkdir(parents=True, exist_ok=True)
            while True:
                faltan = [x["link"] for x in self._banco() if not x.get("ficha")]
                if not faltan:
                    return
                link, ficha = faltan[0], {"ficha": "error"}
                try:
                    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as y:
                        info = y.extract_info(link, download=False)
                    nombre = f"{re.sub(r'[^0-9A-Za-z_-]', '', str(info.get('id') or uuid.uuid4().hex))[:40]}.jpg"
                    ficha = {"ficha": "ok", "dur": round(float(info.get("duration") or 0), 1),
                             "autor": str(info.get("uploader") or info.get("channel") or "")[:60]}
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
                    ficha["error"] = str(ex)[:120]
                def poner(ls, link=link, ficha=ficha):
                    for x in ls:
                        if x["link"] == link:
                            x.update(ficha)
                self._banco(poner)
        finally:
            self._fichando = False

    def previa(self, link):
        """Baja el video del banco para verlo dentro del panel (sin abrir el navegador). Queda guardado."""
        x = next((x for x in self._banco() if x["link"] == link), None)
        if not x:
            raise ValueError("Ese link ya no está en el banco.")
        if x.get("copia"):
            try:
                c = self._datos(x["copia"])
                if c.get("ejemplo") and (self._dir(x["copia"]) / c["ejemplo"]).is_file():
                    return self._dir(x["copia"]) / c["ejemplo"]
            except ValueError:
                pass
        carpeta = self.raiz / "_previas"
        nombre = hashlib.md5(link.encode()).hexdigest()[:12]
        ya = next(iter(carpeta.glob(nombre + ".*")), None) if carpeta.exists() else None
        if ya:
            return ya
        return material.bajar_video(link, carpeta, nombre)[0]

    def agregar_encontrados(self, items):
        """Lo que encontró el buscador y aprobó Claude: entra al banco con su miniatura ya hecha."""
        minis = self.raiz / "_minis"
        minis.mkdir(parents=True, exist_ok=True)
        nuevos = []
        for it in items:
            nombre = hashlib.md5(it["link"].encode()).hexdigest()[:12] + ".jpg"
            try:
                shutil.copyfile(it["img"], minis / nombre)
            except OSError:
                nombre = ""
            nuevos.append({"link": it["link"], "agregado": time.strftime("%Y-%m-%dT%H:%M:%S"), "copia": "",
                           "titulo": it.get("titulo", ""), "cat": it.get("cat", ""), "ficha": "ok", "mini": nombre,
                           "origen": it.get("origen", "buscador"), "voto": it.get("voto", ""), "autor": it.get("autor", "")})
        def cambiar(ls):
            ya = {x["link"] for x in ls}
            ls.extend(x for x in nuevos if x["link"] not in ya)
        antes = len(self._banco())
        self._banco(cambiar)
        return len(self._banco()) - antes

    def poner_autor(self, link, autor):
        def poner(ls):
            for x in ls:
                if x["link"] == link:
                    x["autor"] = autor
        self._banco(poner)

    def votar(self, link, voto, motivo=""):
        """Lo que le gusta (bueno) y lo que no (malo): el buscador lo estudia para traer mejores."""
        voto = voto if voto in ("bueno", "malo", "oro") else ""
        def poner(ls):
            for x in ls:
                if x["link"] == link:
                    x["voto"], x["motivo"] = voto, str(motivo or "")[:80]
        self._banco(poner)

    def borrar_link(self, link):
        self._banco(lambda ls: ls.__setitem__(slice(None), [x for x in ls if x["link"] != link]))

    def hacer_guardados(self, n, cuenta=""):
        """Arma n videos con los links guardados que todavía no se usaron (los más viejos primero)."""
        libres = [x["link"] for x in self._banco() if not x.get("copia")]
        if not libres:
            raise ValueError("No quedan links sin usar. Pegá más arriba.")
        return self.desde_links("\n".join(libres[:max(1, int(n or 1))]), cuenta)

    def desde_links(self, texto, cuenta=""):
        """Uno o muchos links (uno por renglón o pegados como vengan): una copia automática por cada uno."""
        links = self._sacar_links(texto)
        if len(links) > 150:
            raise ValueError("Son muchos: pegá hasta 150 links por vez.")
        ids = []
        for i, link in enumerate(links):
            cid = self._nueva(link=link, cuenta=cuenta, lote_orden=i)
            ids.append(cid)
            threading.Thread(target=self._bajar, args=(cid, link), daemon=True).start()
        self.guardar_links(texto, dict(zip(links, ids)))
        return ids

    def _bajar(self, cid, link):
        try:
            self._bajar_ya(cid, link)
        except ValueError:   # la borraron o cancelaron mientras bajaba
            pass

    def _bajar_ya(self, cid, link):
        with self._bajando:
            try:
                video, _ = material.bajar_video(link, self._dir(cid), "ejemplo")
            except ValueError as ex:
                self._datos(cid, estado="error", error=str(ex))
                return
            self._preparar(cid, video)

    def desde_archivo(self, nombre, datos, cuenta=""):
        if not medios.es_video(nombre):
            raise ValueError("Subí un video (mp4 o mov).")
        cid = self._nueva(cuenta=cuenta)
        p = self._dir(cid) / ("ejemplo" + Path(nombre).suffix.lower())
        p.write_bytes(datos)
        threading.Thread(target=self._preparar, args=(cid, p), daemon=True).start()
        return cid

    def _preparar(self, cid, video):
        """Cuadros cada ~0,5 s con el segundo escrito, para que Claude lea texto y corte de una sola mirada."""
        try:
            from PIL import Image, ImageDraw
            w, h, dur, _ = _info_video(video)
            paso = max(0.5, round(dur / 24 * 2) / 2)
            tiempos = [round(i * paso, 2) for i in range(int(dur / paso) + 1) if i * paso < dur - 0.15]
            tmp = self._dir(cid) / "_cuadros"
            tmp.mkdir(exist_ok=True)
            cw, ch = 220, int(220 * h / w) if w else 390
            cols = 8
            hoja = Image.new("RGB", (cw * cols, ch * ((len(tiempos) + cols - 1) // cols)), "white")
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
            hoja.save(self._dir(cid) / "hoja.jpg", quality=82)
            shutil.rmtree(tmp, ignore_errors=True)
            self.log("leyendo", "Claude está mirando un ejemplo nuevo", img=self.rel(self._dir(cid) / video.name), copia=cid)
            try:   # siempre se guarda la música del TikTok/reel: sirve para otros videos si el audio de uno no sirve
                self.guardar_musica(video, self._datos(cid).get("cuenta") or "", f"copia-{cid}")
            except Exception:
                pass
            self._datos(cid, estado="leyendo", ejemplo=video.name, hoja="hoja.jpg", duracion=round(dur, 2), ancho=w,
                        alto=h, corte=round(min(3.0, dur / 2), 1))
            d = self._dir(cid)
            cuenta = self._datos(cid).get("cuenta") or ""
            fotos_hoja, fotos = self._candidatas(cuenta)
            elegir = ""
            if fotos_hoja:
                usadas = self._usadas(cuenta)
                rep_txt = ", ".join(f"{i + 1}×{usadas[r.name]}" for i, (r, _) in enumerate(fotos) if usadas.get(r.name))
                elegir = (f" Además mirá {fotos_hoja} (fotos y videos de @{cuenta} numerados, los videos tienen una V roja; es la misma hoja para todo el lote) y poné "
                          "en modelo el número de la foto que mejor encaje con el ejemplo (encuadre, pose, ropa y situación "
                          "parecidas; que la cara se vea y quede lugar para el texto)."
                          + (f" Ya usadas (número×veces): {rep_txt}: preferí otras." if rep_txt else ""))
            self.pedido(f"copiar · {cid}. Mirá {d / 'hoja.jpg'} (cuadros del video de ejemplo, con el segundo arriba a la "
                        f"izquierda; dura {dur:.1f} s) y escribí {d / 'lectura.json'} con: corte (segundo donde termina la "
                        "parte de la persona y empieza el remate; si es todo la persona, la duración), texto (el texto "
                        "en pantalla de esa primera parte, tal cual), texto_y (0-1: dónde empieza el texto, arriba del "
                        "todo = 0), texto_tam (alto de letra / alto del video, ej. 0.042), notas (una línea: qué es el remate y si tiene "
                        "riesgo para Instagram)." + elegir + " Sin agente y sin contestar en el chat.",
                        {"tipo_pedido": "copiar", "copia": cid})
        except Exception as ex:
            self._datos(cid, estado="error", error=f"No pude leer el video: {str(ex)[:150]}")

    def _candidatas(self, cuenta):
        """Hoja con las fotos de la modelo numeradas (una por cuenta; se rehace solo si cambian las fotos)."""
        fotos = [(Path(r), et) for r, et in self.fotos_de(cuenta) if medios.es_foto(r) or medios.es_video(r)][:100] if cuenta else []
        if not fotos:
            return None, []
        firma = "|".join(f"{r.name}:{r.stat().st_mtime_ns}" for r, _ in fotos)
        hoja = self.raiz / f"_fotos_{cuenta}.jpg"
        meta = self.leer(self.raiz / f"_fotos_{cuenta}.json", {}) or {}
        if meta.get("firma") != firma or not hoja.exists():
            from PIL import Image, ImageDraw
            cw, ch, cols = 150, 188, 10
            img = Image.new("RGB", (cw * cols, ch * ((len(fotos) + cols - 1) // cols)), "white")
            dr = ImageDraw.Draw(img)
            for i, (r, _) in enumerate(fotos):
                try:
                    if medios.es_video(r):   # los videos de la modelo también se pueden elegir (cuadro del medio + "V")
                        tmp = self.raiz / "_cuadro_tmp.jpg"
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
            self.escribir(self.raiz / f"_fotos_{cuenta}.json", {"firma": firma, "fotos": [r.name for r, _ in fotos]})
        return hoja, fotos

    def _usadas(self, cuenta):
        """Cuántas veces se usó cada foto en las copias de esta cuenta (para no repetir)."""
        n = {}
        if self.raiz.exists():
            for d in self.raiz.iterdir():
                c = self.leer(d / "copia.json", None)
                if isinstance(c, dict) and c.get("cuenta") == cuenta and c.get("modelo_origen"):
                    n[c["modelo_origen"]] = n.get(c["modelo_origen"], 0) + 1
        return n

    def _limpiar_campos(self, l, dur):
        """Valida lo que manda el panel o lo que leyó Claude."""
        out = {}
        if "noti" in l and not (isinstance(l["noti"], dict) and l["noti"].get("texto")):
            out["noti"] = None   # sacarla
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
                                              if l["chat"].get(k) not in (None, "")},
                           "aparece": 0}   # la captura está siempre, desde el primer cuadro (aunque en el ejemplo entre tarde)
            out["pegar"] = []
        if isinstance(l.get("recorte_remate"), dict):
            out["recorte_remate"] = {k: round(_num(l["recorte_remate"].get(k), 0, 0, 1), 4) for k in ("x0", "y0", "x1", "y1")}
        if isinstance(l.get("textos"), list):
            out["textos"] = [{"texto": str(t.get("texto"))[:80], **{k: round(_num(t.get(k), 0.5, 0, 1), 4) for k in ("x", "y", "tam") if t.get(k) not in (None, "")}}
                             for t in l["textos"][:4] if isinstance(t, dict) and t.get("texto")]
        if isinstance(l.get("marcas"), list):
            out["marcas"] = [{k: round(_num(m.get(k), 0, 0.002, 0.998), 4) for k in ("x0", "y0", "x1", "y1")}
                             for m in l["marcas"][:4] if isinstance(m, dict)]
        if isinstance(l.get("pegar"), list):
            out["pegar"] = [{**{k: round(_num(b.get(k), 0, 0, 1), 4) for k in ("x0", "y0", "x1", "y1")},
                             **{k: round(_num(b[k], 0, 0, 600), 2) for k in ("cuadro", "aparece") if b.get(k) not in (None, "")},
                             **({"a_y0": round(_num(b["a_y0"], 0, 0, 1), 4)} if b.get("a_y0") not in (None, "") else {})}
                            for b in l["pegar"][:3] if isinstance(b, dict)]
        if l.get("texto") is not None:
            out["texto"] = str(l["texto"]).replace("\\n", "\n")[:300]
        if l.get("texto_pos") in VIEJAS_POS and l.get("texto_y") is None:
            out["texto_y"] = VIEJAS_POS[l["texto_pos"]]
        if l.get("texto_tam") in VIEJOS_TAM:
            out["texto_tam"] = VIEJOS_TAM[l["texto_tam"]]
        elif l.get("texto_tam") not in (None, ""):
            out["texto_tam"] = round(_num(l["texto_tam"], 0.042, 0.02, 0.1), 4)
        for k, lo, hi in (("texto_y", 0.0, 0.97), ("texto_x", 0.05, 0.95)):
            if l.get(k) not in (None, ""):
                out[k] = round(_num(l[k], 0.5, lo, hi), 4)
        if l.get("fuente") in FUENTES:
            out["fuente"] = l["fuente"]
        if l.get("estilo") in ESTILOS:
            out["estilo"] = l["estilo"]
        if l.get("color") in COLORES:
            out["color"] = l["color"]
        if isinstance(l.get("ubicacion_auto"), bool):
            out["ubicacion_auto"] = l["ubicacion_auto"]
        if l.get("audio") in AUDIOS:
            out["audio"] = l["audio"]
        if l.get("audio_desde") not in (None, ""):
            out["audio_desde"] = round(_num(l["audio_desde"], 0, 0, 3600), 2)
        if l.get("corte") not in (None, ""):
            try:
                out["corte"] = round(max(0.3, min(float(str(l["corte"]).replace(",", ".")), dur or 60)), 2)
            except (TypeError, ValueError):
                raise ValueError("Los segundos de tu modelo tienen que ser un número (ej.: 3 o 2,5).")
        return out

    def recoger(self):
        """Incorpora lo que Claude leyó (lectura.json) a la copia."""
        if not self.raiz.exists():
            return
        nuevos = self.raiz / "_nuevos_links.txt"   # lo que va dejando el buscador de memes
        if nuevos.exists() and time.time() - nuevos.stat().st_mtime > 5:
            texto = nuevos.read_text(encoding="utf-8", errors="ignore")
            nuevos.unlink(missing_ok=True)
            try:
                self.guardar_links(texto)
            except ValueError:
                pass
        for d in self.raiz.iterdir():
            jui = d / "juicio.json"
            if jui.exists():
                j = self.leer(jui, None)
                jui.unlink(missing_ok=True)
                try:
                    if isinstance(j, dict):
                        self._juzgado(d.name, j)
                except ValueError:
                    pass
            lec = d / "lectura.json"
            if not lec.exists():
                continue
            l = self.leer(lec, None)
            lec.unlink(missing_ok=True)
            if not isinstance(l, dict):
                continue
            try:
                c = self._datos(d.name)
                if l.get("descartar"):   # en el ejemplo sale alguien que puede ser menor: no se usa
                    self._datos(d.name, estado="error", leido=True, error="Descartado: en el ejemplo sale alguien que puede ser menor de edad.")
                    continue
                cambios = self._limpiar_campos(l, c.get("duracion"))
                cambios["y_ejemplo"] = cambios.get("texto_y", c.get("texto_y"))
                cambios.update(leido=True, notas=str(l.get("notas") or "")[:300],
                               estado="lista" if c.get("estado") == "leyendo" else c.get("estado"))
                self._datos(d.name, **cambios)
                if l.get("modelo") not in (None, "") and c.get("cuenta"):
                    self._elegir_foto(d.name, c["cuenta"], l["modelo"])
                self._auto(d.name)
            except ValueError:
                pass

    def _elegir_foto(self, cid, cuenta, cual):
        _, fotos = self._candidatas(cuenta)
        r = None
        if str(cual).strip().isdigit() and 1 <= int(cual) <= len(fotos):
            r = fotos[int(cual) - 1][0]
        else:
            r = next((f for f, _ in fotos if f.name == str(cual)), None)
        if r and r.is_file():
            self.subir_modelo(cid, r.name, r.read_bytes(), auto=False)

    def _auto(self, cid):
        """Si ya está todo (ejemplo leído + modelo), arma el video solo."""
        try:
            c = self._datos(cid)
            if c.get("leido") and c.get("modelo") and c.get("ejemplo") and c.get("estado") == "lista":
                self.armar(cid, {}, juzgar=True)
        except ValueError:
            pass

    # --- 2) la modelo y el audio -------------------------------------------------------------------------
    def subir_modelo(self, cid, nombre, datos, auto=True):
        if not (medios.es_foto(nombre) or medios.es_video(nombre)):
            raise ValueError("Subí una foto o un video de la modelo.")
        d = self._dir(cid)
        for viejo in d.glob("modelo*.*"):
            viejo.unlink(missing_ok=True)
        p = d / f"modelo-{int(time.time() * 1000)}{Path(nombre).suffix.lower()}"
        p.write_bytes(datos)
        self._datos(cid, modelo=p.name, modelo_origen=Path(nombre).name)
        if auto:
            self._auto(cid)
        return p.name

    def modelo_de(self, cid, ruta):
        """Usa una foto/video que ya está en Material como modelo."""
        src = Path(ruta)
        if not src.is_file():
            raise ValueError("Ese archivo ya no está.")
        return self.subir_modelo(cid, src.name, src.read_bytes())

    def subir_audio(self, cid, nombre, datos):
        ext = Path(nombre).suffix.lower()
        if ext not in material.AUDIOS and not medios.es_video(nombre):
            raise ValueError("Subí un audio (mp3, m4a, wav) o un video del que se usa el sonido.")
        d = self._dir(cid)
        for viejo in d.glob("audio*.*"):
            viejo.unlink(missing_ok=True)
        p = d / f"audio-{int(time.time() * 1000)}{ext}"
        p.write_bytes(datos)
        self._datos(cid, audio="musica", audio_archivo=p.name, audio_nombre=Path(nombre).stem[:80], audio_desde=0)
        return p.name

    def audio_de(self, cid, ruta, nombre=""):
        src = Path(ruta)
        if not src.is_file():
            raise ValueError("Ese audio ya no está.")
        n = self.subir_audio(cid, src.name, src.read_bytes())
        if nombre:
            self._datos(cid, audio_nombre=str(nombre)[:80])
        return n

    # --- ubicación automática del texto (sin tapar la cara ni lo que tapa Instagram) -----------------------
    def _caras(self, cid, c):
        """Caras de la modelo (en fracciones del video final, ya recortado como el ejemplo). Se guardan en caché."""
        d = self._dir(cid)
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
                    imgs.append(Image.open(f).convert("RGB"))
                    imgs[-1].load()
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
                for x, y, bw, bh in cajas:  # margen: pelo arriba, mentón abajo
                    x, y, bw, bh = (float(v) for v in (x, y, bw, bh))
                    caras.append([round(max(0, (x - bw * 0.12) / ww), 4), round(max(0, (y - bh * 0.3) / hh), 4),
                                  round(min(1, (x + bw * 1.12) / ww), 4), round(min(1, (y + bh * 1.35) / hh), 4)])
        except Exception:
            caras = []
        caras = _juntar(caras)
        self._datos(cid, caras=caras, caras_clave=clave)
        return caras

    def ubicar(self, cid, campos=None):
        """Busca la altura (y si hace falta un tamaño más chico) donde el texto no tapa la cara ni lo que tapa Instagram,
        lo más cerca posible de donde estaba en el ejemplo."""
        c = self._datos(cid)
        if not c.get("modelo"):
            return {"caras": []}
        c = {**c, **self._limpiar_campos(campos or {}, c.get("duracion"))}
        caras = self._caras(cid, c)
        w, h = int(c.get("ancho") or 720), int(c.get("alto") or 1280)
        fuente_p = FUENTES_DIR / FUENTES.get(c["fuente"], FUENTES["clasica"])[0]
        if not fuente_p.exists():
            fuente_p = FUENTES_DIR / "segoeuib.ttf"
        from PIL import ImageFont
        caja = c["estilo"] == "caja"
        preferida = float(c.get("y_ejemplo") if c.get("y_ejemplo") is not None else 0.17)
        tam = float(c["texto_tam"])
        mejor = None
        for _ in range(10):
            px = max(8, int(h * tam * ESCALA_TEXTO))
            lineas = [l for l in _renglones(c.get("texto"), fuente_p, px, w * ANCHO_MAX)] or [""]
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
            tam = max(0.026, tam * 0.9)  # no entra sin tapar: probar con letra más chica
        if not mejor:
            return {"caras": caras}
        return {"texto_y": mejor[1], "texto_x": 0.5, "texto_tam": mejor[2], "tapa_cara": bool(mejor[3] > 0), "caras": caras}

    # --- 3) armar ----------------------------------------------------------------------------------------
    def armar(self, cid, campos, juzgar=False):
        """juzgar: lo armó la fábrica sola, así que pasa por el juez (si lo ajusta ella a mano, no)."""
        c = self._datos(cid)
        if not c.get("ejemplo"):
            raise ValueError("Todavía no está el video de ejemplo.")
        if not c.get("modelo"):
            raise ValueError("Subí primero la foto o el video de tu modelo.")
        cambios = self._limpiar_campos(campos, c.get("duracion"))
        if cambios.get("ubicacion_auto", c.get("ubicacion_auto")) and not c.get("pegar") and not c.get("chat") and not c.get("noti"):   # con captura, se copia el armado del ejemplo
            u = self.ubicar(cid, {**campos, **cambios})
            for k in ("texto_y", "texto_x", "texto_tam"):
                if k in u:
                    cambios[k] = u[k]
        if cambios.get("audio", c.get("audio")) == "musica" and not c.get("audio_archivo"):
            raise ValueError("Elegí o subí la música primero (o cambiá el audio).")
        if not juzgar:
            cambios["juez"] = {}
        self._datos(cid, estado="armando", error="", juzgar=juzgar, **cambios)
        threading.Thread(target=self._armar, args=(cid,), daemon=True).start()

    def _afinar(self, ejemplo, b, w, h, dur):
        """Recorte exacto de la captura: compara un cuadro con la captura y uno de antes de que aparezca, y se queda
        solo con lo que cambió (así no se cuela pelo ni fondo por los costados). Si no se puede, deja el recuadro."""
        aparece = _num(b.get("aparece"), 0, 0, dur)
        if aparece < 0.15:
            return b
        try:
            import numpy as np
            from PIL import Image
            tmp = self.raiz / "_afinar"
            tmp.mkdir(exist_ok=True)
            t = _num(b.get("cuadro"), dur * 0.5, 0, max(0.0, dur - 0.1))
            if not (medios.cuadro(ejemplo, max(0.0, aparece - 0.15), tmp / "a.png", w) and medios.cuadro(ejemplo, t, tmp / "b.png", w)):
                return b
            a = np.asarray(Image.open(tmp / "a.png").convert("L"), dtype=np.int16)
            c = np.asarray(Image.open(tmp / "b.png").convert("L"), dtype=np.int16)
            H, W = a.shape
            # buscar dentro del recuadro de Claude, un poco agrandado
            x0, x1 = max(0, int(W * (_num(b.get("x0"), 0, 0, 1) - 0.04))), min(W, int(W * (_num(b.get("x1"), 1, 0, 1) + 0.04)))
            y0, y1 = max(0, int(H * (_num(b.get("y0"), 0, 0, 1) - 0.04))), min(H, int(H * (_num(b.get("y1"), 1, 0, 1) + 0.04)))
            m = np.abs(c[y0:y1, x0:x1] - a[y0:y1, x0:x1]) > 22
            filas, cols = np.where(m.mean(axis=1) > 0.35)[0], np.where(m.mean(axis=0) > 0.35)[0]
            if len(filas) < 10 or len(cols) < 10:
                return b
            borde = 2   # unos píxeles para adentro: nada del borde de atrás
            nx0, nx1 = (x0 + cols[0] + borde) / W, (x0 + cols[-1] + 1 - borde) / W
            ny0, ny1 = (y0 + filas[0] + borde) / H, (y0 + filas[-1] + 1 - borde) / H
            if nx1 - nx0 < 0.05 or ny1 - ny0 < 0.03:
                return b
            return {**b, "x0": round(nx0, 4), "x1": round(nx1, 4), "y0": round(ny0, 4), "y1": round(ny1, 4)}
        except Exception:
            return b

    def _fondo_claro(self, modelo, c, w, h, corte):
        """¿El fondo justo detrás del texto es claro? (blanco sobre blanco no se lee en muchas pantallas)."""
        try:
            from PIL import Image
            if medios.es_foto(modelo):
                im = medios._abrir_imagen(modelo).convert("L")
            else:
                tmp = self._dir(c["id"]) / "_fondo.jpg"
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
            px = list(caja.getdata())
            claros = sum(1 for v in px if v > 170) / max(1, len(px))
            return claros > 0.25
        except Exception:
            return False

    def _texto_filtros(self, c, d, w, h):
        tam = int(h * float(c["texto_tam"]) * ESCALA_TEXTO)
        archivo = FUENTES.get(c["fuente"], FUENTES["clasica"])[0]
        fuente_p = FUENTES_DIR / archivo
        if not fuente_p.exists():
            fuente_p = FUENTES_DIR / "segoeuib.ttf"
        lineas = _renglones(c.get("texto"), fuente_p, tam, w * ANCHO_MAX)
        color = COLORES.get(c["color"], "ffffff")
        oscuro = color == "111111"
        estilo = c["estilo"]
        pad = int(tam * CAJA_PAD)
        alto_linea = int(tam * INTERLINEA) + (pad if estilo == "caja" else 0)
        y0, cx = int(h * float(c["texto_y"])), float(c["texto_x"])
        f_esc = str(fuente_p).replace("\\", "/").replace(":", "\\:")
        filtros = []
        for i, linea in enumerate(lineas):
            if not linea:
                continue
            (d / f"_t{i}.txt").write_text(linea, encoding="utf-8")
            f = (f"drawtext=fontfile='{f_esc}':textfile=_t{i}.txt:expansion=none:fontcolor=0x{color}:fontsize={tam}:"
                 f"x='max({pad + 4},min(w*{cx:.4f}-tw/2,w-tw-{pad + 4}))':y={y0 + i * alto_linea + (pad if estilo == 'caja' else 0) + int(tam * 0.95)}-ascent")   # todos los renglones sobre la misma línea de base (los acentos no los corren)
            if c.get("_sombra") and estilo == "nada":   # fondo claro: sombra suave para que se lea en cualquier pantalla
                f += f":shadowcolor=black@0.35:shadowx=0:shadowy={max(1, tam // 30)}"   # apenas, sin borde (como Instagram)
            if estilo == "sombra":
                f += f":shadowcolor={'white@0.55' if oscuro else 'black@0.75'}:shadowx=0:shadowy={max(2, tam // 18)}"
            elif estilo == "caja":
                f += f":box=1:boxcolor={'white@0.95' if oscuro else 'black@0.85'}:boxborderw={pad}"
            elif estilo == "contorno":
                f += f":borderw={max(2, tam // 14)}:bordercolor={'white' if oscuro else 'black'}"
            filtros.append(f)
        return filtros

    def _armar(self, cid):
        with self._armando:
            self._armar_ya(cid)

    def _armar_ya(self, cid):
        d = self._dir(cid)
        c = self._datos(cid)
        self.log("editando", f"Editando el reel «{(c.get('texto') or '').splitlines()[0][:60] if c.get('texto') else cid}»",
                 img=self.rel(d / c["modelo"]) if c.get("modelo") else "", copia=cid, cuenta=c.get("cuenta", ""))
        ejemplo, modelo = d / c["ejemplo"], d / c["modelo"]
        try:
            w, h, dur, audio_ej = _info_video(ejemplo)
            corte = min(float(c["corte"]), dur)
            es_foto = medios.es_foto(modelo)
            audio_mod = (not es_foto) and _info_video(modelo)[3]
            entradas = ["-i", str(ejemplo), "-i", str(modelo)]
            if not es_foto and _info_video(modelo)[2] < corte - 0.1:
                # el video de la modelo es más corto que su parte: ida y vuelta en loop (tipo GIF), nunca congelado
                pp = d / "_idayvuelta.mp4"
                r = subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-i", str(modelo), "-filter_complex",
                                    f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,"
                                    "split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-an",
                                    "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p", str(pp)],
                                   capture_output=True, text=True, errors="ignore", timeout=300, creationflags=medios.SIN_VENTANA)
                if not r.returncode and pp.is_file():
                    entradas = ["-i", str(ejemplo), "-stream_loop", "-1", "-i", str(pp)]
                    audio_mod = False
            if es_foto:  # la foto queda fija, sin zoom ni efectos (como los reels buenos)
                base = (f"[1:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                        f"loop=loop=-1:size=1:start=0,setpts=N/30/TB,fps=30,trim=0:{corte:.2f},setpts=PTS-STARTPTS")
            else:
                base = (f"[1:v]trim=0:{corte:.2f},setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase,"
                        f"crop={w}:{h},fps=30,setsar=1,tpad=stop_mode=clone:stop_duration={corte:.2f},trim=0:{corte:.2f}")
            # capturas del ejemplo (chat de WhatsApp, etc.): se recortan del ejemplo y se pegan igual encima
            pegar = [self._afinar(ejemplo, b, w, h, dur) for b in (c.get("pegar") or []) if isinstance(b, dict)]
            if pegar:
                base += "[m0];"
                for k, b in enumerate(pegar):
                    x0, y0 = int(w * _num(b.get("x0"), 0, 0, 1)), int(h * _num(b.get("y0"), 0, 0, 1))
                    cw_, ch_ = max(2, int(w * _num(b.get("x1"), 1, 0, 1)) - x0), max(2, int(h * _num(b.get("y1"), 1, 0, 1)) - y0)
                    # una sola imagen fija, de un momento en que la captura se ve completa (nunca lo que se mueve atrás)
                    t = _num(b.get("cuadro"), min(dur * 0.5, max(0.0, corte - 0.3)), 0, max(0.0, dur - 0.1))
                    aparece = 0   # siempre visible, desde el primer cuadro
                    base += (f"[0:v]trim=start={t:.2f},setpts=PTS-STARTPTS,scale={w}:{h},fps=30,setsar=1,"
                             f"crop={cw_}:{ch_}:{x0}:{y0},trim=end_frame=1,loop=loop=-1:size=1:start=0,"
                             f"setpts=N/30/TB,trim=0:{corte:.2f}[p{k}];"
                             f"[m{k}][p{k}]overlay={x0}:{int(h * _num(b.get('a_y0'), y0 / h, 0, 1))}:enable='gte(t,{aparece:.2f})'[m{k + 1}];")
                base += f"[m{len(pegar)}]null"
            # notificación de Instagram dibujada (ej. "Respondió a tu historia: …"), siempre visible
            nt = c.get("noti") if isinstance(c.get("noti"), dict) else None
            if nt and nt.get("texto"):
                nx0, nx1 = _num(nt.get("x0"), 0.12, 0, 1), _num(nt.get("x1"), 0.88, 0, 1)
                chat.notificacion(nt["texto"], max(60, int(w * (nx1 - nx0))), d / "_noti.png", nt.get("titulo") or "Instagram")
                ni = entradas.count("-i")
                entradas += ["-loop", "1", "-i", str(d / "_noti.png")]
                base += (f"[nb];[{ni}:v]format=rgba,trim=0:{corte:.2f},setpts=PTS-STARTPTS[noti];"
                         f"[nb][noti]overlay={int(w * nx0)}:{int(h * _num(nt.get('y0'), 0.25, 0, 1))},null")
            # chat de WhatsApp dibujado en alta calidad (mejor que recortarlo del ejemplo)
            ch = c.get("chat") if isinstance(c.get("chat"), dict) else None
            if ch and ch.get("mensajes"):
                cx0, cx1 = _num(ch.get("x0"), 0.18, 0, 1), _num(ch.get("x1"), 0.82, 0, 1)
                cy0 = int(h * _num(ch.get("y0"), 0.6, 0, 1))
                cw_, chh = chat.dibujar(ch["mensajes"], max(60, int(w * (cx1 - cx0))), d / "_chat.png")
                ni = entradas.count("-i")
                entradas += ["-loop", "1", "-i", str(d / "_chat.png")]
                base += (f"[cb];[{ni}:v]format=rgba,trim=0:{corte:.2f},setpts=PTS-STARTPTS[chat];"
                         f"[cb][chat]overlay={int(w * cx0)}:{cy0},null")
            # textos sueltos (ej. "Yo por dentro" / "Yo por fuera" arriba de cada dibujito): letra de Instagram, centrados en x
            f_cl = str(FUENTES_DIR / FUENTES["clasica"][0]).replace("\\", "/").replace(":", "\\:")
            for k, tx in enumerate(t for t in (c.get("textos") or []) if isinstance(t, dict) and t.get("texto")):
                (d / f"_x{k}.txt").write_text(str(tx["texto"]), encoding="utf-8")
                tz = int(h * _num(tx.get("tam"), 0.03, 0.015, 0.08) * ESCALA_TEXTO)
                base += (f",drawtext=fontfile='{f_cl}':textfile=_x{k}.txt:expansion=none:fontcolor=white:fontsize={tz}:"
                         f"x='w*{_num(tx.get('x'), 0.5, 0, 1):.4f}-tw/2':y={int(h * _num(tx.get('y'), 0.1, 0, 1)) + int(tz * 0.95)}-ascent")
            c["_sombra"] = self._fondo_claro(modelo, c, w, h, corte)
            for f in self._texto_filtros(c, d, w, h):
                base += "," + f
            sigue = dur - corte > 0.2
            # marcas de agua del remate (TikTok, CapCut, @usuario): Instagram baja el alcance si las ve.
            # Lejos del borde: delogo. Pegadas al borde: un zoom chiquito al remate las deja afuera (sin parches).
            delogos, zoom, tapas = "", 1.0, []
            for m in (c.get("marcas") or []):
                if not isinstance(m, dict):
                    continue
                fx0, fx1 = _num(m.get("x0"), 0, 0, 1), _num(m.get("x1"), 0, 0, 1)
                fy0, fy1 = _num(m.get("y0"), 0, 0, 1), _num(m.get("y1"), 0, 0, 1)
                if fx0 > 0.01 and fy0 > 0.01 and fx1 < 0.99 and fy1 < 0.99:
                    # desenfoque suave de esa zona (delogo deja rayas): se arma después del scale
                    tapas.append((int(w * fx0) // 2 * 2, int(h * fy0) // 2 * 2,
                                  max(8, int(w * (fx1 - fx0))) // 2 * 2, max(8, int(h * (fy1 - fy0))) // 2 * 2))
                    continue
                borde = max(fx1 if fx0 <= 0.01 else 0, 1 - fx0 if fx1 >= 0.99 else 0,
                            fy1 if fy0 <= 0.01 else 0, 1 - fy0 if fy1 >= 0.99 else 0)
                zoom = max(zoom, 1 / max(0.6, 1 - 2 * (borde + 0.01)))
            remate = f"[0:v]trim=start={corte:.2f},setpts=PTS-STARTPTS{delogos},scale={w}:{h},setsar=1"
            for k, (bx, by, bw, bh) in enumerate(tapas):
                rl, rc = max(1, min(bw, bh) // 3), max(1, min(bw, bh) // 6)
                remate += (f"[rr{k}];[rr{k}]split[ra{k}][rb{k}];[rb{k}]crop={bw}:{bh}:{bx}:{by},"
                           f"boxblur=luma_radius={rl}:luma_power=5:chroma_radius={rc}:chroma_power=5[rbl{k}];"
                           f"[ra{k}][rbl{k}]overlay={bx}:{by}")
            rr = c.get("recorte_remate") if isinstance(c.get("recorte_remate"), dict) else None
            if rr:   # recortar el remate (sacar bandas negras / marcas de agua) y que llene la pantalla
                rx0, ry0 = int(w * _num(rr.get("x0"), 0, 0, 1)) // 2 * 2, int(h * _num(rr.get("y0"), 0, 0, 1)) // 2 * 2
                rw = max(16, int(w * (_num(rr.get("x1"), 1, 0, 1) - _num(rr.get("x0"), 0, 0, 1)))) // 2 * 2
                rh = max(16, int(h * (_num(rr.get("y1"), 1, 0, 1) - _num(rr.get("y0"), 0, 0, 1)))) // 2 * 2
                remate += f",crop={rw}:{rh}:{rx0}:{ry0},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
            if zoom > 1.0:
                zw, zh = int(w * zoom) // 2 * 2, int(h * zoom) // 2 * 2
                remate += f",scale={zw}:{zh},crop={w}:{h}"
            remate += ",fps=30,setsar=1"
            filtro = base + ("[a];" + f"{remate}[b];"
                             "[a][b]concat=n=2:v=1:a=0[v]" if sigue else "[v]")
            # audio
            fmt = "aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo"
            mapa_audio = []
            modo = c["audio"]
            if modo == "modelo" and not audio_mod:
                modo = "ejemplo"
            if modo == "ejemplo" and audio_ej:
                mapa_audio = ["-map", "0:a"]
            elif modo == "musica" and c.get("audio_archivo") and (d / c["audio_archivo"]).is_file():
                na = entradas.count("-i")
                entradas += ["-ss", f"{float(c.get('audio_desde') or 0):.2f}", "-i", str(d / c["audio_archivo"])]
                filtro += f";[{na}:a]{fmt},apad[aout]"
                mapa_audio = ["-map", "[aout]"]
            elif modo == "modelo":
                filtro += f";[1:a]atrim=0:{corte:.2f},asetpts=PTS-STARTPTS,{fmt},apad=whole_dur={corte:.2f}[ma]"
                if sigue and audio_ej:
                    filtro += f";[0:a]atrim=start={corte:.2f},asetpts=PTS-STARTPTS,{fmt}[ea];[ma][ea]concat=n=2:v=0:a=1[aout]"
                else:
                    filtro += ";[ma]anull[aout]"
                mapa_audio = ["-map", "[aout]"]
            salida = d / f"resultado-{int(time.time())}.mp4"
            cmd = [medios.ffmpeg(), "-y", "-v", "error", *entradas, "-filter_complex", filtro, "-map", "[v]", *mapa_audio]
            if mapa_audio:
                cmd += ["-c:a", "aac", "-b:a", "160k"]
            cmd += ["-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-t", f"{dur:.2f}",
                    "-movflags", "+faststart", str(salida)]
            r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, errors="ignore", timeout=600,
                               creationflags=medios.SIN_VENTANA)
            for t in list(d.glob("_t*.txt")) + list(d.glob("_x*.txt")):
                t.unlink(missing_ok=True)
            if r.returncode or not salida.is_file():
                raise RuntimeError(r.stderr.strip()[-200:] or "ffmpeg falló")
            vs = (c.get("versiones") or []) + [{"archivo": salida.name, "armado": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                 "texto": c.get("texto", "")[:120]}]
            for v in vs[:-MAX_VERSIONES]:
                (d / v["archivo"]).unlink(missing_ok=True)
            self._datos(cid, estado="lista", resultado=salida.name, armado=vs[-1]["armado"], versiones=vs[-MAX_VERSIONES:])
            self.log("listo", "Reel armado: pasa al juez", img=self.rel(salida), copia=cid, cuenta=c.get("cuenta", ""))
            self.a_resultados(cid)
            if c.get("juzgar"):
                self._al_juez(cid, salida, dur)
        except Exception as ex:
            self._datos(cid, estado="lista", error=f"No se pudo armar: {str(ex)[:180]}")

    # --- 4) el juez: Claude mira el resultado; si no sirve (nota < NOTA_MIN) lo rehace solo ----------------
    def _al_juez(self, cid, video, dur):
        from PIL import Image, ImageDraw
        d = self._dir(cid)
        tiempos = [0.05] + [round(dur * (i + 1) / 8, 2) for i in range(6)] + [round(max(0.1, dur - 0.15), 2)]   # del primer al último cuadro
        tmp = d / "_rev"
        tmp.mkdir(exist_ok=True)
        cw = 240
        hoja = None
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
            for z in (IG_ARRIBA, IG_ABAJO):   # zonas que tapa Instagram
                dr.line((x, y + int(ch * z), x + cw, y + int(ch * z)), fill="red", width=1)
            dr.rectangle((x, y, x + 46, y + 18), fill="black")
            dr.text((x + 4, y + 4), f"{t:g}s", fill="yellow")
        shutil.rmtree(tmp, ignore_errors=True)
        if hoja is None:
            return
        hoja.save(d / "revision.jpg", quality=82)
        c = self._datos(cid)
        juez = {**(c.get("juez") or {}), "estado": "mirando"}
        self._datos(cid, juez=juez, revision="revision.jpg")
        fotos_hoja, _ = self._candidatas(c.get("cuenta") or "")
        self.pedido(f"juez · {cid}. Mirá {d / 'revision.jpg'} (8 cuadros del reel terminado; las líneas rojas marcan lo "
                    f"que tapa Instagram arriba y abajo) y compará con {d / 'hoja.jpg'} (el ejemplo). Escribí "
                    f"{d / 'juicio.json'} con DOS notas de edición 1-5 (menos de {NOTA_MIN} = no sirve), sé muy exigente: "
                    "nota_texto (¿tapa la cara o cae en zona roja? ¿se lee? ¿cortado? ¿la foto tiene sentido con el chiste?) y "
                    "nota_tecnica (mirá cada cuadro, también el primero: ¿se cuela algo del video original (pelo, bordes, "
                    "fondo) alrededor de capturas o recortes? ¿borroso, pixelado, píxeles rotos, negro, cortes raros, "
                    "barras?). Si dudás de un detalle, extraé ese cuadro a mayor tamaño y miralo. "
                    "motivo (una línea), y si la nota es baja arreglo: \"otra_foto\" (con foto = número de "
                    f"{fotos_hoja} si querés una en particular) o \"texto\" (con texto_y 0-1 donde va mejor) o \"musica\" (si el audio no sirve: habla alguien, se escucha mal o no pega). "
                    "Sin agente y sin contestar en el chat.", {"tipo_pedido": "juez", "copia": cid})

    def _juzgado(self, cid, j):
        c = self._datos(cid)
        juez = c.get("juez") or {}
        # dos jueces de edición: texto/cara/zonas y técnico (recortes, bordes, píxeles, nitidez). Manda la peor nota.
        notas = [int(_num(j.get(k), 0, 0, 5)) for k in ("nota_texto", "nota_tecnica", "nota") if j.get(k) not in (None, "")]
        nota = min(notas) if notas else 0
        intentos = int(juez.get("intentos") or 0)
        juez.update(nota=nota, motivo=str(j.get("motivo") or "")[:200])
        if nota >= NOTA_MIN:
            self.log("aprobado", f"El juez aprobó el reel ({nota}/5)", img=self.rel(self.resultado(cid)), copia=cid)
            return self._datos(cid, juez={**juez, "estado": "aprobado"})
        if intentos >= REINTENTOS:
            return self._datos(cid, juez={**juez, "estado": "rechazado"})
        self.log("rehaciendo", f"El juez lo tachó ({nota}/5): {juez.get('motivo', '')[:80]} — rehaciéndolo", copia=cid)
        self._datos(cid, juez={**juez, "estado": "rehaciendo", "intentos": intentos + 1})
        campos = {}
        if j.get("arreglo") == "musica":   # el audio no sirve (habla la chica, se escucha mal): otra música guardada
            import random
            opciones = [m for m in self.musicas(c.get("cuenta") or "") if f"copia-{cid}" not in Path(m).name]
            if opciones:
                self.audio_de(cid, random.choice(opciones))
                self.log("musica", "El juez pidió otra música: le pongo una de las guardadas", copia=cid)
                return self.armar(cid, {"audio": "musica"}, juzgar=True)
        if j.get("arreglo") == "texto" and j.get("texto_y") not in (None, ""):
            campos = {"texto_y": j["texto_y"], "ubicacion_auto": False}
        else:   # otra foto: la que diga el juez o la menos usada que no sea la actual
            _, fotos = self._candidatas(c.get("cuenta") or "")
            cual = j.get("foto")
            if cual in (None, "") and fotos:
                usadas = self._usadas(c.get("cuenta") or "")
                libres = [f.name for f, _ in fotos if f.name != c.get("modelo_origen")]
                cual = min(libres, key=lambda n: usadas.get(n, 0)) if libres else ""
            if cual not in (None, ""):
                self._elegir_foto(cid, c["cuenta"], cual)
            campos = {"ubicacion_auto": True}
        self.armar(cid, campos, juzgar=True)

    # --- carpeta "Resultados": cada video listo con un nombre que se entiende (para mandarlos por WhatsApp) -----
    def nombre_lindo(self, c):
        texto = " ".join(str(c.get("texto") or "").split()) or " ".join(t.get("texto", "") for t in c.get("textos") or [])
        texto = re.sub(r'[\\/:*?"<>|“”«»\n]+', "", texto).strip(" .") or f"video {c['id']}"
        if len(texto) > 60:
            texto = texto[:60].rsplit(" ", 1)[0] + "…"
        quien = c.get("cuenta") or "sin modelo"
        return f"{quien} - {texto} ({c['id'][:4]}).mp4"

    def a_resultados(self, cid):
        """Copia el último resultado a Resultados/ con nombre lindo (si se rehace, reemplaza el anterior)."""
        try:
            c = self._datos(cid)
            src = self.resultado(cid)
        except ValueError:
            return None
        self.resultados.mkdir(parents=True, exist_ok=True)
        for viejo in self.resultados.glob(f"* ({cid[:4]}).mp4"):
            viejo.unlink(missing_ok=True)
        dst = self.resultados / self.nombre_lindo(c)
        shutil.copy2(src, dst)
        return dst

    def resultado(self, cid, archivo=""):
        c = self._datos(cid)
        nombre = Path(archivo).name if archivo else c.get("resultado")
        if not nombre or not (self._dir(cid) / nombre).is_file():
            raise ValueError("Todavía no armaste el video.")
        return self._dir(cid) / nombre
