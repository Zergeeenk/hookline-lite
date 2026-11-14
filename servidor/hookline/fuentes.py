"""Fuentes para los textos de los reels y las capturas dibujadas.

En Lite salían de C:\\Windows\\Fonts (Segoe UI, Impact...). En el servidor se buscan, en orden:
HOOKLINE_DIR_FUENTES, /usr/share/fonts (en Docker se instalan Montserrat, Inter y DejaVu) y, si existe,
C:\\Windows\\Fonts. Para cada estilo hay una lista de candidatas; se usa la primera que exista.
"""
from __future__ import annotations

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
    # para las capturas de chat dibujadas
    "texto": (["Inter-Regular.otf", "Inter-Regular.ttf", "segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"], "", 400, "normal", ""),
    "texto_negrita": (["Inter-SemiBold.otf", "Inter-SemiBold.ttf", "seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"], "", 600, "normal", ""),
}


def _carpetas() -> list[Path]:
    out = list(config().dir_fuentes)
    out += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts", Path(r"C:\Windows\Fonts")]
    return [d for d in out if d.is_dir()]


@lru_cache
def _indice() -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for d in _carpetas():
        for p in d.rglob("*"):
            if p.suffix.lower() in (".ttf", ".otf") and p.name.lower() not in idx:
                idx[p.name.lower()] = p
    return idx


def ruta(estilo: str) -> Path | None:
    cands = ESTILOS.get(estilo, ESTILOS["clasica"])[0]
    idx = _indice()
    for c in cands:
        if c.lower() in idx:
            return idx[c.lower()]
    return None


def ruta_obligatoria(estilo: str = "clasica") -> Path:
    p = ruta(estilo) or ruta("clasica")
    if not p:
        raise RuntimeError("No hay fuentes instaladas para escribir el texto del reel (instalá fonts-dejavu o "
                           "Montserrat, o apuntá HOOKLINE_DIR_FUENTES a una carpeta con .ttf).")
    return p


def disponibles() -> dict:
    """Para la vista previa del panel: solo las que existen en este servidor."""
    return {k: {"familia": v[1], "peso": v[2], "estilo": v[3], "nombre": v[4]}
            for k, v in ESTILOS.items() if v[4] and ruta(k)}
