"""Buscador de memes. Portado de Hookline Lite (buscador.py).

Recorre Instagram/TikTok con un Chrome/Chromium headless propio (perfil en navegador/, con la sesión de una cuenta
"para mirar"), junta candidatos con su miniatura y le pide a Claude (tarea "filtrar") que deje solo los que sirven.
Lo que pasa va al banco de links de la fábrica.

En el servidor no hay ventana para iniciar sesión: la sesión se carga con importar_cookies(texto) (cookies.txt en
formato Netscape exportado del navegador de la cuenta "para mirar").

Ojo (ver REQUERIMIENTOS §11): recorrer Instagram/TikTok con sesión va contra los términos de esas plataformas y los
selectores se rompen cuando cambian las páginas. Se puede apagar con HOOKLINE_BUSCADOR=0.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import socket
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

from sqlalchemy import select

from .. import almacen
from ..comun import ajuste, candado, guardar_ajuste
from ..config import config
from ..db import sesion
from ..ia import tareas
from ..modelos import FuenteBusqueda, LinkBanco
from ..trabajos import encolar, manejador
from . import fabrica
from .material import video_valido

log = logging.getLogger("hookline.buscador")
PUERTO = 9333
POR_TANDA = 40          # miniaturas por hoja que mira Claude
MAX_CANDIDATOS = 400
FUENTES_POR_VEZ = 12
MAX_VUELTAS = 5
MUERTA = 60             # una fuente con 60 vistos y ningún bueno se deja de usar
_lock_tandas = threading.Lock()   # varias tandas pueden terminar a la vez



def _playwright():
    """Playwright es opcional (pip install "hookline[buscador]"): sin él, el buscador avisa en vez de romperse."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError("El buscador no está instalado en este servidor (falta playwright: "
                         "pip install \"hookline[buscador]\").") from None
    return sync_playwright

def _hora(fmt: str) -> str:
    from datetime import datetime
    from ..config import config
    return datetime.now(config().tz).strftime(fmt)


def _abierto() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PUERTO), timeout=1):
            return True
    except OSError:
        return False


def _link(href: str, red: str) -> str:
    if red == "ig":
        m = re.search(r"/(?:reel|p)/([\w-]+)", href)
        return f"https://www.instagram.com/reel/{m.group(1)}/" if m else ""
    if red == "pin":
        m = re.search(r"/pin/([\w-]{5,120})", href)
        return f"https://www.pinterest.com/pin/{m.group(1)}/" if m else ""
    m = re.search(r"/@([\w.]+)/video/(\d+)", href)
    return f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}" if m else ""


def _url(red: str, q: str) -> str:
    if red == "ig":
        return (f"https://www.instagram.com/{q[1:]}/reels/" if q.startswith("@") else
                f"https://www.instagram.com/explore/tags/{urllib.parse.quote(q[1:])}/" if q.startswith("#") else
                f"https://www.instagram.com/explore/search/keyword/?q={urllib.parse.quote(q)}")
    if red == "pin":                     # Pinterest: la pestaña de videos de la búsqueda, o los pines de una cuenta
        return (f"https://www.pinterest.com/{urllib.parse.quote(q[1:])}/_created/" if q.startswith("@") else
                f"https://www.pinterest.com/search/videos/?q={urllib.parse.quote(q.lstrip('#'))}")
    return (f"https://www.tiktok.com/{q}" if q.startswith("@") else
            f"https://www.tiktok.com/search/video?q={urllib.parse.quote(q.lstrip('#'))}")


NOMBRE_RED = {"ig": "Instagram", "tt": "TikTok", "pin": "Pinterest"}
# La grilla se lee entera dentro del navegador en una sola pasada de JS (sin ElementHandle que queden viejos cuando React
# rearma la página). De cada link: si se ve, si está en pantalla, el contenedor de la tarjeta (se sube desde el <a>
# mientras el padre tenga un solo link de la grilla: el <video> puede estar al lado y no adentro), si hay <video> y si ya
# tiene cuadro, la imagen más grande (no el avatar ni un sprite) y los íconos (aria-label del link y de adentro, y los
# <svg><title>). Instagram hoy muestra los reels como /p/… con un <video> que se reproduce solo y sin <img>.
GRILLA_JS = """([sel]) => {
  const RX = /\\b(clips?|reels?|v[ií]deos?)\\b/i;
  const vh = window.innerHeight || 800;
  const out = [];
  for (const a of document.querySelectorAll(sel)) {
    const href = a.getAttribute('href') || '';
    let r = a.getBoundingClientRect();
    if (r.width * r.height < 1600) {     // un <a> inline mide una línea: vale la caja de lo más grande que tiene adentro
      for (const n of a.querySelectorAll('*')) {
        const b = n.getBoundingClientRect();
        if (b.width * b.height > r.width * r.height) r = b;
      }
    }
    const visible = a.getClientRects().length > 0 && r.width >= 40 && r.height >= 40
      && getComputedStyle(a).visibility !== 'hidden';
    let c = a;
    for (let i = 0; i < 4 && c.parentElement && c.parentElement !== document.body; i++) {
      if (c.parentElement.querySelectorAll(sel).length > 1) break;
      c = c.parentElement;
    }
    const v = c.querySelector('video');
    let vArea = 0;
    if (v) { const b = v.getBoundingClientRect(); vArea = b.width * b.height; }
    let img = null, imgArea = -1, imgW = 0, imgH = 0;
    for (const im of c.querySelectorAll('img')) {
      const b = im.getBoundingClientRect(), s = b.width * b.height;
      if (s > imgArea) { img = im; imgArea = s; imgW = b.width; imgH = b.height; }
    }
    // la miniatura: una imagen de verdad (no un avatar ni un ícono) que no sea mucho más chica que el video de la tarjeta
    const grande = img && ((imgW >= 60 && imgH >= 60) || (img.naturalWidth || 0) >= 150) && !(vArea > 4 * imgArea);
    let src = '', alt = '', imgLista = false;
    if (grande) {
      const u = img.currentSrc || img.getAttribute('src') || '';
      if (/^https?:/.test(u)) {
        src = u; alt = img.getAttribute('alt') || ''; imgLista = img.complete && img.naturalWidth > 1;
      }
    }
    let bg = '';
    if (!src) {
      for (const n of [c, ...c.querySelectorAll('*')]) {
        const b = n.getBoundingClientRect();
        if (b.width < 60 || b.height < 60) continue;
        const m = (getComputedStyle(n).backgroundImage || '').match(/^url\\(["']?([^"')]+)["']?\\)$/);
        if (m && /^https?:/.test(m[1])) { bg = m[1]; break; }
      }
    }
    const textos = [a, ...c.querySelectorAll('[aria-label]')].map(n => n.getAttribute('aria-label') || '')
      .concat([...c.querySelectorAll('svg title')].map(t => t.textContent || '')).filter(Boolean);
    const poster = v ? (v.getAttribute('poster') || '') : '';
    out.push({href, visible, enPantalla: r.bottom > 0 && r.top < vh, area: Math.round(r.width * r.height),
              video: !!v, videoListo: !!v && v.readyState >= 2 && v.videoWidth > 0,
              poster: /^https?:/.test(poster) ? poster : '', src, alt, imgLista, bg,
              icono: textos.some(t => RX.test(t)), etiquetas: textos.join(' | ').slice(0, 120)});
  }
  return out;
}"""
# Antes de capturar una tarjeta sin imagen: esperar (1,5 s como mucho) a que su video tenga cuadro o su imagen cargue.
LISTA_JS = """(a, sel) => new Promise(res => {
  let c = a;
  for (let i = 0; i < 4 && c.parentElement && c.parentElement !== document.body; i++) {
    if (c.parentElement.querySelectorAll(sel).length > 1) break;
    c = c.parentElement;
  }
  const t0 = performance.now();
  (function mirar() {
    const v = c.querySelector('video'), im = c.querySelector('img');
    const ok = (v && v.readyState >= 2 && v.videoWidth > 0) || (im && im.complete && im.naturalWidth > 1);
    if (ok || performance.now() - t0 > 1500) res(!!ok); else setTimeout(mirar, 100);
  })();
})"""
MAX_CAPTURAS = 40          # capturas de tarjetas por fuente (cada una tarda 1-3 s)
MAX_FOTOS_RESERVA = 30     # fotos que se mandan a Claude cuando la página no marca ningún video
SELECTOR = {"ig": "a[href*='/reel/'], a[href*='/p/']", "tt": "a[href*='/video/']", "pin": "a[href*='/pin/']"}


