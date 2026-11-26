"""Reels que bajan mudos aunque en Instagram suenan (26/11): cuando el reel lleva una canción con licencia o un audio
original, Instagram puede mandar el video sin sonido y NINGUNA pista de audio suelta en la lista de formatos. El arreglo
del 25/11 (unir la pista aparte) no tiene qué unir y el video quedaba mudo. El sonido sí está en los datos del reel
(api/v1/media/<pk>/info → clips_metadata): se baja de ahí y se une al video, desde el segundo en que arranca.

Revisión del #31: un audio cortado o un arranque fuera del audio armaban una pista de puro silencio que pasaba como
«con sonido»; y los datos del reel se pedían dos veces con la cuenta «para mirar» (yt-dlp ya los había pedido)."""
import functools
import http.server
import re
import subprocess
import sys
import threading
import urllib.request
from types import SimpleNamespace

import pytest

from hookline import medios

pytestmark = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")

CODIGO = "DQx1Abc2_Zz"
LINK = f"https://www.instagram.com/reel/{CODIGO}/"


def _servir(carpeta, pedidos: list):
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            pedidos.append(self.path)
            super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(carpeta)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _pistas(p) -> str:
    return subprocess.run([medios.ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr


def _fin_del_silencio_inicial(p) -> float:
    """Cuántos segundos de silencio tiene el audio al principio (0 = suena desde el arranque)."""
    r = subprocess.run([medios.ffmpeg(), "-hide_banner", "-nostats", "-i", str(p), "-vn", "-af",
                        "silencedetect=noise=-50dB:d=0.1", "-f", "null", "-"], capture_output=True, text=True)
    ini = re.search(r"silence_start:\s*(-?[\d.]+)", r.stderr)
    fin = re.search(r"silence_end:\s*([\d.]+)", r.stderr)
    if not ini or float(ini.group(1)) > 0.05:
        return 0.0
    return float(fin.group(1)) if fin else 99.0


@pytest.fixture
def cdn(tmp_path):
    """El video mudo (4 s) y dos audios: la canción (3 s de silencio y después 8 s de tono) y un audio original
    (6 s de tono)."""
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
    pedidos = []
    srv = _servir(d, pedidos)
    yield SimpleNamespace(dir=d, base=f"http://127.0.0.1:{srv.server_address[1]}", pedidos=pedidos)
    srv.shutdown()


class _Cortado:
    """Una respuesta del CDN que se corta: dice un tamaño y manda menos (el urlopen de yt-dlp no avisa)."""

    def __init__(self, r):
        self.r = r
        self.headers = {"Content-Length": str(int(r.headers["Content-Length"]) + 5000)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.r.close()

    def read(self, n=-1):
        return self.r.read(n)


def _yt_dlp_falso(cdn, medio, logueado=True, pide_en_extract=False, cortar=False, datos_pedidos=None):
    """Imita a yt-dlp con un reel de Instagram: baja el video mudo, en los formatos no hay ninguna pista de audio sola,
    y el extractor de Instagram (una sola instancia por YoutubeDL, como el real) contesta los datos del reel. Con
    pide_en_extract, extract_info pide los datos como lo hace yt-dlp con la sesión (_real_extract)."""
    datos_pedidos = datos_pedidos if datos_pedidos is not None else []

    class IE:
        _is_logged_in = logueado
        _API_BASE_URL = "https://www.instagram.com/api/v1"
        _api_headers = {"X-IG-App-ID": "936619743392459"}

        def _download_json(self, url, video_id, *a, **kw):
            datos_pedidos.append(url)
            return {"items": [medio]} if medio is not None else False

    class YDL:
        def __init__(self, o):
            self.o = o
            self.ie = IE()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, link, download=False):
            if pide_en_extract:
                self.get_info_extractor("Instagram")._download_json(
                    f"https://www.instagram.com/api/v1/media/{_pk(CODIGO)}/info/", CODIGO)
            destino = self.o["outtmpl"].replace("%(ext)s", "mp4")
            with urllib.request.urlopen(cdn.base + "/mudo.mp4") as r, open(destino, "wb") as f:
                f.write(r.read())
            return {"id": CODIGO, "extractor_key": "Instagram", "ext": "mp4",
                    "formats": [{"format_id": "1080", "vcodec": "h264", "acodec": None, "url": cdn.base + "/mudo.mp4"}],
                    "requested_downloads": [{"filepath": destino}]}

        def prepare_filename(self, info):
            return self.o["outtmpl"].replace("%(ext)s", "mp4")

        def get_info_extractor(self, clave):
            assert clave == "Instagram"
            return self.ie

        def urlopen(self, url):
            r = urllib.request.urlopen(url)
            return _Cortado(r) if cortar else r

    return SimpleNamespace(YoutubeDL=YDL)


@pytest.fixture
def local(monkeypatch):
    """El CDN de la prueba es 127.0.0.1 por http: se acepta solo acá."""
    from hookline.servicios import buscador, material
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setattr(material, "ESQUEMAS_AUDIO", ("https://", "http://127.0.0.1:"))
    monkeypatch.setattr(material, "HOSTS_AUDIO_IG", (".cdninstagram.com", ".fbcdn.net", "127.0.0.1"))


def _cancion(cdn, desde_ms=None, dura_ms=None, archivo="cancion.m4a"):
    asset = {"fast_start_progressive_download_url": f"{cdn.base}/{archivo}"}
    if dura_ms is not None:
        asset["duration_in_ms"] = dura_ms
    m = {"music_asset_info": asset}
    if desde_ms is not None:
        m["music_consumption_info"] = {"audio_asset_start_time_in_ms": desde_ms}
    return {"pk": "1", "clips_metadata": {"music_info": m, "original_sound_info": None}}


def _original(cdn, archivo, desde_ms, dura_ms=None):
    o = {"progressive_download_url": f"{cdn.base}/{archivo}", "audio_asset_start_time_in_ms": desde_ms}
    if dura_ms is not None:
        o["duration_in_ms"] = dura_ms
    return {"pk": "1", "clips_metadata": {"music_info": None, "original_sound_info": o}}


def _bajar(tmp_path):
    from hookline.servicios import material
    return material.bajar_video(LINK, tmp_path / "salida", "ejemplo")[0]


def _sin_restos(video):
    assert sorted(x.name for x in video.parent.iterdir()) == ["ejemplo.mp4"]


def test_reel_mudo_con_cancion_arranca_en_su_segundo_exacto(cdn, local, tmp_path, monkeypatch):
    # la canción tiene 3 s de silencio: arrancando en 2,5 s, el reel tiene que tener 0,5 s de silencio y después sonar
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 2500)))
    video = _bajar(tmp_path)
    p = _pistas(video)
    assert video.name == "ejemplo.mp4" and "Video: h264" in p and "Audio: aac" in p, p
    assert abs(_fin_del_silencio_inicial(video) - 0.5) < 0.1
    assert abs(medios.duracion(video) - 4) < 0.3              # el largo es el del video, no el de la canción
    _sin_restos(video)


