"""Fuentes (tipografías) de los textos de los reels y de las capturas dibujadas.

Hay tres clases:
  - **las de siempre** (ESTILOS: Clásica, Fuerte, Moderna...): para cada una hay una lista de candidatas y se usa la
    primera que exista en el servidor.
  - **el catálogo**: todas las demás fuentes instaladas en el servidor (en Docker: Roboto, Open Sans, Lato, las de
    Word como Arial, Times New Roman, Comic Sans o Impact, y muchas más). Se buscan en HOOKLINE_DIR_FUENTES,
    /usr/share/fonts y, si existe, C:\\Windows\\Fonts. Solo las que tienen letras latinas con acentos y ñ.
  - **subidas** desde el panel: .ttf u .otf sueltos o en un .zip (como baja Google Fonts). Viven en
    datos/fuentes/<clave>.<ext> (el volumen que comparten la API y el worker) y sus datos en `fuentes_subidas`.

**Mis fuentes** (las que se ofrecen en Copiar video y ve Claude) = las de siempre que no se sacaron
(`fuentes_ocultas`) + las agregadas del catálogo (`fuentes_activas`) + las subidas. Sacar una de siempre o del
catálogo la devuelve al catálogo; sacar una subida la borra.

La **predeterminada** es la que usa la fábrica en los reels nuevos (`fuente_predeterminada`; si no, la Clásica). Con
`fuente_claude` prendido, Claude ve un muestrario y elige la más parecida al ejemplo. Lo que ya se armó con una fuente
que se sacó sigue saliendo (cae a la predeterminada si el archivo ya no está).

Los caracteres que la letra no tiene (emojis, por ejemplo) no se dibujan: `dibujable()` los saca antes de escribir.
"""
from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from functools import lru_cache
from pathlib import Path

from .config import config