def clave(red: str, q: str) -> str:
    return f"{red} {q}".lower()


# --- estado ----------------------------------------------------------------------------------------------------------------
def estado(**cambios) -> dict:
    e = ajuste("busqueda", None) or {"estado": "parado", "mensaje": "", "giro": 0}
    if cambios:
        e = {**e, **cambios}
        guardar_ajuste("busqueda", e)
    return e


def al_arrancar() -> None:
    if estado().get("estado") == "buscando":
        estado(estado="error", mensaje="La búsqueda se cortó (se reinició el sistema). Tocá «Buscar memes» de nuevo.")


# --- fuentes ---------------------------------------------------------------------------------------------------------------
def sumar_fuentes(nuevas) -> int:
    """Agrega fuentes ("ig @cuenta", "ig: frase", "ig #tag", "tt @cuenta", "tt: frase"), sin repetir."""
    n = 0
    with sesion() as s:
        ya = {clave(f.red, f.consulta) for f in s.scalars(select(FuenteBusqueda))}
        for x in nuevas or []:
            m = re.match(r"\s*(ig|tt|pin)\s*:?\s*(.+)", str(x), re.I)
            if not m or str(x).strip().startswith("#"):
                continue
            red, q = m.group(1).lower(), m.group(2).strip()[:200]
            if clave(red, q) in ya:
                continue
            ya.add(clave(red, q))
            s.add(FuenteBusqueda(red=red, consulta=q))
            n += 1
    return n


def fuentes() -> list[dict]:
    with sesion() as s:
        return [{"id": f.id, "red": f.red, "consulta": f.consulta, "vistos": f.vistos, "sirven": f.sirven,
                 "activa": f.activa, "texto": f"{f.red} {f.consulta}" if f.consulta.startswith(("@", "#"))
                 else f"{f.red}: {f.consulta}"}
                for f in s.scalars(select(FuenteBusqueda).order_by(FuenteBusqueda.id))]


def fuente(fid, activa=None, borrar: bool = False) -> None:
    try:
        fid = int(fid)
    except (TypeError, ValueError):
        raise ValueError("Esa fuente no existe.") from None
    with sesion() as s:
        f = s.get(FuenteBusqueda, fid)
        if not f:
            raise ValueError("Esa fuente no existe.")
        if borrar:
            s.delete(f)
        elif activa is not None:
            f.activa = bool(activa)


def sembrar_desde_banco() -> int:
    """Si no hay dónde buscar, se arranca por las cuentas de los memes que ya guardaron (las que mejor funcionan)."""
    with sesion() as s:
        ls = s.scalars(select(LinkBanco)).all()
    nuevas = []
    for l in ls:
        m = re.search(r"tiktok\.com/@([\w.]{2,40})", l.link)
        if m:
            nuevas.append(f"tt @{m.group(1)}")
            continue
        usuario = str((l.datos or {}).get("usuario") or "")
        if "instagram.com" in l.link and re.fullmatch(r"[\w.]{2,30}", usuario):
            nuevas.append(f"ig @{usuario}")
    return sumar_fuentes(nuevas)


def _elegir() -> list[tuple[str, str, str]]:
    with sesion() as s:
        fs = [f for f in s.scalars(select(FuenteBusqueda).where(FuenteBusqueda.activa.is_(True)))
              if not (f.vistos >= MUERTA and not f.sirven)]
    def puntaje(f):   # primero las que nunca probó, después las que más buenos dieron
        return (1 if f.vistos else 0, 0 if f.consulta.startswith("@") else 1, -(f.sirven + 1) / (f.vistos + 10))
    return [(f.red, f.consulta, _url(f.red, f.consulta)) for f in sorted(fs, key=puntaje)[:FUENTES_POR_VEZ]]


# --- el navegador ----------------------------------------------------------------------------------------------------------
def _chrome() -> str:
    c = config().chrome
    if c and Path(c).exists():
        return c
    for n in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        p = shutil.which(n)
        if p:
            return p
    for p in ("/opt/pw-browsers/chromium", r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        q = Path(p)
        if q.is_file():
            return str(q)
        if q.is_dir():
            cand = next(iter(sorted(q.rglob("chrome"))), None)
            if cand and cand.is_file():
                return str(cand)
    raise ValueError("No encontré Chrome/Chromium en el servidor (instalalo o poné HOOKLINE_CHROME).")


# Chrome sin ventana se presenta como «HeadlessChrome» y Instagram/TikTok lo tratan como robot (piden login o no muestran
# nada): se presenta como un Chrome común de escritorio.
AGENTE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/142.0.0.0 Safari/537.36")


def _soltar_candados(perfil: Path) -> int:
    """El perfil vive en el volumen de datos y sobrevive a cada `docker compose up --build`: si Chrome se cortó, deja
    SingletonLock apuntando al contenedor viejo y el nuevo se niega a abrir el perfil («en uso por otra computadora»).
    Solo se llama cuando no hay ningún navegador del buscador escuchando, así que nadie lo está usando."""
    n = 0
    for nombre in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = perfil / nombre
        if p.is_symlink() or p.exists():
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


def abrir() -> None:
    if _abierto():
        return
    perfil = almacen.dir_de("navegador", "perfil")
    if _soltar_candados(perfil):
        log.info("buscador: saqué los candados viejos del perfil de Chrome")
    registro = perfil.parent / "chrome.log"
    with open(registro, "wb") as salida:
        proc = subprocess.Popen([_chrome(), f"--user-data-dir={perfil}", f"--remote-debugging-port={PUERTO}",
                                 "--no-first-run", "--no-default-browser-check", "--headless=new", "--no-sandbox",
                                 "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1280,900", "--mute-audio",
                                 f"--user-agent={AGENTE}", "--lang=es-AR",
                                 "--disable-blink-features=AutomationControlled", "about:blank"],
                                stdout=salida, stderr=salida, start_new_session=True)
    for _ in range(60):
        if _abierto():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    try:
        renglones = [r for r in registro.read_text(encoding="utf-8", errors="replace").splitlines()
                     if "ERROR" in r or "error" in r.lower()]
    except OSError:
        renglones = []
    motivo = re.sub(r"^\[[^\]]*\]\s*", "", renglones[0])[:220] if renglones else ""
    codigo = proc.poll()
    if codigo is None:
        proc.kill()
    raise ValueError("No pude abrir el navegador del buscador"
                     + (f" (Chrome salió con código {codigo})" if codigo not in (None, 0) else " (no respondió en 30 s)")
                     + (f": {motivo}" if motivo else ".") + f" El detalle queda en {registro}.")


def _contexto(p):
    br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PUERTO}")
    return br, (br.contexts[0] if br.contexts else br.new_context())