def test_audio_original_con_arranque_distinto_de_cero(cdn, local, tmp_path, monkeypatch):
    # un audio original reusado de otro reel: el tramo empieza en 1 s → 2 s de silencio de la canción y después suena
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _original(cdn, "cancion.m4a", 1000)))
    video = _bajar(tmp_path)
    assert "Audio: aac" in _pistas(video) and abs(_fin_del_silencio_inicial(video) - 2.0) < 0.1
    _sin_restos(video)


def test_audio_original_desde_el_principio(cdn, local, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _original(cdn, "original.m4a", 0)))
    video = _bajar(tmp_path)
    assert "Audio: aac" in _pistas(video) and _fin_del_silencio_inicial(video) == 0.0


def test_si_yt_dlp_ya_pidio_los_datos_no_se_piden_de_nuevo(cdn, local, tmp_path, monkeypatch):
    pedidos = []
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 3000), pide_en_extract=True,
                                                            datos_pedidos=pedidos))
    video = _bajar(tmp_path)
    assert "Audio: aac" in _pistas(video)
    assert pedidos == [f"https://www.instagram.com/api/v1/media/{_pk(CODIGO)}/info/"]     # uno solo: el de yt-dlp


def test_si_yt_dlp_no_los_pidio_se_piden_una_vez(cdn, local, tmp_path, monkeypatch):
    pedidos = []
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 3000), datos_pedidos=pedidos))
    assert "Audio: aac" in _pistas(_bajar(tmp_path))
    assert pedidos == [f"https://www.instagram.com/api/v1/media/{_pk(CODIGO)}/info/"]


def test_sin_sesion_no_pide_los_datos_y_queda_como_estaba(cdn, local, tmp_path, monkeypatch):
    pedidos = []
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn), logueado=False, datos_pedidos=pedidos))
    video = _bajar(tmp_path)
    assert "Audio:" not in _pistas(video) and pedidos == []
    _sin_restos(video)


def test_audio_cortado_no_se_usa_y_no_queda_un_video_con_silencio(cdn, local, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 3000), cortar=True))
    video = _bajar(tmp_path)
    assert "Audio:" not in _pistas(video)
    _sin_restos(video)


def test_arranque_despues_del_audio_no_arma_una_pista_muda(cdn, local, tmp_path, monkeypatch):
    # la canción dura 11 s; sin su duración en los datos, arrancar en el 20 da puro silencio: no se usa
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn, 20000)))
    video = _bajar(tmp_path)
    assert "Audio:" not in _pistas(video)
    _sin_restos(video)


def test_arranque_mayor_que_la_duracion_saltea_ese_y_usa_el_siguiente(cdn, local, tmp_path, monkeypatch):
    medio = {"pk": "1", "clips_metadata": {
        "original_sound_info": {"progressive_download_url": f"{cdn.base}/original.m4a",
                                "audio_asset_start_time_in_ms": 6000, "duration_in_ms": 6000},
        "music_info": {"music_asset_info": {"fast_start_progressive_download_url": f"{cdn.base}/cancion.m4a"},
                       "music_consumption_info": {"audio_asset_start_time_in_ms": 3000}}}}
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, medio))
    video = _bajar(tmp_path)
    assert "Audio: aac" in _pistas(video) and _fin_del_silencio_inicial(video) == 0.0
    assert "/original.m4a" not in cdn.pedidos and "/cancion.m4a" in cdn.pedidos     # el original ni se bajó


