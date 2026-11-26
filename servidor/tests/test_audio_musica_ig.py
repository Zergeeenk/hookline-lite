"""Reels que bajan mudos aunque en Instagram suenan (26/11): cuando el reel lleva una canción con licencia o un audio
original, Instagram puede mandar el video sin sonido y NINGUNA pista de audio suelta en la lista de formatos. El arreglo
del 25/11 (unir la pista aparte) no tiene qué unir y el video quedaba mudo. El sonido sí está en los datos del reel
(api/v1/media/<pk>/info → clips_metadata): se baja de ahí y se une al video, desde el segundo en que arranca."""
import functools
import http.server
import subprocess
import sys
import threading
import urllib.request
from types import SimpleNamespace

import pytest

from hookline import medios

pytestmark = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")


def _servir(carpeta):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        type("H", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(carpeta)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _pistas(p) -> str:
    return subprocess.run([medios.ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr


def _volumen(p, desde: float, largo: float) -> float:
    """Volumen máximo (dB) de un tramo del audio: -91 = silencio."""
    r = subprocess.run([medios.ffmpeg(), "-hide_banner", "-ss", f"{desde}", "-t", f"{largo}", "-i", str(p), "-vn",
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    linea = next((x for x in r.stderr.splitlines() if "max_volume" in x), "max_volume: -91 dB")
    return float(linea.split("max_volume:")[1].split("dB")[0])


@pytest.fixture
def cdn(tmp_path):
    """El video mudo (4 s) y dos audios: la canción (3 s de silencio y después el tono) y un audio original (tono)."""
    ff = medios.ffmpeg()
    d = tmp_path / "cdn"
    d.mkdir()
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=360x640:rate=30:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "mudo.mp4")], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=3", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=8", "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
                    "-c:a", "aac", str(d / "cancion.m4a")], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=660:duration=6", "-c:a", "aac",
                    str(d / "original.m4a")], check=True)
    srv = _servir(d)
    yield SimpleNamespace(dir=d, base=f"http://127.0.0.1:{srv.server_address[1]}")
    srv.shutdown()


def _yt_dlp_falso(cdn, medio: dict | None, logueado: bool = True, pedidos: list | None = None):
    """Imita a yt-dlp con un reel de Instagram: baja el video mudo, en los formatos no hay ninguna pista de audio sola,
    y el extractor de Instagram contesta los datos del reel (lo que manda api/v1/media/<pk>/info)."""
    pedidos = pedidos if pedidos is not None else []

    class IE:
        _is_logged_in = logueado
        _API_BASE_URL = "https://www.instagram.com/api/v1"
        _api_headers = {"X-IG-App-ID": "936619743392459"}

        def _download_json(self, url, video_id, *a, **kw):
            pedidos.append(url)
            return {"items": [medio]} if medio is not None else None

    class YDL:
        def __init__(self, o):
            self.o = o

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, link, download=False):
            destino = self.o["outtmpl"].replace("%(ext)s", "mp4")
            with urllib.request.urlopen(cdn.base + "/mudo.mp4") as r, open(destino, "wb") as f:
                f.write(r.read())
            return {"id": "DQx1Abc2_Zz", "extractor_key": "Instagram", "ext": "mp4",
                    "formats": [{"format_id": "1080", "vcodec": "h264", "acodec": None, "url": cdn.base + "/mudo.mp4"}],
                    "requested_downloads": [{"filepath": destino}]}

        def prepare_filename(self, info):
            return self.o["outtmpl"].replace("%(ext)s", "mp4")

        def get_info_extractor(self, clave):
            assert clave == "Instagram"
            return IE()

        def urlopen(self, url):
            return urllib.request.urlopen(url)

    return SimpleNamespace(YoutubeDL=YDL)


@pytest.fixture
def local(monkeypatch):
    """El CDN de la prueba es 127.0.0.1 por http: se acepta solo acá."""
    from hookline.servicios import buscador, material
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setattr(material, "ESQUEMAS_AUDIO", ("https://", "http://127.0.0.1:"))
    monkeypatch.setattr(material, "HOSTS_AUDIO_IG", (".cdninstagram.com", ".fbcdn.net", "127.0.0.1"))


def _cancion(cdn, desde_ms=None):
    m = {"music_asset_info": {"progressive_download_url": cdn.base + "/cancion.m4a"}}
    if desde_ms is not None:
        m["music_consumption_info"] = {"audio_asset_start_time_in_ms": desde_ms}
    return {"pk": "1", "clips_metadata": {"music_info": m, "original_sound_info": None}}