REDES = {"instagram": ".instagram.com", "tiktok": ".tiktok.com"}
SESION_DE = {"instagram": "sessionid", "tiktok": "sessionid"}          # sin esta cookie no hay sesión iniciada
NOMBRES_TIKTOK = ("tt_", "ttwid", "sid_tt", "sid_guard", "msToken", "tt_chain_token", "passport_")


def _red(dominio: str) -> str:
    d = str(dominio or "").lower()
    return next((r for r in REDES if r in d), "")


def _ruta_cookies() -> Path:
    from .material import COOKIES_REL
    return almacen.raiz() / COOKIES_REL


def leer_cookies(texto: str) -> list[dict]:
    """Las cookies de Instagram/TikTok en cualquiera de los formatos que dan las extensiones del navegador:
    cookies.txt (Netscape, con tabs o con espacios), la línea del encabezado "Cookie: a=1; b=2" o el JSON de
    Cookie-Editor / EditThisCookie. Devuelve dicts como los que usa Playwright."""
    import json
    texto = str(texto or "").replace("\ufeff", "").strip()
    out: list[dict] = []
    if texto.startswith(("[", "{")):
        try:
            datos = json.loads(texto)
        except ValueError:
            raise ValueError("El texto parece JSON pero está cortado o mal copiado: copialo entero.") from None
        for x in datos if isinstance(datos, list) else (datos.get("cookies") or [] if isinstance(datos, dict) else []):
            if not isinstance(x, dict) or not x.get("name"):
                continue
            c = {"name": str(x["name"]), "value": str(x.get("value") or ""), "domain": str(x.get("domain") or ""),
                 "path": str(x.get("path") or "/"), "secure": bool(x.get("secure", True)),
                 "httpOnly": bool(x.get("httpOnly", False))}
            vence = x.get("expirationDate") or x.get("expires")
            if isinstance(vence, (int, float)) and vence > 0:
                c["expires"] = int(vence)
            out.append(c)
    else:
        tabla = False
        for r in texto.splitlines():
            r = r.strip("\r")
            if not r.strip() or (r.startswith("#") and not r.startswith("#HttpOnly_")):
                continue
            partes = r.split("\t") if "\t" in r else r.split(None, 6)
            if len(partes) < 7 or partes[1].upper() not in ("TRUE", "FALSE") or "." not in partes[0]:
                continue
            tabla = True
            dominio, _, ruta, segura, vence, nombre, valor = [x.strip() for x in partes[:7]]
            c = {"name": nombre, "value": valor, "domain": dominio.replace("#HttpOnly_", ""), "path": ruta or "/",
                 "secure": segura.upper() == "TRUE", "httpOnly": dominio.startswith("#HttpOnly_")}
            if vence.isdigit() and int(vence) > 0:
                c["expires"] = int(vence)
            out.append(c)
        if not tabla and "=" in texto:
            linea = re.sub(r"^\s*cookie\s*:\s*", "", " ".join(texto.split()), flags=re.I)
            pares = [x.strip() for x in linea.split(";") if "=" in x]
            nombres = [x.split("=", 1)[0].strip() for x in pares]
            red = "tiktok" if any(n.startswith(NOMBRES_TIKTOK) for n in nombres) else "instagram"
            for x in pares:
                n, v = x.split("=", 1)
                if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", n.strip()):
                    out.append({"name": n.strip(), "value": v.strip(), "domain": REDES[red], "path": "/",
                                "secure": True, "httpOnly": False})
    out = [c for c in out if _red(c["domain"]) and len(c["value"]) <= 4096]
    if not out:
        raise ValueError("No encontré cookies de Instagram o TikTok en ese texto. Pegá lo que exporta la extensión "
                         "(cookies.txt, JSON o la línea «nombre=valor; ...») con la cuenta iniciada.")
    redes = {_red(c["domain"]) for c in out}
    for red in redes:
        if not any(_red(c["domain"]) == red and c["name"] == SESION_DE[red] and c["value"] for c in out):
            raise ValueError(f"En las cookies de {red.title()} falta «{SESION_DE[red]}»: exportalas con la cuenta "
                             "«para mirar» iniciada en ese navegador.")
    return out


def _netscape(cookies: list[dict]) -> str:
    renglones = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        dominio = ("#HttpOnly_" if c.get("httpOnly") else "") + c["domain"]
        renglones.append("\t".join([dominio, "TRUE" if c["domain"].startswith(".") else "FALSE", c.get("path") or "/",
                                    "TRUE" if c.get("secure") else "FALSE", str(int(max(0, c.get("expires") or 0))),
                                    c["name"], c["value"]]))
    return "\n".join(renglones) + "\n"