def test_audio_de_un_host_que_no_es_de_instagram_no_se_baja(cdn, local, tmp_path, monkeypatch):
    from hookline.servicios import material
    monkeypatch.setattr(material, "HOSTS_AUDIO_IG", (".cdninstagram.com", ".fbcdn.net"))   # 127.0.0.1 ya no
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, _cancion(cdn)))
    video = _bajar(tmp_path)
    assert "Audio:" not in _pistas(video) and "/cancion.m4a" not in cdn.pedidos


@pytest.mark.parametrize("medio", [None, {}, {"clips_metadata": None}, {"clips_metadata": {"music_info": "x"}},
                                   {"clips_metadata": {"music_info": {"music_asset_info": {
                                       "fast_start_progressive_download_url": 5}}}}])
def test_datos_raros_o_vacios_no_rompen_la_descarga(cdn, local, tmp_path, monkeypatch, medio):
    monkeypatch.setitem(sys.modules, "yt_dlp", _yt_dlp_falso(cdn, medio))
    video = _bajar(tmp_path)
    assert "Audio:" not in _pistas(video)
    _sin_restos(video)


def test_la_vista_previa_no_toma_temporales_de_una_descarga_cortada(tmp_path, monkeypatch):
    import hashlib
    from hookline import almacen
    from hookline.servicios import fabrica, material
    link = "https://www.instagram.com/reel/PrEvIa123/"
    fabrica.agregar_encontrados([{"link": link, "titulo": "pov", "img": "", "dur": 4.0}])
    carpeta = almacen.dir_de("copias", "_previas")
    nombre = hashlib.md5(link.encode()).hexdigest()[:12]
    for resto in (f"{nombre}.audio.m4a", f"{nombre}.con-musica.mp4", f"{nombre}.musica"):
        (carpeta / resto).write_bytes(b"x")
    bajado = []
    monkeypatch.setattr(material, "bajar_video", lambda l, c, n: (bajado.append(n) or (c / f"{n}.mp4"), {}))
    assert fabrica.previa(link) == carpeta / f"{nombre}.mp4" and bajado == [nombre]    # no tomó ningún resto
    (carpeta / f"{nombre}.mp4").write_bytes(b"x")
    assert fabrica.previa(link) == carpeta / f"{nombre}.mp4" and bajado == [nombre]    # el bueno sí, sin bajar


def test_el_yt_dlp_instalado_tiene_lo_que_usa_el_arreglo():
    """El arreglo usa el extractor de Instagram de yt-dlp (su sesión, sus encabezados, su forma de pedir y que haya una
    sola instancia por YoutubeDL, para capturar lo que ya pidió). Si una versión nueva cambia algo de eso, esto falla
    en el CI antes de llegar al servidor."""
    yt_dlp = pytest.importorskip("yt_dlp")
    with yt_dlp.YoutubeDL({"quiet": True}) as y:
        ie = y.get_info_extractor("Instagram")
        assert y.get_info_extractor("Instagram") is ie
        assert isinstance(ie._is_logged_in, bool) and not ie._is_logged_in
        assert ie._API_BASE_URL.startswith("https://") and ie._api_headers.get("X-IG-App-ID")
        assert callable(ie._download_json) and callable(y.urlopen)


def test_la_captura_funciona_con_el_extractor_real(tmp_path):
    """Con el yt-dlp real: lo que pide su extractor de Instagram a media/<pk>/info queda capturado; lo demás no."""
    yt_dlp = pytest.importorskip("yt_dlp")
    from hookline.servicios import material
    d = tmp_path / "api"
    (d / "api" / "v1" / "media" / "123" / "info").mkdir(parents=True)
    (d / "api" / "v1" / "media" / "123" / "info" / "index.html").write_text('{"items": [{"pk": "123"}]}')
    (d / "otra.json").write_text('{"x": 1}')
    srv = _servir(d, [])
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        capturado = []
        with yt_dlp.YoutubeDL({"quiet": True}) as y:
            material._capturar_datos_ig(y, LINK, capturado)
            ie = y.get_info_extractor("Instagram")
            assert ie._download_json(base + "/otra.json", "x", note=False) == {"x": 1}
            assert ie._download_json(base + "/api/v1/media/123/info/", "x", note=False) == {"items": [{"pk": "123"}]}
        assert capturado == [{"items": [{"pk": "123"}]}]
    finally:
        srv.shutdown()


def _pk(shortcode):
    from hookline.servicios import material
    return material._pk_instagram(shortcode)


def test_el_pk_es_el_mismo_que_calcula_yt_dlp():
    ig = pytest.importorskip("yt_dlp.extractor.instagram")
    for sc in ("DQx1Abc2_Zz", "CeHRW8FvZhn", "B-_a9", "Df8qGyNuD65" + "x" * 28):
        assert _pk(sc) == ig._id_to_pk(sc), sc