def test_reel_mudo_con_cancion_baja_con_la_cancion_desde_su_segundo(cdn, local, tmp_path, monkeypatch):
    from hookline.servicios import material
    pedidos = []
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 3000), pedidos=pedidos))
    video, _ = material.bajar_video("https://www.instagram.com/reel/DQx1Abc2_Zz/", tmp_path / "salida", "ejemplo")
    p = _pistas(video)
    assert video.name == "ejemplo.mp4" and "Video: h264" in p and "Audio: aac" in p, p
    assert _volumen(video, 0.2, 0.6) > -30                    # arranca en el segundo 3 de la canción: ya suena
    assert abs(medios.duracion(video) - 4) < 0.3              # el largo es el del video, no el de la canción
    assert sorted(x.name for x in video.parent.iterdir()) == ["ejemplo.mp4"]          # sin restos
    assert pedidos == ["https://www.instagram.com/api/v1/media/" + str(_pk("DQx1Abc2_Zz")) + "/info/"]


def test_reel_mudo_con_audio_original_baja_con_ese_audio(cdn, local, tmp_path, monkeypatch):
    from hookline.servicios import material
    medio = {"pk": "1", "clips_metadata": {"music_info": None, "original_sound_info": {
        "progressive_download_url": cdn.base + "/original.m4a", "audio_asset_start_time_in_ms": 0}}}
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, medio))
    video, _ = material.bajar_video("https://www.instagram.com/reel/DQx1Abc2_Zz/", tmp_path / "salida", "ejemplo")
    assert "Audio: aac" in _pistas(video) and _volumen(video, 0.2, 0.6) > -30


def test_sin_sesion_no_pide_los_datos_y_queda_como_estaba(cdn, local, tmp_path, monkeypatch):
    from hookline.servicios import material
    pedidos = []
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn), logueado=False, pedidos=pedidos))
    video, _ = material.bajar_video("https://www.instagram.com/reel/DQx1Abc2_Zz/", tmp_path / "salida", "ejemplo")
    assert video.is_file() and "Audio:" not in _pistas(video) and pedidos == []


def test_audio_de_un_host_que_no_es_de_instagram_no_se_baja(cdn, local, tmp_path, monkeypatch):
    from hookline.servicios import material
    monkeypatch.setattr(material, "HOSTS_AUDIO_IG", (".cdninstagram.com", ".fbcdn.net"))   # 127.0.0.1 ya no
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn)))
    video, _ = material.bajar_video("https://www.instagram.com/reel/DQx1Abc2_Zz/", tmp_path / "salida", "ejemplo")
    assert video.is_file() and "Audio:" not in _pistas(video)


@pytest.mark.parametrize("medio", [None, {}, {"clips_metadata": None}, {"clips_metadata": {"music_info": "x"}},
                                   {"clips_metadata": {"music_info": {"music_asset_info": {"progressive_download_url": 5}}}}])
def test_datos_raros_o_vacios_no_rompen_la_descarga(cdn, local, tmp_path, monkeypatch, medio):
    from hookline.servicios import material
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, medio))
    video, _ = material.bajar_video("https://www.instagram.com/reel/DQx1Abc2_Zz/", tmp_path / "salida", "ejemplo")
    assert video.is_file() and "Audio:" not in _pistas(video)
    assert sorted(x.name for x in video.parent.iterdir()) == ["ejemplo.mp4"]


def test_el_yt_dlp_instalado_tiene_lo_que_usa_el_arreglo():
    """El arreglo usa el extractor de Instagram de yt-dlp (su sesión, sus encabezados y su forma de pedir). Si una
    versión nueva de yt-dlp cambia esos nombres, esto falla en el CI antes de llegar al servidor."""
    yt_dlp = pytest.importorskip("yt_dlp")
    with yt_dlp.YoutubeDL({"quiet": True}) as y:
        ie = y.get_info_extractor("Instagram")
        assert isinstance(ie._is_logged_in, bool) and not ie._is_logged_in
        assert ie._API_BASE_URL.startswith("https://") and ie._api_headers.get("X-IG-App-ID")
        assert callable(ie._download_json) and callable(y.urlopen)


def _pk(shortcode):
    from hookline.servicios import material
    return material._pk_instagram(shortcode)


def test_el_pk_es_el_mismo_que_calcula_yt_dlp():
    ig = pytest.importorskip("yt_dlp.extractor.instagram")
    for sc in ("DQx1Abc2_Zz", "CeHRW8FvZhn", "B-_a9", "Df8qGyNuD65" + "x" * 28):
        assert _pk(sc) == ig._id_to_pk(sc), sc
