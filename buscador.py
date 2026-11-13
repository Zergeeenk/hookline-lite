"""Buscador de memes: recorre Instagram/TikTok en una ventana de Chrome propia (con la sesión de una cuenta para
mirar), junta candidatos con su miniatura y le pide a Claude que deje solo los que sirven. Lo que pasa va al banco."""
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

PUERTO = 9333
POR_TANDA = 40          # miniaturas por hoja que mira Claude
MAX_CANDIDATOS = 400    # por búsqueda
FUENTES_POR_VEZ = 12    # cuántas fuentes recorre por vuelta (primero las nuevas, después las que más dieron)
MAX_VUELTAS = 5         # da vueltas sola hasta llegar al objetivo, pero no más que esto (para no abusar)
MUERTA = 60             # una fuente con 60 vistos y ningún bueno se deja de usar
CHROMES = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
           Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
           Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"]


def _abierto():
    try:
        with socket.create_connection(("127.0.0.1", PUERTO), timeout=1):
            return True
    except OSError:
        return False


def _link(href, red):
    if red == "ig":
        m = re.search(r"/(?:reel|p)/([\w-]+)", href)
        return f"https://www.instagram.com/reel/{m.group(1)}/" if m else ""
    m = re.search(r"/@([\w.]+)/video/(\d+)", href)
    return f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}" if m else ""


def fuentes(archivo):
    """buscador/fuentes.txt: una por renglón. "ig: frase", "ig #hashtag", "ig @cuenta", "tt: frase", "tt @cuenta"."""
    out = []
    for r in Path(archivo).read_text(encoding="utf-8").splitlines() if Path(archivo).exists() else []:
        r = r.strip()
        m = re.match(r"(ig|tt)\s*[:]?\s*(.+)", r, re.I)
        if not m or r.startswith("#"):
            continue
        red, q = m.group(1).lower(), m.group(2).strip()
        if red == "ig":
            url = (f"https://www.instagram.com/{q[1:]}/reels/" if q.startswith("@") else
                   f"https://www.instagram.com/explore/tags/{urllib.parse.quote(q[1:])}/" if q.startswith("#") else
                   f"https://www.instagram.com/explore/search/keyword/?q={urllib.parse.quote(q)}")
        else:
            url = (f"https://www.tiktok.com/{q}" if q.startswith("@") else
                   f"https://www.tiktok.com/search/video?q={urllib.parse.quote(q.lstrip('#'))}")
        out.append((red, q, url))
    return out


def clave(red, q):
    return f"{red} {q}".lower()