# clave -> (candidatas, familia CSS, peso CSS, estilo CSS, nombre visible)
ESTILOS = {
    # la "Clásica" de Instagram: sans redonda en negrita media (ver buscador/estilo.md)
    "clasica": (["Montserrat-SemiBold.ttf", "Montserrat-SemiBold.otf", "Inter-SemiBold.otf", "Inter-SemiBold.ttf",
                 "segoeuib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"], "Montserrat, Inter, sans-serif", 600, "normal", "Clásica"),
    "fuerte": (["Montserrat-Black.ttf", "Inter-Black.otf", "seguibl.ttf", "DejaVuSans-Bold.ttf"], "Montserrat, Inter, sans-serif", 900, "normal", "Fuerte"),
    "moderna": (["Inter-Regular.otf", "Inter-Regular.ttf", "bahnschrift.ttf", "DejaVuSans.ttf"], "Inter, sans-serif", 400, "normal", "Moderna"),
    "meme": (["Anton-Regular.ttf", "impact.ttf", "DejaVuSans-Bold.ttf"], "Impact, Anton, sans-serif", 400, "normal", "Meme"),
    "maquina": (["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "courbd.ttf"], "'Courier New', monospace", 700, "normal", "Máquina"),
    "elegante": (["DejaVuSerif-BoldItalic.ttf", "LiberationSerif-BoldItalic.ttf", "georgiaz.ttf"], "Georgia, serif", 700, "italic", "Elegante"),
    "redonda": (["Nunito-Bold.ttf", "trebucbd.ttf", "DejaVuSans-Bold.ttf"], "'Trebuchet MS', sans-serif", 700, "normal", "Redonda"),
    # para las capturas de chat dibujadas (no se ofrecen en el panel)
    "texto": (["Inter-Regular.otf", "Inter-Regular.ttf", "segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"], "", 400, "normal", ""),
    "texto_negrita": (["Inter-SemiBold.otf", "Inter-SemiBold.ttf", "seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"], "", 600, "normal", ""),
}
EXTENSIONES = (".ttf", ".otf")
MAX_ARCHIVO = 15 * 1024 * 1024
MAX_ZIP_TOTAL = 80 * 1024 * 1024
MAX_POR_ZIP = 40
MAX_SUBIDAS = 300
CLAVE_SUBIDA = re.compile(r"f_[0-9a-f]{10}")
MUESTRA = "Cuando me dice «ya llego» 😅"


# --- las del sistema --------------------------------------------------------------------------------------------------
def _carpetas() -> list[Path]:
    out = list(config().dir_fuentes)
    out += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path(r"C:\Windows\Fonts")]
    return [d for d in out if d.is_dir()]


@lru_cache
def _indice() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    reales: dict[Path, Path] = {}              # el mismo archivo con dos nombres (Impact.ttf → impact.ttf): uno solo
    for d in _carpetas():
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() not in EXTENSIONES or not p.is_file() or p.name.lower() in idx:
                continue
            r = p.resolve()
            idx[p.name.lower()] = reales.setdefault(r, p)
    return idx


def _ruta_sistema(estilo: str) -> Path | None:
    cands = ESTILOS.get(estilo, ESTILOS["clasica"])[0]
    idx = _indice()
    for c in cands:
        if c.lower() in idx:
            return idx[c.lower()]
    return None


@lru_cache(maxsize=1024)
def _cmap(p: Path) -> frozenset | None:
    """Los caracteres que la fuente sabe dibujar (None si no se pudo leer)."""
    try:
        from fontTools.ttLib import TTFont
        with TTFont(str(p), lazy=True, fontNumber=0) as f:
            return frozenset(f.getBestCmap() or {})
    except Exception:
        return None


LATINAS = frozenset(map(ord, "AaZzÑñÁáÉéÍíÓóÚú¿?¡!0123456789"))


@lru_cache
def _catalogo_sistema() -> tuple[dict, ...]:
    """Todas las fuentes instaladas que sirven para escribir en castellano, sin las que ya usan las de siempre."""
    propias = {_ruta_sistema(k) for k, v in ESTILOS.items() if v[4]}
    out, claves, vistos = [], set(), set()
    for nombre, p in sorted(_indice().items()):
        if p in propias or p in vistos or p.stat().st_size > MAX_ARCHIVO * 4:
            continue
        vistos.add(p)
        cm = _cmap(p)
        if cm is None or not LATINAS <= cm:
            continue
        fam = _familia(p)
        if not fam:
            continue
        base = "s_" + (re.sub(r"[^a-z0-9]+", "-", p.stem.lower()).strip("-")[:40] or "fuente")
        clave, i = base, 2
        while clave in claves:
            clave, i = f"{base}-{i}", i + 1
        claves.add(clave)
        out.append({"clave": clave, "ruta": p, "nombre": fam})
    out.sort(key=lambda x: x["nombre"].lower())
    return tuple(out)


def _curadas() -> list[tuple[str, Path]]:
    """Las de siempre que existen en este servidor, sin repetir archivo (si Meme y Redonda caen en la misma letra,
    se ofrece solo la primera)."""
    out, vistos = [], set()
    for k, v in ESTILOS.items():
        p = _ruta_sistema(k) if v[4] else None
        if p and p not in vistos:
            vistos.add(p)
            out.append((k, p))
    return out


def _del_catalogo(clave: str) -> dict | None:
    return next((x for x in _catalogo_sistema() if x["clave"] == clave), None)


# --- ajustes (con un cache cortito: normalizar() de la fábrica pregunta por cada copia) ---------------------------------
_cache: dict = {"t": 0.0, "v": None}


def _ajustes() -> dict:
    if _cache["v"] is not None and time.monotonic() - _cache["t"] < 2:
        return _cache["v"]
    from .comun import ajuste
    v = {"subidas": [x for x in (ajuste("fuentes_subidas", []) or []) if isinstance(x, dict)
                     and CLAVE_SUBIDA.fullmatch(str(x.get("clave", "")))],
         "ocultas": [str(x) for x in (ajuste("fuentes_ocultas", []) or [])],
         "activas": [str(x) for x in (ajuste("fuentes_activas", []) or [])],
         "predeterminada": str(ajuste("fuente_predeterminada", "") or ""),
         "claude": bool(ajuste("fuente_claude", False))}
    _cache.update(t=time.monotonic(), v=v)
    return v


def _guardar(clave: str, valor) -> None:
    from .comun import guardar_ajuste
    guardar_ajuste(clave, valor)
    _cache["v"] = None
    _cache["lista"] = None


def _dir() -> Path:
    from . import almacen
    return almacen.dir_de("fuentes")


def _archivo_subida(f: dict) -> Path | None:
    p = _dir() / f"{f['clave']}{f.get('ext', '.ttf')}"
    return p if p.is_file() else None


# --- consultar ------------------------------------------------------------------------------------------------------------
def ruta(estilo: str) -> Path | None:
    """El archivo de una fuente (aunque esté oculta: lo ya armado con ella sigue saliendo)."""
    estilo = str(estilo or "")
    if CLAVE_SUBIDA.fullmatch(estilo):
        f = next((x for x in _ajustes()["subidas"] if x["clave"] == estilo), None)
        return _archivo_subida(f) if f else None
    if estilo.startswith("s_"):
        x = _del_catalogo(estilo)
        return x["ruta"] if x else None
    if estilo not in ESTILOS:
        return None
    return _ruta_sistema(estilo)


def ruta_obligatoria(estilo: str = "clasica") -> Path:
    p = ruta(estilo) or ruta(predeterminada()) or _ruta_sistema("clasica")
    if not p:
        raise RuntimeError("No hay fuentes instaladas para escribir el texto del reel (instalá fonts-dejavu o "
                           "Montserrat, o subí una en la sección Fuentes).")
    return p


def lista() -> list[dict]:
    """Mis fuentes: las de siempre que no se sacaron + las agregadas del catálogo + las subidas.
    Cache de 2 s: la fábrica pregunta por cada copia."""
    if _cache.get("lista") is not None and time.monotonic() - _cache.get("lista_t", 0) < 2:
        return [dict(x) for x in _cache["lista"]]
    a = _ajustes()
    out, vistos = [], set()
    for k, p in _curadas():
        vistos.add(p)
        if k in a["ocultas"]:
            continue
        out.append({"clave": k, "nombre": ESTILOS[k][4], "origen": "sistema", "archivo": p.name, "familia": _familia(p),
                    "bytes": p.stat().st_size, "por": "", "fecha": ""})
    for k in a["activas"]:
        x = _del_catalogo(k)
        if x and x["ruta"] not in vistos:
            vistos.add(x["ruta"])
            out.append({"clave": k, "nombre": x["nombre"], "origen": "catalogo", "archivo": x["ruta"].name,
                        "familia": x["nombre"], "bytes": x["ruta"].stat().st_size, "por": "", "fecha": ""})
    for f in a["subidas"]:
        p = _archivo_subida(f)
        if not p:
            continue
        out.append({"clave": f["clave"], "nombre": f.get("nombre") or f["clave"], "origen": "subida",
                    "archivo": f.get("original", ""), "familia": f.get("familia", ""),
                    "bytes": p.stat().st_size, "por": f.get("por", ""), "fecha": f.get("fecha", "")})
    pre = predeterminada(out)
    for x in out:
        x["oculta"] = False
        x["predeterminada"] = x["clave"] == pre
        x["url"] = f"/api/fuente?k={x['clave']}"
    _cache.update(lista=out, lista_t=time.monotonic())
    return [dict(x) for x in out]


def catalogo(q: str = "", pagina: int = 0, por_pagina: int = 24) -> dict:
    """Lo que se puede agregar: todo lo instalado que no está en Mis fuentes (también las de siempre que se sacaron)."""
    mias = {x["clave"] for x in lista()}
    a = _ajustes()
    todas = [{"clave": k, "nombre": ESTILOS[k][4], "archivo": p.name} for k, p in _curadas() if k in a["ocultas"]]
    todas += [{"clave": x["clave"], "nombre": x["nombre"], "archivo": x["ruta"].name} for x in _catalogo_sistema()]
    todas = [x for x in todas if x["clave"] not in mias]
    q = re.sub(r"\s+", " ", str(q or "")).strip().lower()[:60]
    if q:
        todas = [x for x in todas if all(w in (x["nombre"] + " " + x["archivo"]).lower() for w in q.split(" "))]
    por_pagina = max(1, min(int(por_pagina or 24), 60))
    pagina = max(0, int(pagina or 0))
    items = todas[pagina * por_pagina:(pagina + 1) * por_pagina]
    for x in items:
        x["url"] = f"/api/fuente?k={x['clave']}"
    return {"items": items, "total": len(todas), "pagina": pagina, "por_pagina": por_pagina}


def existe_alguna(clave: str) -> bool:
    """Para servir el archivo: está en Mis fuentes o en el catálogo."""
    clave = str(clave or "")
    if any(x["clave"] == clave for x in lista()):
        return True
    if clave.startswith("s_"):
        return _del_catalogo(clave) is not None
    return any(k == clave for k, _ in _curadas())


def visibles(todas: list[dict] | None = None) -> list[dict]:
    return [x for x in (todas if todas is not None else lista()) if not x.get("oculta")]


def predeterminada(todas: list[dict] | None = None) -> str:
    vis = [x["clave"] for x in (todas if todas is not None else lista()) if not x.get("oculta")]
    p = _ajustes()["predeterminada"]
    if p in vis:
        return p
    return "clasica" if "clasica" in vis else (vis[0] if vis else "clasica")


def disponibles() -> dict:
    """Para el panel (Ajustar a mano y la vista previa). Cada una con su archivo: el navegador la carga de verdad."""
    return {x["clave"]: {"familia": f"hl-{x['clave']}", "peso": 400, "estilo": "normal", "nombre": x["nombre"],
                         "url": x["url"], "predeterminada": x["predeterminada"]} for x in visibles()}


def claves_disponibles() -> set[str]:
    return {x["clave"] for x in visibles()}


def claude_elige() -> bool:
    return _ajustes()["claude"]


@lru_cache(maxsize=256)
def _familia(p: Path) -> str:
    try:
        from PIL import ImageFont
        fam, est = ImageFont.truetype(str(p), 20).getname()
        return f"{fam} {est}".strip()
    except Exception:
        return ""


# --- subir ----------------------------------------------------------------------------------------------------------
def _validar(datos: bytes) -> tuple[str, str]:
    """(familia, estilo) si es una fuente que se puede usar para escribir; si no, ValueError."""
    if len(datos) > MAX_ARCHIVO:
        raise ValueError("La fuente pesa más de 15 MB.")
    if datos[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"):
        raise ValueError("Eso no es una fuente .ttf u .otf.")
    if datos[:4] == b"ttcf":
        raise ValueError("Las colecciones .ttc no se aceptan: subí cada fuente como .ttf u .otf.")
    try:
        from PIL import Image, ImageDraw, ImageFont
        f = ImageFont.truetype(io.BytesIO(datos), 40)
        fam, est = f.getname()
        im = Image.new("L", (400, 80))
        ImageDraw.Draw(im).text((5, 5), "Hola Ññ 123", font=f, fill=255)   # que de verdad dibuje
        if not im.getbbox():
            raise ValueError("vacía")
    except Exception:
        raise ValueError("No se pudo leer esa fuente (¿está dañada?).") from None
    limpio = lambda s: re.sub(r"[^\w áéíóúñÁÉÍÓÚÑ.\-]", "", str(s or "")).strip()[:60]
    return limpio(fam), limpio(est)


def _archivos_de(nombre: str, datos: bytes) -> list[tuple[str, bytes]]:
    nombre = Path(str(nombre or "fuente")).name
    if nombre.lower().endswith(".zip") or datos[:4] == b"PK\x03\x04":
        try:
            z = zipfile.ZipFile(io.BytesIO(datos))
        except zipfile.BadZipFile:
            raise ValueError("El .zip está dañado.") from None
        out, total = [], 0
        for i in z.infolist():
            n = Path(i.filename).name
            if i.is_dir() or not n.lower().endswith(EXTENSIONES) or n.startswith((".", "__")):
                continue
            if i.file_size > MAX_ARCHIVO:
                continue
            total += i.file_size
            if total > MAX_ZIP_TOTAL or len(out) >= MAX_POR_ZIP:
                break
            with z.open(i) as fh:
                b = fh.read(MAX_ARCHIVO + 1)
            if len(b) <= MAX_ARCHIVO:
                out.append((n, b))
        if not out:
            raise ValueError("El .zip no tiene fuentes .ttf u .otf.")
        return out
    if not nombre.lower().endswith(EXTENSIONES):
        raise ValueError("Subí fuentes .ttf u .otf (sueltas o en un .zip). Las .woff de páginas web no sirven para video.")
    return [(nombre, datos)]


def _con_candado(f):
    """Los cambios leen y reescriben la lista de fuentes: con dos a la vez (o API y worker) se perdería una."""
    import functools

    @functools.wraps(f)
    def envuelta(*a, **k):
        from .comun import candado
        with candado("fuentes"):
            _cache["v"] = None
            _cache["lista"] = None
            return f(*a, **k)
    return envuelta


@_con_candado
def subir(nombre: str, datos: bytes, por: str = "") -> dict:
    """Devuelve {"nuevas": [...], "repetidas": n, "errores": [...]}."""
    from .comun import iso_local
    from .db import ahora
    import secrets
    archivos = _archivos_de(nombre, datos)
    subidas = list(_ajustes()["subidas"])
    huellas = {x.get("huella") for x in subidas}
    nuevas, repetidas, errores = [], 0, []
    for n, b in archivos:
        try:
            fam, est = _validar(b)
        except ValueError as ex:
            errores.append(f"{n}: {ex}")
            continue
        h = hashlib.sha256(b).hexdigest()
        if h in huellas:
            repetidas += 1
            continue
        if len(subidas) >= MAX_SUBIDAS:
            errores.append(f"{n}: ya hay {MAX_SUBIDAS} fuentes subidas; borrá alguna primero.")
            break
        clave = "f_" + secrets.token_hex(5)
        ext = ".otf" if b[:4] == b"OTTO" else ".ttf"
        (_dir() / f"{clave}{ext}").write_bytes(b)
        visible = (f"{fam} {est}" if est and est.lower() not in ("regular", "normal") else fam) or Path(n).stem
        f = {"clave": clave, "ext": ext, "nombre": visible[:60], "familia": f"{fam} {est}".strip(),
             "original": re.sub(r"[^\w.\- ]", "_", n)[:80], "huella": h, "por": str(por)[:120],
             "fecha": iso_local(ahora())}
        subidas.append(f)
        huellas.add(h)
        nuevas.append({k: f[k] for k in ("clave", "nombre", "familia")})
    if nuevas:
        _guardar("fuentes_subidas", subidas)
    if not nuevas and not repetidas:
        raise ValueError(errores[0] if len(errores) == 1 else "Ninguna de esas fuentes se pudo usar: " + "; ".join(errores[:3]))
    return {"nuevas": nuevas, "repetidas": repetidas, "errores": errores}


# --- cambiar ------------------------------------------------------------------------------------------------------------
def _existe(clave: str) -> dict:
    x = next((f for f in lista() if f["clave"] == clave), None)
    if not x:
        raise ValueError("Esa fuente ya no está en Mis fuentes.")
    return x


@_con_candado
def borrar(clave: str) -> None:
    """Saca una fuente de Mis fuentes: las subidas se borran; las otras vuelven al catálogo."""
    x = _existe(str(clave))
    if len(lista()) <= 1:
        raise ValueError("Tiene que quedar al menos una fuente: agregá o subí otra antes de sacar esta.")
    a = _ajustes()
    if x["origen"] == "sistema":
        _guardar("fuentes_ocultas", a["ocultas"] + [x["clave"]])
    elif x["origen"] == "catalogo":
        _guardar("fuentes_activas", [k for k in a["activas"] if k != x["clave"]])
    else:
        f = next(f for f in a["subidas"] if f["clave"] == x["clave"])
        _guardar("fuentes_subidas", [s for s in a["subidas"] if s["clave"] != x["clave"]])
        p = _archivo_subida(f)
        if p:
            p.unlink(missing_ok=True)
    if _ajustes()["predeterminada"] == x["clave"]:
        _guardar("fuente_predeterminada", "")


MAX_ACTIVAS = 120


@_con_candado
def agregar(clave: str) -> None:
    """Pasa una fuente del catálogo a Mis fuentes."""
    clave = str(clave or "")
    a = _ajustes()
    if clave in ESTILOS and ESTILOS[clave][4]:
        if not any(k == clave for k, _ in _curadas()):
            raise ValueError("Esa fuente no está en este servidor.")
        if clave not in a["ocultas"]:
            return
        _guardar("fuentes_ocultas", [k for k in a["ocultas"] if k != clave])
        return
    if not _del_catalogo(clave):
        raise ValueError("Esa fuente no está en el catálogo.")
    if clave in a["activas"]:
        return
    if len(a["activas"]) >= MAX_ACTIVAS:
        raise ValueError(f"Ya hay {MAX_ACTIVAS} fuentes del catálogo en uso: sacá alguna primero.")
    _guardar("fuentes_activas", a["activas"] + [clave])


def mostrar(clave: str) -> None:
    agregar(clave)


@_con_candado
def elegir_predeterminada(clave: str) -> None:
    x = _existe(str(clave))
    if x["oculta"]:
        raise ValueError("Esa fuente está oculta: mostrala primero.")
    _guardar("fuente_predeterminada", x["clave"])


@_con_candado
def renombrar(clave: str, nombre: str) -> None:
    x = _existe(str(clave))
    if x["origen"] != "subida":
        raise ValueError("Las fuentes del sistema no se renombran.")
    nombre = re.sub(r"[\x00-\x1f\x7f<>]", "", str(nombre or "")).strip()[:60]
    if not nombre:
        raise ValueError("Escribí un nombre.")
    _guardar("fuentes_subidas", [{**s, "nombre": nombre} if s["clave"] == x["clave"] else s for s in _ajustes()["subidas"]])


def poner_claude(valor: bool) -> None:
    _guardar("fuente_claude", bool(valor))


# --- lo que la letra sabe dibujar -----------------------------------------------------------------------------------------
def dibujable(texto: str, p: Path | None) -> str:
    """Saca lo que la fuente no tiene (emojis y símbolos raros) para que no salgan cuadraditos en el video.
    Si la fuente no se puede leer, devuelve el texto tal cual."""
    texto = str(texto or "")
    cm = _cmap(Path(p)) if p else None
    if cm is None:
        return texto
    out = "".join(ch for ch in texto if ch in "\n\t " or ord(ch) in cm)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(l.strip() for l in out.split("\n")).strip("\n")


def faltan(texto: str, clave: str) -> str:
    """Los caracteres del texto que esa fuente no va a dibujar (para avisar en el panel)."""
    p = ruta(clave)
    cm = _cmap(p) if p else None
    if cm is None:
        return ""
    vistos = []
    for ch in str(texto or ""):
        if ch not in "\n\t " and ord(ch) not in cm and ch not in vistos:
            vistos.append(ch)
    return "".join(vistos[:20])


# --- muestrario para Claude ------------------------------------------------------------------------------------------------
def muestrario() -> tuple[Path, list[dict]] | None:
    """Una imagen con cada fuente visible escrita en sí misma, numerada con su clave, para que Claude elija."""
    vis = visibles()
    if not vis:
        return None
    firma = hashlib.sha256("|".join(f"{x['clave']}:{x['bytes']}" for x in vis).encode()).hexdigest()[:12]
    destino = _dir() / f"_muestrario-{firma}.jpg"
    if not destino.is_file():
        from PIL import Image, ImageDraw, ImageFont
        fila, ancho = 92, 1100
        im = Image.new("RGB", (ancho, 20 + fila * len(vis)), "white")
        d = ImageDraw.Draw(im)
        rotulo = ImageFont.truetype(str(ruta_obligatoria("clasica")), 20)
        for i, x in enumerate(vis):
            y = 10 + i * fila
            d.text((14, y + 4), x["clave"], font=rotulo, fill=(200, 30, 90))
            try:
                f = ImageFont.truetype(str(ruta(x["clave"])), 40)
                d.text((230, y + 18), "Cuando me dice ya llego", font=f, fill="black")
            except Exception:
                pass
            d.line((0, y + fila - 6, ancho, y + fila - 6), fill=(230, 230, 230))
        for viejo in _dir().glob("_muestrario-*.jpg"):
            viejo.unlink(missing_ok=True)
        im.save(destino, quality=85)
    return destino, vis
