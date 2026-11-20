"""Lo que pasó en el server (20/11): cerraron la sesión «para mirar», yt-dlp vio que no valía, le BORRÓ el sessionid al
cookies.txt de ella al guardar, y desde ahí todos los links daban «No se pudo bajar» sin decir por qué."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HEADER = "sessionid=111%3Aabc%3A22; csrftoken=tok; ds_user_id=111"


class _YDLQueBorra:
    """Imita a yt-dlp con una sesión vencida: avisa, limpia el sessionid y reescribe el cookiefile al cerrar."""
    usados = []

    def __init__(self, o):
        self.o = o
        _YDLQueBorra.usados.append(o.get("cookiefile"))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        f = self.o.get("cookiefile")
        if f:
            renglones = [r for r in Path(f).read_text().splitlines() if "\tsessionid\t" not in r]
            Path(f).write_text("\n".join(renglones) + "\n")

    def extract_info(self, link, download=False):
        if self.o.get("logger") and self.o.get("cookiefile"):
            self.o["logger"].warning("The provided Instagram account cookies are no longer valid")
        raise RuntimeError("ERROR: [Instagram] Df8qGyNuD65: There is no video in this post")

    def prepare_filename(self, info):
        return "x"


@pytest.fixture
def ytdlp(monkeypatch):
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    _YDLQueBorra.usados.clear()
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDLQueBorra))


def test_ytdlp_no_le_borra_la_sesion_a_ella_y_dice_que_vencio(ytdlp, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(HEADER)
    assert buscador.estado_sesion()["instagram"]
    with pytest.raises(ValueError) as e:
        material.bajar_video("https://www.instagram.com/p/Df8qGyNuD65/?dmpr=x", tmp_path)
    assert "venció" in str(e.value) and "Cargar sesión" in str(e.value)
    assert _YDLQueBorra.usados[0] and not _YDLQueBorra.usados[0].endswith("navegador/cookies.txt")   # una copia
    assert not Path(_YDLQueBorra.usados[0]).exists()                       # y la copia se borra
    assert buscador.estado_sesion()["instagram"]                           # el archivo de ella, intacto


def test_fichas_tampoco_le_borran_la_sesion(ytdlp):
    from hookline.servicios import buscador, fabrica
    buscador.importar_cookies(HEADER)
    fabrica.guardar_links("https://www.instagram.com/reel/AAA111/")
    fabrica._fichas({})
    assert buscador.estado_sesion()["instagram"]
    assert [x["ficha"] for x in fabrica.links()] == ["error"]


@pytest.mark.parametrize("error,avisos,sesion,espera", [
    ("There is no video in this post", [], False, "no da los videos sin sesión"),
    ("Instagram sent an empty media response", [], False, "no da los videos sin sesión"),
    ("There is no video in this post", [], True, "puede ser de fotos, privado o borrado"),
    ("There is no video in this post", ["The provided Instagram account cookies are no longer valid"], True, "venció"),
    ("This post is a photo", [], True, "es de fotos"),
    ("HTTP Error 404", [], False, "No se pudo bajar"),
])
def test_motivo_en_criollo(error, avisos, sesion, espera, monkeypatch):
    from hookline.servicios import buscador, material
    monkeypatch.setattr(buscador, "estado_sesion", lambda: {"instagram": sesion, "tiktok": False})
    assert espera in material.motivo_ytdlp(RuntimeError(error), "https://www.instagram.com/p/X/", avisos)


def test_sin_cookies_no_pasa_cookiefile(ytdlp, tmp_path):
    from hookline.servicios import material
    with pytest.raises(ValueError) as e:
        material.bajar_video("https://www.instagram.com/reel/AAA/", tmp_path)
    assert _YDLQueBorra.usados == [None]
    assert "Cargar sesión" in str(e.value)