class Buscador:
    def __init__(self, raiz, perfil, archivo_fuentes, leer, escribir, pedido, cop):
        self.raiz, self.perfil, self.archivo_fuentes = Path(raiz), Path(perfil), Path(archivo_fuentes)
        self.leer, self.escribir, self.pedido, self.cop = leer, escribir, pedido, cop
        self._lock = threading.Lock()
        self._visible = True   # si ya había uno abierto al arrancar, no sabemos cómo: se trata como visible

    def al_arrancar(self):
        """Solo cuando arranca el panel: si quedó una búsqueda a medias (se cerró en el medio), se puede volver a buscar."""
        if self.estado().get("estado") == "buscando":
            self.estado(estado="error", mensaje="La búsqueda se cortó (se reinició el panel). Tocá «Buscar memes» de nuevo.")

    # --- estado ------------------------------------------------------------------------------------------
    def _p(self):
        return self.raiz / "_busqueda.json"

    def estado(self, **cambios):
        with self._lock:
            e = self.leer(self._p(), None) or {"estado": "parado", "mensaje": "", "giro": 0}
            if cambios:
                e.update(cambios)
                self.raiz.mkdir(parents=True, exist_ok=True)
                self.escribir(self._p(), e)
            return e

    # --- la ventana --------------------------------------------------------------------------------------
    def exportar_cookies(self, destino):
        """Guarda la sesión de Instagram/TikTok del buscador en cookies.txt, para bajar reels con el mismo login."""
        from playwright.sync_api import sync_playwright
        self.abrir()
        with sync_playwright() as p:
            cs = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PUERTO}").contexts[0].cookies()
        renglones = ["# Netscape HTTP Cookie File"]
        for c in cs:
            if not any(k in c["domain"] for k in ("instagram", "tiktok")):
                continue
            renglones.append("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"],
                                         "TRUE" if c["secure"] else "FALSE", str(int(max(0, c["expires"]))),
                                         c["name"], c["value"]]))
        Path(destino).write_text("\n".join(renglones) + "\n", encoding="utf-8")
        return len(renglones) - 1

    def _cerrar(self):
        """Cierra el Chrome del buscador (para cambiarlo de oculto a visible o al revés)."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                p.chromium.connect_over_cdp(f"http://127.0.0.1:{PUERTO}").close()
        except Exception:
            pass
        for _ in range(20):
            if not _abierto():
                break
            time.sleep(0.5)

    def abrir(self, urls=(), visible=False):
        """Por defecto, oculto (sin ventana: ella sigue usando la compu tranquila). Visible solo para iniciar sesión."""
        if _abierto():
            if not visible and not self._visible:
                return True
            self._cerrar()
        chrome = next((c for c in CHROMES if c.exists()), None)
        if not chrome:
            raise ValueError("No encontré Google Chrome en esta compu: instalalo y probá de nuevo.")
        self.perfil.mkdir(parents=True, exist_ok=True)
        modo = ["--new-window", *(urls or ["https://www.instagram.com/"])] if visible else \
            ["--headless=new", "--window-size=1280,900", "--mute-audio", "about:blank"]
        subprocess.Popen([str(chrome), f"--user-data-dir={self.perfil}", f"--remote-debugging-port={PUERTO}",
                          "--no-first-run", *modo], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._visible = visible
        for _ in range(30):
            if _abierto():
                return True
            time.sleep(0.5)
        raise ValueError("No pude abrir la ventana del buscador.")

    # --- buscar ------------------------------------------------------------------------------------------
    def buscar(self, objetivo=30):
        if self.estado()["estado"] in ("buscando", "filtrando"):
            raise ValueError("Ya está buscando: esperá a que termine.")
        if not fuentes(self.archivo_fuentes):
            raise ValueError("No hay nada para buscar en buscador/fuentes.txt.")
        self.estado(estado="buscando", mensaje="Abriendo el navegador…", juntados=0, nuevos=0, error="",
                    objetivo=max(1, int(objetivo or 30)), vuelta=1, acumulado=0)
        threading.Thread(target=self._buscar, daemon=True).start()

    # --- qué fuentes dan buenos (aprende solo) -----------------------------------------------------------
    def _stats(self, cambiar=None):
        p = self.raiz / "_fuentes_stats.json"
        with self._lock:
            st = self.leer(p, None) or {}
            if cambiar:
                cambiar(st)
                self.escribir(p, st)
            return st

    def _elegir(self):
        st = self._stats()
        def muerta(f):
            x = st.get(clave(f[0], f[1]), {})
            return x.get("vistos", 0) >= MUERTA and not x.get("sirven")
        def puntaje(f):   # primero las que nunca probó, después las que más buenos dieron
            x = st.get(clave(f[0], f[1]), {})
            return (1 if x.get("vistos") else 0, 0 if f[1].startswith("@") else 1,
                    -(x.get("sirven", 0) + 1) / (x.get("vistos", 0) + 10))
        return sorted([f for f in fuentes(self.archivo_fuentes) if not muerta(f)], key=puntaje)[:FUENTES_POR_VEZ]

    def sumar_fuentes(self, nuevas):
        """Agrega fuentes a fuentes.txt (las cuentas de los buenos, lo que propone Claude), sin repetir."""
        ya = {clave(r, q) for r, q, _ in fuentes(self.archivo_fuentes)}
        renglones = []
        for n in nuevas:
            m = re.match(r"\s*(ig|tt)\s*:?\s*(.+)", str(n), re.I)
            if not m:
                continue
            red, q = m.group(1).lower(), m.group(2).strip()
            if clave(red, q) in ya:
                continue
            ya.add(clave(red, q))
            renglones.append(f"{red} {q}" if q[0] in "@#" else f"{red}: {q}")
        if renglones:
            with open(self.archivo_fuentes, "a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(renglones) + "\n")
        return len(renglones)

    def _buscar(self):
        try:
            from playwright.sync_api import sync_playwright
            from PIL import Image
            self.abrir()
            lote = time.strftime("%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
            d = self.raiz / "_candidatos" / lote
            d.mkdir(parents=True, exist_ok=True)
            ya = {x["link"] for x in self.cop._banco()}
            cands, sin_sesion = [], set()
            with sync_playwright() as p:
                br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PUERTO}")
                ctx = br.contexts[0]
                pg = ctx.new_page()
                self.estado(mensaje="Mirando de quién son los buenos…")
                self._autores(pg)
                elegidas = self._elegir()
                if not elegidas:
                    pg.close()
                    return self.estado(estado="error", mensaje="Ya probé todas las fuentes y ninguna dio buenos: pasame cuentas que suban este tipo de video.")
                for i, (red, q, url) in enumerate(elegidas):
                    if len(cands) >= MAX_CANDIDATOS:
                        break
                    self.estado(mensaje=f"Buscando «{q}» ({i + 1} de {len(elegidas)})…", juntados=len(cands))
                    try:
                        pg.goto(url, wait_until="domcontentloaded", timeout=40000)
                        pg.wait_for_timeout(4000)
                        if "/login" in pg.url or "accounts/login" in pg.url:
                            sin_sesion.add("Instagram" if red == "ig" else "TikTok")
                            continue
                        sel = "a[href*='/reel/'], a[href*='/p/']" if red == "ig" else "a[href*='/video/']"
                        for _ in range(6):
                            for a in pg.query_selector_all(sel):
                                href = a.get_attribute("href") or ""
                                link = _link(href, red)
                                if not link or link in ya:
                                    continue
                                # en Instagram, solo videos (reels): las fotos y carruseles de memes no sirven
                                if red == "ig" and "/reel/" not in href and not a.query_selector(
                                        "svg[aria-label*='Clip'], svg[aria-label*='Reel'], svg[aria-label*='Video'], svg[aria-label*='clip']"):
                                    continue
                                img = a.query_selector("img")
                                src = img.get_attribute("src") if img else ""
                                if not src:   # en la pestaña de reels de una cuenta, la miniatura es un fondo (CSS)
                                    src = a.evaluate("""el => { for (const n of [el, ...el.querySelectorAll('*')]) {
                                        const b = getComputedStyle(n).backgroundImage;
                                        if (b && b.startsWith('url(')) return b.slice(4, -1).replace(/^["']|["']$/g, ''); }
                                        return ''; }""")
                                if not src:
                                    continue
                                ya.add(link)
                                cands.append({"link": link, "src": src, "alt": ((img.get_attribute("alt") if img else "") or "")[:160],
                                              "fuente": clave(red, q)})
                            pg.mouse.wheel(0, 3000)
                            pg.wait_for_timeout(1200)
                    except Exception:
                        continue
                self.estado(mensaje="Bajando miniaturas…", juntados=len(cands))
                buenos = []
                for c in cands[:MAX_CANDIDATOS]:
                    try:
                        r = pg.request.get(c["src"], timeout=15000)
                        if not r.ok:
                            continue
                        n = len(buenos) + 1
                        (d / f"{n}.img").write_bytes(r.body())
                        im = Image.open(d / f"{n}.img").convert("RGB")
                        im.thumbnail((300, 534))
                        im.save(d / f"{n}.jpg", quality=80)
                        (d / f"{n}.img").unlink(missing_ok=True)
                        buenos.append({**c, "n": n})
                    except Exception:
                        continue
                pg.close()
            aviso = f" {' y '.join(sorted(sin_sesion))} pide iniciar sesión: tocá «Abrir navegador» y entrá." if sin_sesion else ""
            if not buenos:
                shutil.rmtree(d, ignore_errors=True)
                return self.estado(estado="error" if aviso else "listo", fin=time.strftime("%H:%M"),
                                   mensaje=("No encontré nada nuevo." + aviso).strip())
            self.escribir(d / "candidatos.json", buenos)
            hojas = self._hojas(d, buenos)
            self.estado(estado="filtrando", lote=lote, hojas=len(hojas), hechas=0, juntados=len(buenos),
                        mensaje=f"Claude está mirando {len(buenos)} candidatos…")
            for k, (hoja, rango) in enumerate(hojas, 1):
                self.pedido(f"filtrar · {lote} · {k}/{len(hojas)}. Mirá {hoja} (miniaturas numeradas {rango[0]}-{rango[1]} "
                            f"de reels candidatos; los textos están en {d / 'candidatos.json'}). Reglas y gustos: "
                            f"buscador/formatos.md (las \"Reglas que no se rompen\" y \"Lo que aprendí de sus votos\"). "
                            f"Escribí {d / f'filtro-{k}.json'} con {{\"sirven\": [{{\"n\": 3, \"cat\": \"numero\", "
                            "\"titulo\": \"texto en pantalla corto\"}], \"fuentes_nuevas\": [\"ig @cuenta\", \"ig: frase\"]} "
                            "solo con los que sirven (chica claramente adulta en cámara, del formato, con gracia). Ante la "
                            "duda con la edad, afuera. En fuentes_nuevas (opcional, 0-5) proponé cuentas o frases nuevas "
                            "para buscar si ves pistas en las miniaturas. Sin agente y sin contestar en el chat.",
                            {"tipo_pedido": "filtrar", "lote": lote})
        except ValueError as ex:
            self.estado(estado="error", mensaje=str(ex))
        except Exception as ex:
            self.estado(estado="error", mensaje=f"Se cortó la búsqueda: {str(ex)[:150]}")

    def _autores(self, pg):
        """De los reels de Instagram buenos (los que aprobó Claude y los ⭐ de ella) averigua la cuenta y la suma como fuente."""
        sin = [x for x in self.cop._banco() if (x.get("origen") == "buscador" or x.get("voto") in ("oro", "bueno"))
               and "instagram.com" in x["link"] and not x.get("autor")][:15]
        nuevas = []
        for x in sin:
            autor = "?"
            try:
                pg.goto(x["link"], wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(2500)
                og = pg.get_attribute("meta[property='og:url']", "content") or ""
                m = re.search(r"instagram\.com/([\w.]+)/(?:reel|p)/", og) or re.search(r"@([\w.]+)", pg.title() or "")
                if m and m.group(1) not in ("reel", "reels", "p", "explore"):
                    autor = m.group(1)
                    nuevas.append(f"ig @{autor}")
            except Exception:
                pass
            self.cop.poner_autor(x["link"], autor)
        self.sumar_fuentes(nuevas)

    @staticmethod
    def _hojas(d, buenos):
        from PIL import Image, ImageDraw
        out = []
        cw, ch, cols = 200, 300, 10
        for k in range(0, len(buenos), POR_TANDA):
            parte = buenos[k:k + POR_TANDA]
            img = Image.new("RGB", (cw * cols, ch * ((len(parte) + cols - 1) // cols)), "white")
            dr = ImageDraw.Draw(img)
            for i, c in enumerate(parte):
                try:
                    im = Image.open(d / f"{c['n']}.jpg")
                    im.thumbnail((cw - 4, ch - 4))
                except Exception:
                    continue
                x, y = (i % cols) * cw, (i // cols) * ch
                img.paste(im, (x + 2, y + 2))
                dr.rectangle((x, y, x + 34, y + 20), fill="black")
                dr.text((x + 5, y + 4), str(c["n"]), fill="yellow")
            hoja = d / f"hoja-{k // POR_TANDA + 1}.jpg"
            img.save(hoja, quality=82)
            out.append((hoja, (parte[0]["n"], parte[-1]["n"])))
        return out

    # --- lo que eligió Claude ----------------------------------------------------------------------------
    def recoger(self):
        e = self.estado()
        if e.get("estado") != "filtrando" or not e.get("lote"):
            return
        d = self.raiz / "_candidatos" / e["lote"]
        cands = {c["n"]: c for c in (self.leer(d / "candidatos.json", []) or [])}
        hechas, nuevos = int(e.get("hechas") or 0), int(e.get("nuevos") or 0)
        for f in sorted(d.glob("filtro-*.json")):
            fil = self.leer(f, None) or {}
            f.rename(f.with_suffix(".hecho"))
            hechas += 1
            k = int(re.search(r"(\d+)", f.stem).group(1))
            de_hoja = [c for n, c in cands.items() if (k - 1) * POR_TANDA < n <= k * POR_TANDA]
            ok_ns = {int(x.get("n")) for x in fil.get("sirven") or [] if str(x.get("n") or "").isdigit()}
            def tally(st, de_hoja=de_hoja, ok_ns=ok_ns):
                for c in de_hoja:
                    x = st.setdefault(c.get("fuente", "?"), {"vistos": 0, "sirven": 0})
                    x["vistos"] += 1
                    x["sirven"] += 1 if c["n"] in ok_ns else 0
            self._stats(tally)
            nuevas = list(fil.get("fuentes_nuevas") or [])[:5]
            for n in ok_ns:   # quien subió uno bueno suele subir más: su cuenta pasa a ser fuente
                m = re.search(r"tiktok\.com/@([\w.]+)/", (cands.get(n) or {}).get("link", ""))
                if m:
                    nuevas.append(f"tt @{m.group(1)}")
            self.sumar_fuentes(nuevas)
            items = []
            for s in fil.get("sirven") or []:
                c = cands.get(int(s.get("n") or 0)) if str(s.get("n") or "").isdigit() else None
                if c:
                    items.append({"link": c["link"], "titulo": str(s.get("titulo") or c["alt"])[:120],
                                  "cat": str(s.get("cat") or "")[:30], "img": d / f"{c['n']}.jpg"})
            nuevos += self.cop.agregar_encontrados(items)
        if hechas >= int(e.get("hojas") or 0):
            shutil.rmtree(d, ignore_errors=True)
            total = int(e.get("acumulado") or 0) + nuevos
            vuelta, objetivo = int(e.get("vuelta") or 1), int(e.get("objetivo") or 30)
            if total < objetivo and vuelta < MAX_VUELTAS:
                self.estado(estado="buscando", hechas=0, nuevos=0, acumulado=total, vuelta=vuelta + 1,
                            mensaje=f"Vuelta {vuelta + 1} de {MAX_VUELTAS}: llevo {total} de {objetivo}, busco en otros lados…")
                threading.Thread(target=self._buscar, daemon=True).start()
            else:
                vueltas = "1 vuelta" if vuelta == 1 else f"{vuelta} vueltas"
                self.estado(estado="listo", hechas=hechas, nuevos=nuevos, acumulado=total, fin=time.strftime("%H:%M"),
                            mensaje=f"Listo: {total} memes nuevos en {vueltas}.")
        else:
            self.estado(hechas=hechas, nuevos=nuevos, mensaje=f"Claude está mirando… ({hechas} de {e['hojas']} tandas, {nuevos} sirven)")