def cookies_guardadas() -> list[dict]:
    f = _ruta_cookies()
    if not f.is_file():
        return []
    try:
        return leer_cookies(f.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return []


def importar_cookies(texto: str) -> int:
    """Guarda la sesión de la cuenta "para mirar" en datos/navegador/cookies.txt (lo leen el buscador, que corre en el
    worker, y las descargas con yt-dlp). No necesita abrir un navegador: la API y el worker son procesos distintos."""
    nuevas = leer_cookies(texto)
    redes = {_red(c["domain"]) for c in nuevas}
    with candado("cookies"):
        quedan = [c for c in cookies_guardadas() if _red(c["domain"]) not in redes]   # la otra red queda como estaba
        todas = quedan + nuevas
        f = _ruta_cookies()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(_netscape(todas), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(f)
        with candado("sesiones_redes"):
            d = dict(ajuste("sesiones_redes", {}) or {})
            for r in redes:
                d[r] = {"vencida": False, "motivo": "", "cargada_el": _hora("%d/%m %H:%M"), "visto": ""}
            guardar_ajuste("sesiones_redes", d)
    if _abierto():                       # si el navegador del buscador ya está abierto en esta máquina, también
        try:
            sync_playwright = _playwright()
            with sync_playwright() as p:
                _, ctx = _contexto(p)
                ctx.add_cookies(nuevas)
        except Exception as ex:          # no es grave: el buscador las carga solo al arrancar
            log.info("no pude pasar las cookies al navegador abierto: %s", ex)
    return len(nuevas)


def estado_sesion() -> dict:
    cs = cookies_guardadas()
    return {r: any(_red(c["domain"]) == r and c["name"] == SESION_DE[r] for c in cs) for r in REDES}


def marcar_sesion(red: str, vencida: bool, motivo: str = "") -> None:
    """Lo que se sabe de la sesión de cada red más allá del archivo: si Instagram/TikTok la cortaron (la vio vencida el
    buscador o una descarga). Se limpia al cargar cookies nuevas."""
    if red not in REDES:
        return
    with candado("sesiones_redes"):
        d = dict(ajuste("sesiones_redes", {}) or {})
        r = dict(d.get(red) or {})
        if bool(r.get("vencida")) == bool(vencida) and (not vencida or r.get("motivo") == motivo):
            return
        r.update(vencida=bool(vencida), motivo=str(motivo or "")[:200], visto=_hora("%d/%m %H:%M"))
        d[red] = r
        guardar_ajuste("sesiones_redes", d)


def sesiones() -> dict:
    """Para el panel: por red {cargada, vencida, cargada_el, motivo, visto}. Sobrevive a recargar la página (F5)."""
    hay = estado_sesion()
    d = ajuste("sesiones_redes", {}) or {}
    out = {}
    for r in REDES:
        x = d.get(r) or {}
        out[r] = {"cargada": bool(hay.get(r)), "vencida": bool(hay.get(r) and x.get("vencida")),
                  "cargada_el": str(x.get("cargada_el") or ""), "motivo": str(x.get("motivo") or ""),
                  "visto": str(x.get("visto") or "")}
    return out


def _sesion_viva(ctx, red: str) -> bool:
    """Después de abrir una página de la red: si el sitio borró la cookie de sesión, la sesión murió."""
    dom = "https://www.instagram.com" if red == "instagram" else "https://www.tiktok.com"
    try:
        return any(c.get("name") == SESION_DE[red] and c.get("value") for c in ctx.cookies(dom))
    except Exception:
        return True


def _cargar_sesion(ctx) -> None:
    cs = cookies_guardadas()
    if cs:
        ctx.add_cookies(cs)


def exportar_cookies(destino) -> int:
    sync_playwright = _playwright()
    abrir()
    with sync_playwright() as p:
        _, ctx = _contexto(p)
        cs = ctx.cookies()
    renglones = ["# Netscape HTTP Cookie File"]
    for c in cs:
        if not any(k in c["domain"] for k in ("instagram", "tiktok")):
            continue
        renglones.append("\t".join([c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE", c["path"],
                                    "TRUE" if c["secure"] else "FALSE", str(int(max(0, c.get("expires") or 0))),
                                    c["name"], c["value"]]))
    Path(destino).parent.mkdir(parents=True, exist_ok=True)
    Path(destino).write_text("\n".join(renglones) + "\n", encoding="utf-8")
    return len(renglones) - 1


# --- buscar ------------------------------------------------------------------------------------------------------------------
def buscar(objetivo: int = 30) -> dict:
    if not config().buscador_activo:
        raise ValueError("El buscador de memes está apagado en este servidor (HOOKLINE_BUSCADOR=0).")
    if estado()["estado"] in ("buscando", "filtrando"):
        raise ValueError("Ya está buscando: esperá a que termine.")
    if not _elegir() and not sembrar_desde_banco():
        raise ValueError("Todavía no hay dónde buscar: en Memes → «Dónde busca» pegá cuentas (ig @cuenta, tt @cuenta) "
                         "o frases (ig: pov soltera), o guardá links de memes que te gusten.")
    e = estado(estado="buscando", mensaje="Abriendo el navegador…", juntados=0, nuevos=0, error="",
               objetivo=max(1, int(objetivo or 30)), vuelta=1, acumulado=0, lote="", hojas=0, hechas=0)
    encolar("buscar", {}, clave=f"buscar:{time.time():.0f}", max_intentos=1)
    return e


@manejador("buscar")
def _buscar(_d: dict) -> None:
    try:
        from PIL import Image
        sync_playwright = _playwright()
        abrir()
        lote = time.strftime("%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
        d = almacen.dir_de("candidatos", lote)
        with sesion() as s:
            ya = set(s.scalars(select(LinkBanco.link)).all())
        cands, sin_sesion, problemas = [], set(), []
        with sync_playwright() as p:
            _, ctx = _contexto(p)
            _cargar_sesion(ctx)
            hay = estado_sesion()
            pg = ctx.new_page()
            estado(mensaje="Mirando de quién son los buenos…")
            _autores(pg)
            elegidas = _elegir()
            if not elegidas:
                pg.close()
                estado(estado="error", mensaje="Ya probé todas las fuentes y ninguna dio buenos: pasame cuentas que suban este tipo de video.")
                return
            for i, (red, q, url) in enumerate(elegidas):
                if len(cands) >= MAX_CANDIDATOS:
                    break
                estado(mensaje=f"Buscando «{q}» ({i + 1} de {len(elegidas)})…", juntados=len(cands))
                try:
                    nuevos, muro, _ = _juntar(ctx, red, q, url, ya, hay)
                except ErrorRed as ex:
                    log.info("buscador: %s %s: %s", red, q, ex)
                    problemas.append(str(ex))
                    continue
                except Exception as ex:
                    log.info("buscador: %s %s: %s", red, q, ex)
                    continue
                if muro and red != "pin":         # Pinterest no usa la sesión: no hay nada que cargar
                    sin_sesion.add(NOMBRE_RED[red])
                cands += nuevos
            estado(mensaje="Bajando miniaturas…", juntados=len(cands))
            buenos = []
            for c in cands[:MAX_CANDIDATOS]:
                try:
                    imagen = c.pop("captura", None)
                    if not imagen:
                        r = pg.request.get(c["src"], timeout=15000)
                        if not r.ok:
                            continue
                        imagen = r.body()
                    n = len(buenos) + 1
                    (d / f"{n}.img").write_bytes(imagen)
                    im = Image.open(d / f"{n}.img").convert("RGB")
                    im.thumbnail((300, 534))
                    im.save(d / f"{n}.jpg", quality=80)
                    (d / f"{n}.img").unlink(missing_ok=True)
                    buenos.append({**c, "n": n})
                except Exception:
                    continue
            pg.close()
        aviso_ = (f" {' y '.join(sorted(sin_sesion))} pide iniciar sesión: cargá la sesión de nuevo en Memes → «Cargar "
                  "sesión de Instagram/TikTok» (cookies exportadas con la cuenta iniciada)." if sin_sesion else "")
        if problemas:
            aviso_ += " " + "; ".join(dict.fromkeys(problemas))[:300] + "."
        if not buenos:
            shutil.rmtree(d, ignore_errors=True)
            estado(estado="error" if aviso_ else "listo", fin=_hora("%H:%M"), mensaje=("No encontré nada nuevo." + aviso_).strip())
            return
        import json
        (d / "candidatos.json").write_text(json.dumps(buenos, ensure_ascii=False), encoding="utf-8")
        hojas = _hojas(d, buenos)
        estado(estado="filtrando", lote=lote, hojas=len(hojas), hechas=0, juntados=len(buenos),
               mensaje=f"Claude está mirando {len(buenos)} candidatos…")
        for k, (hoja, rango, parte) in enumerate(hojas, 1):
            tareas.crear("filtrar", {"desde": rango[0], "hasta": rango[1],
                                     "textos": {str(c["n"]): c["alt"] for c in parte if c.get("alt")}},
                         referencia=f"{lote}:{k}", imagenes=[almacen.relativa(hoja)])
    except ValueError as ex:
        estado(estado="error", mensaje=str(ex))
    except Exception as ex:
        estado(estado="error", mensaje=f"Se cortó la búsqueda: {str(ex)[:150]}")


class ErrorRed(Exception):
    """La red contestó con error (403, 429…) o la página no cargó. El texto dice qué pasó en criollo."""


def que_paso(red: str, estado: int, error: str = "") -> str:
    n = NOMBRE_RED.get(red, red)
    if estado in (401, 407):
        return f"{n} pide iniciar sesión ({estado})"
    if estado == 403:
        return f"{n} bloqueó al servidor (403): suele ser por la IP del servidor o la sesión"
    if estado == 429:
        return f"{n} está limitando al servidor por muchos pedidos (429): esperá un rato"
    if estado >= 500:
        return f"{n} falló de su lado ({estado})"
    if estado >= 400:
        return f"{n} contestó con error {estado}"
    m = re.search(r"net::(ERR_[A-Z_]+)", error or "")
    if m and m.group(1) == "ERR_HTTP_RESPONSE_CODE_FAILURE":
        return f"{n} contestó con un error y sin contenido (suele ser que pide sesión o que bloquea la IP del servidor)"
    return f"{n} no cargó la página ({m.group(1) if m else (error or 'sin respuesta')[:80]})"


def _juntar(ctx, red: str, q: str, url: str, ya: set, hay: dict, pasadas: int = 6,
            captura: Path | None = None) -> tuple[list[dict], bool, dict]:
    """Abre la fuente en una pestaña propia (si una falla no arrastra a la siguiente) y junta los videos que se ven
    (link + miniatura). Devuelve (candidatos, pidió_login, {final, estado})."""
    pg = ctx.new_page()
    try:
        return _juntar_en(pg, ctx, red, q, url, ya, hay, pasadas, captura)
    finally:
        try:
            pg.close()
        except Exception:
            pass


def _juntar_en(pg, ctx, red, q, url, ya, hay, pasadas, captura=None):
    estado = {"n": 0}

    def al_responder(r):
        try:
            if not estado["n"] and r.request.is_navigation_request() and r.frame == pg.main_frame:
                estado["n"] = r.status
        except Exception:
            pass
    pg.on("response", al_responder)
    try:
        resp = pg.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception as ex:
        raise ErrorRed(que_paso(red, estado["n"], str(ex))) from None
    codigo = (resp.status if resp else 0) or estado["n"]
    if codigo >= 400 and codigo not in (401,):
        raise ErrorRed(que_paso(red, codigo))
    try:                                 # los resultados los arma JavaScript: se espera a que aparezcan (12 s como mucho)
        pg.wait_for_selector(SELECTOR[red], timeout=12000, state="attached")
    except Exception:
        pass
    pg.wait_for_timeout(1500)
    info = {"final": pg.url, "estado": codigo}
    nombre_red = {"ig": "instagram", "tt": "tiktok"}.get(red, "")
    muro = "/login" in pg.url or "accounts/login" in pg.url or bool(
        red == "ig" and pg.query_selector("input[name='username'], form#loginForm"))
    if nombre_red and hay.get(nombre_red) and not _sesion_viva(ctx, nombre_red):
        marcar_sesion(nombre_red, True, f"{NOMBRE_RED[red]} cerró la sesión al buscar")
        hay[nombre_red] = False
        muro = True
    muro = muro or codigo == 401
    if muro and red != "pin":            # Pinterest muestra los resultados aunque tape con el cartel de registrarse
        _mirar(pg, info, captura)
        return [], True, info
    previos = set(ya)                    # el banco y lo que juntaron las fuentes anteriores de esta búsqueda
    elegidos: dict = {}                  # link → candidato (video con miniatura), en el orden en que aparecen
    fotos: dict = {}                     # IG: link → candidato que por ahora parece foto (se vuelve a mirar)
    sin_mini: dict = {}                  # link → href de un video que todavía no tiene miniatura
    visto_en: dict = {}                  # link → paso en que se vio por primera vez sin miniatura
    intentos: dict = {}                  # link → capturas fallidas o lisas
    ultimo: dict = {}                    # link → última lectura de su tarjeta (para el diagnóstico)
    marcados: set = set()                # IG: links (también los previos) con marca de video
    capturas = 0
    pasos = max(2, pasadas * 2)
    for paso in range(pasos + 1):
        ultima = paso == pasos
        try:
            tarjetas = pg.evaluate(GRILLA_JS, [SELECTOR[red]])
        except Exception as ex:
            if _se_fue(ex):
                info["navego"] = True    # la página se fue a otro lado (login, challenge): lo juntado hasta acá vale
                break
            tarjetas = []
        mejor: dict = {}                 # de varios <a> al mismo post: el que se ve, con contenido y más grande
        for t in tarjetas or []:
            link = _link(str(t.get("href") or ""), red)
            if link:
                p = mejor.get(link)
                clave_t = (bool(t.get("visible")), bool(t.get("video") or t.get("src")), t.get("area") or 0)
                if p is None or clave_t > (bool(p.get("visible")), bool(p.get("video") or p.get("src")), p.get("area") or 0):
                    mejor[link] = t
        for link, t in mejor.items():
            ultimo[link] = t
            es_video = red != "ig" or "/reel/" in str(t.get("href")) or bool(t.get("video") or t.get("icono"))
            if red == "ig" and es_video:
                marcados.add(link)
            if link in previos or link in elegidos:
                continue
            mini = next((u for u in (t.get("src"), t.get("poster"), t.get("bg"))
                         if str(u or "").startswith(("http://", "https://"))), "")
            item = {"link": link, "alt": str(t.get("alt") or "")[:160], "fuente": clave(red, q), "src": mini}
            if not es_video:
                if mini:
                    fotos[link] = item
                continue
            fotos.pop(link, None)        # parecía foto y ahora tiene <video> o ícono
            if mini:
                elegidos[link] = item
                sin_mini.pop(link, None)
            elif t.get("visible") and intentos.get(link, 0) < 3 and (t.get("enPantalla") or link in visto_en):
                sin_mini[link] = str(t.get("href"))      # se captura recién cuando ya pasó por la pantalla (lazy load)
                visto_en.setdefault(link, paso)
        # capturas: solo de las que ya tuvieron un paso para cargar (o al final), con un tope por fuente
        for link, href in list(sin_mini.items()):
            if capturas >= MAX_CAPTURAS:
                break
            if not ultima and visto_en.get(link, paso) >= paso:
                continue
            capturas += 1
            img = _capturar(pg, href, SELECTOR[red])
            if img:
                elegidos[link] = {"link": link, "alt": str((ultimo.get(link) or {}).get("alt") or "")[:160],
                                  "fuente": clave(red, q), "src": "", "captura": img}
                del sin_mini[link]
            else:
                intentos[link] = intentos.get(link, 0) + 1
                if intentos[link] >= 3:
                    del sin_mini[link]
        if ultima:
            break
        try:
            pg.evaluate("() => window.scrollBy(0, Math.round((window.innerHeight || 800) * 0.8))")
        except Exception as ex:
            if _se_fue(ex):
                info["navego"] = True
                break
        pg.wait_for_timeout(800)
    if red != "pin" and ("/login" in pg.url or "/challenge" in pg.url):
        muro = True                      # se fue al login o a un challenge a mitad de camino: lo juntado vale
        info["navego"] = True
    out = list(elegidos.values())
    con_img = sum(1 for t in ultimo.values() if t.get("src") or t.get("poster") or t.get("bg"))
    info["tarjetas"] = {"links": len(ultimo), "con_video": sum(1 for t in ultimo.values() if t.get("video")),
                        "con_img": con_img, "con_icono": sum(1 for t in ultimo.values() if t.get("icono")),
                        "capturas": capturas, "etiquetas": list(dict.fromkeys(
                            str(t.get("etiquetas")) for t in ultimo.values() if t.get("etiquetas")))[:4]}
    ya.update(x["link"] for x in out)
    if red == "ig" and not muro and not q.startswith("@") and len(out) < 10:
        api = _ig_api(pg, q, ya, info)   # pocos o ninguno en la grilla: la búsqueda de Instagram, desde la página
        out += api
        info["api"] = bool(api)
    if red == "ig" and not out and fotos and not marcados:
        out = list(fotos.values())[:MAX_FOTOS_RESERVA]   # la página no marca ningún video: que Claude mire las fotos
        ya.update(x["link"] for x in out)
        info["sin_marca_de_video"] = True
        if len(fotos) > MAX_FOTOS_RESERVA:
            log.info("buscador: %s sin marca de video: mando %s de %s fotos", q, MAX_FOTOS_RESERVA, len(fotos))
    if red == "pin" and not out and not q.startswith("@"):
        out = _pines_api(pg, q, ya)              # la página no mostró pines: los mismos, pedidos a la búsqueda de Pinterest
        info["api"] = True
    _mirar(pg, info, captura)
    return out, muro and not out, info


def _se_fue(ex: Exception) -> bool:
    """La página navegó mientras se la leía (Instagram manda al login o a un challenge)."""
    t = str(ex).lower()
    return "execution context was destroyed" in t or "navigat" in t or "target closed" in t or "has been closed" in t


def _lisa(img: bytes) -> bool:
    """Una captura sin nada: el fondo de la tarjeta antes de que cargue el video o la imagen."""
    from io import BytesIO
    from PIL import Image, ImageStat
    try:
        return sum(ImageStat.Stat(Image.open(BytesIO(img)).convert("L")).stddev) < 4
    except Exception:
        return True


def _capturar(pg, href: str, sel: str) -> bytes | None:
    """Captura JPEG de la tarjeta visible de ese link, después de esperar que tenga cuadro o imagen. None si no se pudo
    o si salió lisa (se reintenta en otro paso)."""
    h = href.replace("\\", "\\\\").replace('"', '\\"')
    try:
        loc = pg.locator(f'a[href="{h}"]:visible').first
        loc.scroll_into_view_if_needed(timeout=2000)
        try:
            loc.evaluate(LISTA_JS, sel, timeout=3000)
        except TypeError:                # Playwright viejo: evaluate sin timeout
            loc.evaluate(LISTA_JS, sel)
        img = loc.screenshot(type="jpeg", quality=80, timeout=3000)
    except Exception:
        return None
    return None if _lisa(img) else img


def _mirar(pg, info: dict, captura) -> None:
    """Para el diagnóstico: dónde terminó, qué texto se ve y una captura de la pantalla."""
    info["final"] = pg.url
    try:
        info["texto"] = " ".join((pg.inner_text("body", timeout=3000) or "").split())[:300]
    except Exception:
        info["texto"] = ""
    if captura:
        try:
            pg.screenshot(path=str(captura))
        except Exception:
            pass


def video_de_pin(x: dict) -> tuple[str, float, str]:
    """(url del video, segundos, miniatura) de un pin de la API de Pinterest. Prefiere un MP4; si no, el HLS (.m3u8),
    que yt-dlp también baja. Mira el video del pin y el de la primera página de un idea pin."""
    listas = [((x.get("videos") or {}).get("video_list") or {})]
    for pagina in ((x.get("story_pin_data") or {}).get("pages") or [])[:1]:
        for bloque in (pagina or {}).get("blocks") or []:
            listas.append(((bloque or {}).get("video") or {}).get("video_list") or {})
    for lista in listas:
        vs = [v for v in lista.values() if isinstance(v, dict) and video_valido(str(v.get("url") or ""))]
        if not vs:
            continue
        mp4 = sorted((v for v in vs if v["url"].split("?")[0].endswith(".mp4")),
                     key=lambda v: -int(v.get("width") or 0))
        v = (mp4 or vs)[0]
        dur = next((float(w["duration"]) / 1000 for w in vs if isinstance(w.get("duration"), (int, float))), 0.0)
        mini = next((str(w["thumbnail"]) for w in vs if str(w.get("thumbnail") or "").startswith("http")), "")
        return v["url"], round(dur, 1), mini
    return "", 0.0, ""


IG_APP_ID = "936619743392459"           # el de la web de Instagram (público: lo manda la propia página)


CARRUSEL = ("carousel_media", "edge_sidecar_to_children")   # las diapositivas de un carrusel no son reels


def _medios_ig(x, out: list, vistos: set, prof: int = 0, cuenta: dict | None = None) -> None:
    """Junta los videos de cualquier respuesta de la API de Instagram, sin depender de su forma: busca a fondo los
    objetos de un medio (code + video) y se queda con su miniatura. No baja a las diapositivas de un carrusel.
    cuenta["con_code"] suma todos los medios que vio (fotos incluidas), para el diagnóstico."""
    if prof > 40:                        # ninguna respuesta real anida tanto
        return
    if isinstance(x, list):
        for y in x[:500]:
            _medios_ig(y, out, vistos, prof + 1, cuenta)
        return
    if not isinstance(x, dict) or x.get("carousel_parent_id"):
        return
    code = x.get("code") or x.get("shortcode")
    if isinstance(code, str) and re.fullmatch(r"[\w-]{5,40}", code):
        if cuenta is not None:
            cuenta["con_code"] = cuenta.get("con_code", 0) + 1
        es_video = bool(x.get("video_versions")) or x.get("media_type") == 2 or x.get("product_type") == "clips" \
            or x.get("is_video") is True
        if es_video and code not in vistos:
            iv = x.get("image_versions2")
            cands = iv.get("candidates") if isinstance(iv, dict) else None
            src = next((str(c.get("url")) for c in (cands if isinstance(cands, list) else [])
                        if isinstance(c, dict) and str(c.get("url") or "").startswith(("https://", "http://"))), "") \
                or str(x.get("display_url") or "")
            if src.startswith(("https://", "http://")):
                vistos.add(code)
                cap = x.get("caption")
                alt = (cap.get("text") if isinstance(cap, dict) else "") or x.get("accessibility_caption") or ""
                out.append({"code": code, "src": src, "alt": str(alt)[:160]})
    for k, v in list(x.items())[:200]:
        if k not in CARRUSEL and isinstance(v, (dict, list)):
            _medios_ig(v, out, vistos, prof + 1, cuenta)


def _ig_api(pg, q: str, ya: set, info: dict | None = None) -> list[dict]:
    """La búsqueda de Instagram por su API web, pedida con fetch() desde la propia página: así sale como la pide la
    web (mismo origen, con la sesión y los encabezados del navegador). Pedida fuera del navegador (page.request)
    contestó 404 «not-logged-in» el 23/11. Frases → fbsearch/web/top_serp; #tag → tags/web_info. 15 s como mucho.
    Deja en info["api_ig"] qué contestó (para `buscador-probar`)."""
    diag = {"estado": 0, "medios": 0, "inicio": ""}
    if info is not None:
        info["api_ig"] = diag
    p = urllib.parse.urlsplit(pg.url or "")
    if p.scheme not in ("http", "https"):            # la página quedó en un error: no hay desde dónde pedir
        diag["inicio"] = f"la página quedó en {pg.url[:60]}"
        return []
    base = f"{p.scheme}://{p.netloc}"
    ruta = base + (f"/api/v1/tags/web_info/?tag_name={urllib.parse.quote(q[1:])}" if q.startswith("#") else
                   f"/api/v1/fbsearch/web/top_serp/?enable_metadata=true&query={urllib.parse.quote(q)}")
    try:
        estado_, texto = pg.evaluate("""async ([ruta, app]) => {
            const c = new AbortController();
            const t = setTimeout(() => c.abort(), 15000);
            try {
              const r = await fetch(ruta, {signal: c.signal, credentials: 'include', headers: {'X-IG-App-ID': app,
                'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}});
              return [r.status, await r.text()];
            } catch (e) { return [0, String(e)]; } finally { clearTimeout(t); }
        }""", [ruta, IG_APP_ID])
    except Exception as ex:
        diag["inicio"] = str(ex)[:120]
        log.info("buscador: la API de Instagram no sirvió para %s: %s", q, ex)
        return []
    diag["estado"] = estado_
    if estado_ != 200:
        diag["inicio"] = " ".join(str(texto or "").split())[:120]
        log.info("buscador: la API de Instagram contestó %s para %s", estado_, q)
        return []
    import json
    medios: list[dict] = []
    cuenta: dict = {}
    try:                                 # una forma rara no puede tirar lo que ya dio la grilla
        datos = json.loads(texto)
        _medios_ig(datos, medios, set(), 0, cuenta)
    except Exception as ex:
        diag["inicio"] = f"respuesta rara ({type(ex).__name__}): " + " ".join(str(texto or "").split())[:90]
        return []
    diag["medios"] = len(medios)
    if not medios:                       # 200 pero nada reconocible: qué trajo, para saber qué cambió
        claves = ",".join(list(datos)[:10]) if isinstance(datos, dict) else type(datos).__name__
        diag["inicio"] = (f"claves: {claves} · {cuenta.get('con_code', 0)} medios (ninguno video) · "
                          + " ".join(str(texto or "").split())[:80])
    out = []
    for m in medios:
        link = f"https://www.instagram.com/reel/{m['code']}/"
        if link in ya:
            continue
        ya.add(link)
        out.append({"link": link, "src": m["src"], "alt": m["alt"], "fuente": clave("ig", q)})
    return out


def _pines_api(pg, q: str, ya: set) -> list[dict]:
    """La búsqueda de Pinterest por su servicio interno (lo que usa su propia página), con el navegador del buscador.
    Pinterest no pide sesión para esto. Devuelve los pines con video primero."""
    import json
    p = urllib.parse.urlsplit(pg.url)
    base = f"{p.scheme}://{p.netloc}" if p.scheme.startswith("http") else "https://www.pinterest.com"
    datos = json.dumps({"options": {"query": q, "scope": "videos", "page_size": 50}, "context": {}},
                       separators=(",", ":"))
    url = (f"{base}/resource/BaseSearchResource/get/?source_url="
           f"{urllib.parse.quote('/search/videos/?q=' + q)}&data={urllib.parse.quote(datos)}")
    try:
        r = pg.request.get(url, timeout=20000, headers={
            "Accept": "application/json, text/javascript, */*; q=0.01", "X-Requested-With": "XMLHttpRequest",
            "X-Pinterest-PWS-Handler": "www/search/[scope].js"})
        if not r.ok:
            return []
        d = r.json()
    except Exception:
        return []
    datos_r = ((d or {}).get("resource_response") or {}).get("data") or {}
    res = datos_r.get("results") if isinstance(datos_r, dict) else datos_r
    con_video, otros = [], []
    for x in res or []:
        if not isinstance(x, dict) or not re.fullmatch(r"[\w-]{5,120}", str(x.get("id") or "")):
            continue                     # sin sesión Pinterest manda ids codificados («e1_AfqP…»), no el número del pin
        link = f"https://www.pinterest.com/pin/{x['id']}/"
        imgs = x.get("images") or {}
        video, dur, mini = video_de_pin(x)
        src = next((str((imgs.get(k) or {}).get("url") or "") for k in ("474x", "236x", "736x", "orig")
                    if (imgs.get(k) or {}).get("url")), "") or mini
        if link in ya or not src.startswith("http"):
            continue
        item = {"link": link, "src": src, "fuente": clave("pin", q),
                "alt": str(x.get("grid_title") or x.get("title") or x.get("description") or "")[:160]}
        if video:
            item.update(video=video, dur=dur)
        es_video = (bool(video) or bool(x.get("videos")) or x.get("story_pin_data") is not None
                    or bool(x.get("is_video")))
        (con_video if es_video else otros).append(item)
    out = con_video or otros
    ya.update(x["link"] for x in out)
    return out


def probar(consultas: list[str] | None = None) -> list[dict]:
    """Para el servidor (`hookline buscador-probar`): abre una fuente de cada red con la sesión cargada y cuenta cuántos
    videos ve. No guarda nada en el banco. Sirve para saber, de verdad y contra las redes, si el buscador anda."""
    if estado().get("estado") in ("buscando", "filtrando"):
        raise ValueError("Hay una búsqueda andando: esperá a que termine.")
    consultas = consultas or ["ig: meme pov", "tt: meme pov", "pin: meme pov"]
    sync_playwright = _playwright()
    abrir()
    out = []
    try:
        pw = sync_playwright().__enter__()
    except Exception as ex:
        raise ValueError(f"El navegador abrió pero Playwright no pudo conectarse ({str(ex)[:160]}). Mirá que la imagen "
                         "tenga sus bibliotecas (docker compose up -d --build).") from None
    with _Cerrar(pw) as p:
        _, ctx = _contexto(p)
        _cargar_sesion(ctx)
        hay = estado_sesion()
        carpeta = almacen.dir_de("navegador")
        for viejo in carpeta.glob("probar-*.png"):
            viejo.unlink(missing_ok=True)
        for i, c in enumerate(consultas, 1):
            m = re.match(r"\s*(ig|tt|pin)\s*:?\s*(.+)", str(c), re.I)
            if not m:
                out.append({"fuente": c, "ok": False, "error": "formato: ig: frase · tt @cuenta · pin: frase"})
                continue
            red, q = m.group(1).lower(), m.group(2).strip()
            r = {"fuente": f"{red} {q}", "red": NOMBRE_RED[red], "url": _url(red, q),
                 "sesion": bool(hay.get({"ig": "instagram", "tt": "tiktok"}.get(red, ""), False))}
            captura = carpeta / f"probar-{i}-{red}.png"
            try:
                cands, muro, info = _juntar(ctx, red, q, r["url"], set(), hay, pasadas=3, captura=captura)
                r.update(ok=bool(cands), videos=len(cands), pidio_login=muro, final=info["final"],
                         estado=info["estado"], ejemplos=[x["link"] for x in cands[:3]], texto=info.get("texto", ""),
                         api=bool(info.get("api")), captura=str(captura) if captura.is_file() else "",
                         tarjetas=info.get("tarjetas") or {}, sin_marca=bool(info.get("sin_marca_de_video")),
                         api_ig=info.get("api_ig") or {}, navego=bool(info.get("navego")))
                if r["sin_marca"]:               # fotos que fueron a Claude sin saber si son videos: no es un ok
                    r["ok"] = False
            except ErrorRed as ex:
                r.update(ok=False, error=str(ex))
            except Exception as ex:
                r.update(ok=False, error=f"{type(ex).__name__}: {str(ex)[:160]}")
            out.append(r)
    return out


class _Cerrar:
    def __init__(self, pw):
        self.pw = pw

    def __enter__(self):
        return self.pw

    def __exit__(self, *a):
        try:
            self.pw.stop()
        except Exception:
            pass


def _autores(pg) -> None:
    """De los reels de Instagram buenos averigua la cuenta y la suma como fuente."""
    with sesion() as s:
        sin = [l.link for l in s.scalars(select(LinkBanco)) if (l.origen == "buscador" or l.voto in ("oro", "bueno"))
               and "instagram.com" in l.link and not l.autor][:15]
    nuevas = []
    for link in sin:
        autor = "?"
        try:
            pg.goto(link, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(2500)
            og = pg.get_attribute("meta[property='og:url']", "content") or ""
            m = re.search(r"instagram\.com/([\w.]+)/(?:reel|p)/", og) or re.search(r"@([\w.]+)", pg.title() or "")
            if m and m.group(1) not in ("reel", "reels", "p", "explore"):
                autor = m.group(1)
                nuevas.append(f"ig @{autor}")
        except Exception:
            pass
        fabrica.poner_autor(link, autor)
    sumar_fuentes(nuevas)


def _hojas(d: Path, buenos: list[dict]):
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
        out.append((hoja, (parte[0]["n"], parte[-1]["n"]), parte))
    return out


# --- lo que eligió Claude ----------------------------------------------------------------------------------------------------
def _tanda_hecha(lote: str, k: int, fil: dict) -> None:
    with _lock_tandas, candado("buscador"):
        _tanda_hecha_ya(lote, k, fil)


def _tanda_hecha_ya(lote: str, k: int, fil: dict) -> None:
    import json
    e = estado()
    if e.get("lote") != lote:
        return
    d = almacen.dir_de("candidatos", lote)
    try:
        cands = {c["n"]: c for c in json.loads((d / "candidatos.json").read_text(encoding="utf-8"))}
    except Exception:
        cands = {}
    de_hoja = [c for n, c in cands.items() if (k - 1) * POR_TANDA < n <= k * POR_TANDA]
    ok_ns = {int(x["n"]) for x in fil.get("sirven") or []}
    with sesion() as s:   # qué fuentes dan buenos (aprende solo)
        for c in de_hoja:
            red, _, q = c.get("fuente", "? ?").partition(" ")
            f = next((x for x in s.scalars(select(FuenteBusqueda).where(FuenteBusqueda.red == red))
                      if clave(x.red, x.consulta) == c.get("fuente")), None)
            if f:
                f.vistos += 1
                f.sirven += 1 if c["n"] in ok_ns else 0
    nuevas = list(fil.get("fuentes_nuevas") or [])[:5]
    for n in ok_ns:
        m = re.search(r"tiktok\.com/@([\w.]+)/", (cands.get(n) or {}).get("link", ""))
        if m:
            nuevas.append(f"tt @{m.group(1)}")
    sumar_fuentes(nuevas)
    items = []
    for x in fil.get("sirven") or []:
        c = cands.get(int(x["n"]))
        if c:
            items.append({"link": c["link"], "titulo": str(x.get("titulo") or c["alt"])[:120], "cat": str(x.get("cat") or "")[:30],
                          "img": d / f"{c['n']}.jpg", "video": c.get("video", ""), "dur": c.get("dur", 0)})
    nuevos = int(e.get("nuevos") or 0) + fabrica.agregar_encontrados(items)
    hechas = int(e.get("hechas") or 0) + 1
    if hechas < int(e.get("hojas") or 0):
        estado(hechas=hechas, nuevos=nuevos, mensaje=f"Claude está mirando… ({hechas} de {e['hojas']} tandas, {nuevos} sirven)")
        return
    shutil.rmtree(d, ignore_errors=True)
    total = int(e.get("acumulado") or 0) + nuevos
    vuelta, objetivo = int(e.get("vuelta") or 1), int(e.get("objetivo") or 30)
    if total < objetivo and vuelta < MAX_VUELTAS:
        estado(estado="buscando", hechas=0, nuevos=0, acumulado=total, vuelta=vuelta + 1, lote="",
               mensaje=f"Vuelta {vuelta + 1} de {MAX_VUELTAS}: llevo {total} de {objetivo}, busco en otros lados…")
        encolar("buscar", {}, clave=f"buscar:{time.time():.0f}:{vuelta + 1}", max_intentos=1)
    else:
        vueltas = "1 vuelta" if vuelta == 1 else f"{vuelta} vueltas"
        estado(estado="listo", hechas=hechas, nuevos=nuevos, acumulado=total, fin=_hora("%H:%M"),
               mensaje=f"Listo: {total} memes nuevos en {vueltas}.")


@tareas.al_terminar("filtrar")
def _filtrado(t, fil: dict) -> None:
    lote, _, k = t.referencia.rpartition(":")
    _tanda_hecha(lote, int(k or 0), fil)


@tareas.al_fallar("filtrar")
def _filtro_fallo(t, motivo: str) -> None:
    lote, _, k = t.referencia.rpartition(":")
    _tanda_hecha(lote, int(k or 0), {"sirven": []})


def recoger() -> None:
    """Los filtros llegan por al_terminar; acá solo se destraba una búsqueda colgada (más de 3 h filtrando)."""
    e = estado()
    if e.get("estado") == "filtrando" and e.get("lote"):
        d = almacen.raiz() / almacen.SECCIONES["candidatos"] / e["lote"]
        if d.exists() and time.time() - d.stat().st_mtime > 3 * 3600:
            estado(estado="error", mensaje="Claude no terminó de mirar los candidatos. Tocá «Buscar memes» de nuevo.")
