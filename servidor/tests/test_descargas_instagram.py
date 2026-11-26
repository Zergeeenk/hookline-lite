"""Copiar video desde Instagram (26/11): «/p/DgDwFQHByVM» falló con «No video formats found!» y el panel decía «Fijate
que el link sea de un video público». Con la sesión, yt-dlp sí había traído los datos del post: no tenía video (una
foto). Ahora:
- si los datos del post dicen que es de fotos (una foto o un carrusel de fotos), lo dice así y no insiste;
- de un carrusel con video baja ese video (antes la primera foto del carrusel hacía fallar todo);
- si con la sesión «para mirar» no sale (vencida, trabada, frenada), prueba una vez sin sesión;
- si nada sirve, el mensaje dice el motivo real.

Con el yt-dlp de verdad y su extractor de Instagram; lo único de mentira es Instagram: se reemplaza YoutubeDL.urlopen
para lo que va a instagram.com (los videos salen de un servidor local, como si fuera el CDN)."""
import functools
import http.server
import io
import json
import subprocess
import sys
import threading
import urllib.parse
from types import SimpleNamespace

import pytest

from hookline import medios

yt_dlp = pytest.importorskip("yt_dlp")
pytestmark = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")

from yt_dlp.networking import Response                      # noqa: E402
from yt_dlp.networking.exceptions import HTTPError          # noqa: E402

FOTO, VIDEO, CARRUSEL, CARRUSEL_FOTOS = "DgDwFQHByVM", "DQx1Abc2_Zz", "C4rrus3lVid", "C4rrus3lFot"
HIJO_FOTO, HIJO_VIDEO, HIJO_VIDEO2 = "Hij0F0t0aaa", "Hij0V1d30bb", "Hij0V1d30cc"


def _pk(codigo: str) -> int:
    from hookline.servicios import material
    return material._pk_instagram(codigo)


def _cookies(valor: str) -> str:
    return f"sessionid=111%3A{valor}%3A22; csrftoken=tok; ds_user_id=111"


class _Instagram:
    """Contesta lo que pide el extractor de Instagram de yt-dlp. La sesión del pedido (su cookie sessionid) decide:
    «buena» entra; «vieja» redirige al login (yt-dlp avisa «no longer valid» y sigue sin sesión); «trabada» contesta
    400 (un «confirmá que sos vos» pendiente: yt-dlp no sigue); «frenada» contesta 429. Sin sesión, la página del post
    trae los datos que ve cualquiera (o 404 si sin_sesion=False)."""

    def __init__(self, cdn: str):
        self.cdn = cdn
        self.posts: dict[int, dict] = {}
        self.sin_sesion = True
        self.sin_urls_sin_sesion = False                 # sin sesión, los videos vienen sin sus URLs (como el 20/11)
        self.al_pedir_el_post = None                     # algo que pasa mientras yt-dlp pide los datos con la sesión
        self.pedidos: list[tuple[str, str]] = []        # (ruta, sesión con la que se pidió: «» = sin sesión)
        self.desconocidos: list[str] = []               # lo que yt-dlp pidió y este Instagram no conoce
        self.del_cdn: list[str] = []                    # lo que se bajó del CDN

    def medio(self, codigo, tipo, hijos=()):
        m = {"pk": str(_pk(codigo)), "id": f"{_pk(codigo)}_9", "code": codigo, "media_type": tipo,
             "user": {"username": "cuenta.memes", "pk": "9", "full_name": "Cuenta Memes"},
             "caption": {"text": f"post {codigo}"}, "taken_at": 1764100000,
             "image_versions2": {"candidates": [{"url": f"{self.cdn}/foto.jpg", "width": 1080, "height": 1350}]}}
        if tipo == 2:
            m.update(video_versions=[{"type": 101, "url": f"{self.cdn}/{codigo}.mp4", "width": 360, "height": 640}],
                     video_duration=2.0, has_audio=True)
        if hijos:
            m["carousel_media"] = list(hijos)
        return m

    def publicar(self, codigo, tipo, hijos=()):
        self.posts[_pk(codigo)] = self.medio(codigo, tipo, hijos)

    def _resp(self, url, cuerpo, codigo=200, tipo="application/json"):
        if not isinstance(cuerpo, (bytes, str)):
            cuerpo = json.dumps(cuerpo)
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode()
        r = Response(io.BytesIO(cuerpo), url=url, headers={"Content-Type": tipo + "; charset=utf-8"}, status=codigo,
                     reason={200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                             429: "Too Many Requests"}.get(codigo))
        if codigo >= 400:
            raise HTTPError(r)
        return r

    def contestar(self, ydl, req):
        url = req.url
        u = urllib.parse.urlsplit(url)
        sesion = next((c.value for c in ydl.cookiejar if c.name == "sessionid" and "instagram.com" in c.domain), "")
        self.pedidos.append((u.path, sesion))
        if u.path in ("", "/"):                        # «Setting up session»: el token LSD de la página de inicio
            return self._resp(url, '<html><script type="application/json" id="__eqmc">{"l": "LSD1"}</script></html>',
                              tipo="text/html")
        if u.path.startswith("/api/v1/media/") and u.path.endswith("/info/"):
            if self.al_pedir_el_post:
                self.al_pedir_el_post()
            if "vieja" in sesion:
                return self._resp("https://www.instagram.com/accounts/login/?next=" + urllib.parse.quote(u.path),
                                  "<html>login</html>", 401, "text/html")
            if "trabada" in sesion:
                return self._resp(url, {"message": "checkpoint_required", "status": "fail"}, 400)
            if "frenada" in sesion:
                return self._resp(url, {"message": "Please wait a few minutes before you try again.", "status": "fail"},
                                  429)
            if "rota" in sesion:
                return self._resp(url, "<html>Service Unavailable</html>", 503, "text/html")
            if "lenta" in sesion:
                from yt_dlp.networking.exceptions import TransportError
                raise TransportError("The read operation timed out")
            medio = self.posts.get(int(u.path.split("/")[4]))
            return self._resp(url, {"items": [medio], "num_results": 1, "status": "ok"} if medio else {}, 200 if medio else 404)
        if u.path == "/api/v1/web/get_ruling_for_content/":
            return self._resp(url, {"status": "ok"})
        if u.path == "/api/graphql":                   # solo con curl_cffi (impersonate): acá no da nada, sigue por /p/
            return self._resp(url, {}, 404)
        if u.path.startswith("/p/") and not self.sin_sesion:
            return self._resp(url, "<html>login</html>", 404, "text/html")
        if u.path.startswith("/p/"):
            medio = next((m for m in self.posts.values() if m["code"] == u.path.split("/")[2]), None)
            if medio and self.sin_urls_sin_sesion:
                medio = json.loads(json.dumps(medio))
                for x in [medio] + medio.get("carousel_media", []):
                    x.pop("video_versions", None)
            if medio:
                datos = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
                    ["RelayPrefetchedStreamCache", "next", [], ["adp_PolarisPostRoot", {"__bbox": {"result": {"data": {
                        "xig_polaris_media": {"if_not_gated_logged_out": medio}}}}}]]]}}]]]}
                return self._resp(url, f'<html><script type="application/json" data-sjs>{json.dumps(datos)}</script>'
                                       '</html>', tipo="text/html")
            return self._resp(url, "<html>no existe</html>", 404, "text/html")
        self.desconocidos.append(f"{req.method} {u.path}")   # yt-dlp pidió algo nuevo: que la prueba lo diga
        return self._resp(url, {}, 404)


@pytest.fixture(scope="module")
def _videos(tmp_path_factory):
    """Un MP4 con sonido por cada video (se arman una vez por archivo de tests)."""
    d = tmp_path_factory.mktemp("cdn")
    for codigo in (VIDEO, HIJO_VIDEO, HIJO_VIDEO2):
        subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=green:size=360x640:rate=30:duration=2", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-shortest", str(d / f"{codigo}.mp4")], check=True)
    return d


@pytest.fixture
def ig(_videos, monkeypatch):
    """El CDN (un servidor local con los MP4) e Instagram de mentira. Estricto: si yt-dlp pide algo que este Instagram
    no conoce, o algo fuera de instagram.com y del CDN, la prueba falla diciendo qué (un yt-dlp nuevo cambió algo)."""
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setitem(sys.modules, "yt_dlp", yt_dlp)
    falso = None

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            falso.del_cdn.append(self.path)
            super().do_GET()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(_videos)))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    falso = _Instagram(f"http://127.0.0.1:{srv.server_address[1]}")
    original = yt_dlp.YoutubeDL.urlopen

    def urlopen(self, req):
        if isinstance(req, str):
            from yt_dlp.networking import Request
            req = Request(req)
        host = urllib.parse.urlsplit(req.url).hostname or ""
        if host.endswith("instagram.com"):
            return falso.contestar(self, req)
        if host == "127.0.0.1":
            return original(self, req)
        from yt_dlp.networking.exceptions import TransportError
        falso.desconocidos.append(f"{req.method} {host}")
        raise TransportError(f"la prueba no sale a Internet ({host})")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "urlopen", urlopen)
    yield falso
    srv.shutdown()
    srv.server_close()
    assert not falso.desconocidos, f"yt-dlp pidió {falso.desconocidos}: hay que sumarlo al Instagram de mentira"


def _bajar(tmp_path, codigo, tipo="p"):
    from hookline.servicios import material
    return material.bajar_video(f"https://www.instagram.com/{tipo}/{codigo}/", tmp_path / "salida", "ejemplo")


def _pistas(p) -> str:
    return subprocess.run([medios.ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr


def _sin_sesion(ig) -> list[str]:
    """Lo que se le pidió a Instagram sin ninguna sesión (la prueba sin sesión)."""
    return [ruta for ruta, sesion in ig.pedidos if not sesion]


# --- lo que pasó el 26/11 -------------------------------------------------------------------------------------------------
def test_una_foto_con_sesion_dice_que_es_una_foto_y_no_insiste(ig, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar(FOTO, 1)
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, FOTO)
    assert str(e.value) == material.M_FOTO
    assert _sin_sesion(ig) == []                                      # no probó sin sesión: ya se sabe
    assert not buscador.sesiones()["instagram"]["vencida"]            # y la sesión no se marca: anduvo


def test_el_error_real_de_yt_dlp_con_una_foto_es_no_video_formats(ig, tmp_path):
    """Lo que dijo el server: con la sesión, una foto da «No video formats found!» (lo levanta YoutubeDL, no el
    extractor). Si yt-dlp cambia eso, esta prueba avisa."""
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar(FOTO, 1)
    with pytest.raises(yt_dlp.utils.DownloadError, match="No video formats found"), material.cookies_ytdlp() as ck:
        with yt_dlp.YoutubeDL({"quiet": True, "logger": material._Callado(), "outtmpl": str(tmp_path / "x.%(ext)s"),
                               "cookiefile": ck}) as y:
            y.extract_info(f"https://www.instagram.com/p/{FOTO}/", download=True)


def test_un_carrusel_de_fotos_lo_dice(ig, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar(CARRUSEL_FOTOS, 8, [ig.medio(HIJO_FOTO, 1), ig.medio(HIJO_FOTO[:-1] + "z", 1)])
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, CARRUSEL_FOTOS)
    assert str(e.value) == material.M_CARRUSEL
    assert _sin_sesion(ig) == []


def test_un_carrusel_con_video_baja_el_primer_video_y_nada_mas(ig, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar(CARRUSEL, 8, [ig.medio(HIJO_FOTO, 1), ig.medio(HIJO_VIDEO, 2), ig.medio(HIJO_VIDEO2, 2)])
    video, info = _bajar(tmp_path, CARRUSEL)
    assert video.name == "ejemplo.mp4" and "Audio: aac" in _pistas(video)
    assert info["id"] == HIJO_VIDEO and info.get("uploader") == "Cuenta Memes"
    assert sorted(x.name for x in video.parent.iterdir()) == ["ejemplo.mp4"]          # solo uno, sin restos
    assert f"/{HIJO_VIDEO2}.mp4" not in ig.del_cdn                                          # el otro, ni lo pidió
    # con el nombre de siempre (Material → Ejemplos): cada video tendría su archivo; queda uno solo
    video, _ = material.bajar_video(f"https://www.instagram.com/p/{CARRUSEL}/", tmp_path / "ejemplos")
    assert sorted(x.name for x in video.parent.iterdir()) == [f"Cuenta_Memes_{HIJO_VIDEO}.mp4"]


def test_un_carrusel_con_video_sin_sesion_tambien(ig, tmp_path):
    ig.publicar(CARRUSEL, 8, [ig.medio(HIJO_FOTO, 1), ig.medio(HIJO_VIDEO, 2)])
    video, info = _bajar(tmp_path, CARRUSEL)
    assert info["id"] == HIJO_VIDEO and sorted(x.name for x in video.parent.iterdir()) == ["ejemplo.mp4"]


def test_un_video_con_sesion_baja_como_siempre(ig, tmp_path):
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar(VIDEO, 2)
    video, info = _bajar(tmp_path, VIDEO, "reel")
    assert video.name == "ejemplo.mp4" and info["id"] == VIDEO and "Audio: aac" in _pistas(video)
    assert _sin_sesion(ig) == []


def test_p_y_reel_piden_lo_mismo(ig, tmp_path):
    """Lo que se había creído: que «/p/» necesitaba sesión y «/reel/» no. yt-dlp usa solo el código del post: sin
    curl_cffi (como la imagen del servidor) los dos piden exactamente lo mismo. Con curl_cffi cambiaría solo el Referer
    de un pedido sin sesión (a /api/graphql), no lo que se baja."""
    ig.publicar(VIDEO, 2)
    _bajar(tmp_path / "a", VIDEO, "p")
    con_p, ig.pedidos[:] = list(ig.pedidos), []
    _bajar(tmp_path / "b", VIDEO, "reel")
    assert con_p == ig.pedidos and con_p


# --- la sesión «para mirar» no sirve: se prueba sin sesión -------------------------------------------------------------
def test_sesion_trabada_prueba_sin_sesion_y_baja(ig, tmp_path):
    """Con un «confirmá que sos vos» pendiente Instagram contesta 400 y yt-dlp no sigue: antes fallaba todo."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("trabada"))
    ig.publicar(VIDEO, 2)
    video, info = _bajar(tmp_path, VIDEO)
    assert info["id"] == VIDEO and "Audio: aac" in _pistas(video)
    assert f"/p/{VIDEO}" in _sin_sesion(ig)                           # la bajó la prueba sin sesión
    assert not buscador.sesiones()["instagram"]["vencida"]            # trabada no es vencida: no se marca
    assert buscador.estado_sesion()["instagram"]                      # y el archivo de ella sigue igual


def test_sesion_trabada_y_sin_sesion_tampoco_dice_el_motivo(ig, tmp_path):
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("trabada"))
    ig.publicar(VIDEO, 2)
    ig.sin_sesion = False
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, VIDEO)
    m = str(e.value)
    assert m.startswith("Instagram no dio ese video (") and "HTTP Error 400" in m and "confirmá que sos vos" in m, m
    assert "la cuenta «para mirar»" in m and len(m) <= 300, m
    assert "github" not in m.lower() and "please report" not in m.lower()


def test_si_la_sesion_trajo_el_post_y_falla_el_video_no_culpa_al_post(ig, tmp_path):
    """Revisión: con la sesión buena, el video no está en el CDN (404). Antes probaba sin sesión, chocaba con el muro de
    Instagram y decía «puede ser de fotos, privado o borrado», tapando el motivo real."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("buena"))
    ig.publicar("Vid30N0Cdn1", 2)                                   # su MP4 no está en el CDN
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, "Vid30N0Cdn1")
    m = str(e.value)
    assert "404" in m and "fotos" not in m and "privado" not in m, m
    assert _sin_sesion(ig) == []                                      # no probó sin sesión: la sesión anduvo


@pytest.mark.parametrize("sesion", ["trabada", "frenada"])
def test_una_foto_con_la_sesion_trabada_o_frenada_dice_que_puede_ser_una_foto(ig, tmp_path, sesion):
    """Revisión: la sesión no anda (400 o 429) y sin sesión Instagram muestra el post sin video. Antes el mensaje
    hablaba solo de la sesión y ella no sabía que podía ser una foto."""
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies(sesion))
    ig.publicar(FOTO, 1)
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, FOTO)
    assert str(e.value) == material.SIN_VIDEO + " " + material.CONSEJO[sesion]       # frenada: esperá, no recargues
    assert f"/p/{FOTO}" in _sin_sesion(ig)


@pytest.mark.parametrize("sesion", ["lenta", "rota"])
def test_si_instagram_falla_al_pedir_el_post_dice_que_se_pruebe_en_un_rato(ig, tmp_path, sesion):
    """Revisión: con la sesión buena, el pedido se corta por tiempo o Instagram da 503; sin sesión choca con el muro.
    Antes decía «puede ser de fotos, privado o borrado» de un video público."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies(sesion))
    ig.publicar(VIDEO, 2)
    ig.sin_sesion = False
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, VIDEO)
    m = str(e.value)
    assert m.startswith("Instagram no respondió bien al pedir ese video (") and "Probá de nuevo en un rato" in m, m
    assert "fotos" not in m and "privado" not in m


@pytest.mark.parametrize("sesion,espera", [("trabada", "aviso pendiente"), ("frenada", "esperá un rato"),
                                           ("vieja", "venció"), ("", "iniciá sesión")])
@pytest.mark.parametrize("hijos", ["foto_y_video", "fotos"])
def test_un_carrusel_que_sin_sesion_no_muestra_videos_no_dice_carrusel_de_fotos(ig, tmp_path, sesion, espera, hijos):
    """Revisión: sin una sesión que ande, Instagram puede mostrar el carrusel sin las URLs de sus videos. Antes decía
    «carrusel de fotos, sin ningún video» (falso y definitivo): sin los datos de la sesión no se sabe."""
    from hookline.servicios import buscador, material
    if sesion:
        buscador.importar_cookies(_cookies(sesion))
    ig.publicar(CARRUSEL, 8, [ig.medio(HIJO_FOTO, 1), ig.medio(HIJO_VIDEO, 2 if hijos == "foto_y_video" else 1)])
    ig.sin_urls_sin_sesion = True
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, CARRUSEL)
    m = str(e.value)
    assert m != material.M_CARRUSEL and "puede ser una foto o un video que" in m and espera in m, m
    if sesion == "vieja":
        assert buscador.sesiones()["instagram"]["vencida"]                # y queda marcada en el panel


def test_sesion_frenada_prueba_sin_sesion_y_baja(ig, tmp_path):
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("frenada"))
    ig.publicar(VIDEO, 2)
    video, info = _bajar(tmp_path, VIDEO)
    assert info["id"] == VIDEO and f"/p/{VIDEO}" in _sin_sesion(ig)
    assert not buscador.sesiones()["instagram"]["vencida"]


def test_sesion_vencida_baja_igual_y_queda_marcada(ig, tmp_path):
    """yt-dlp ve que la sesión no vale, sigue sin sesión y baja. Antes la sesión vencida quedaba como buena en el panel
    (solo se marcaba si la descarga fallaba)."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("vieja"))
    ig.publicar(VIDEO, 2)
    video, info = _bajar(tmp_path, VIDEO)
    assert info["id"] == VIDEO
    s = buscador.sesiones()["instagram"]
    assert s["vencida"] and "bajar" in s["motivo"]


def test_sesion_vencida_y_sin_sesion_sin_video_dice_que_vencio(ig, tmp_path):
    """Si la sesión venció, eso va primero (el 20/11 los videos daban «no hay video» sin sesión)."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("vieja"))
    ig.publicar(FOTO, 1)
    with pytest.raises(ValueError, match="venció"):
        _bajar(tmp_path, FOTO)


def test_sesion_vencida_y_sin_sesion_tampoco_dice_que_vencio(ig, tmp_path):
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("vieja"))
    ig.publicar(VIDEO, 2)
    ig.sin_sesion = False
    with pytest.raises(ValueError, match="venció"):
        _bajar(tmp_path, VIDEO)
    assert buscador.sesiones()["instagram"]["vencida"]
    assert buscador.estado_sesion()["instagram"]                      # el archivo de ella, intacto (era una copia)
    # yt-dlp ya probó sin sesión en esa misma vuelta: no se repite (pedidos sin sesión de más = freno de Instagram)
    assert _sin_sesion(ig).count(f"/p/{VIDEO}") == 1, ig.pedidos


def test_si_ella_carga_otra_sesion_mientras_baja_la_nueva_no_queda_vencida(ig, tmp_path):
    """Revisión: mientras yt-dlp pedía con la sesión vieja (que venció), ella cargó una nueva. La nueva no venció."""
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("vieja"))
    ig.publicar(VIDEO, 2)
    ig.al_pedir_el_post = lambda: buscador.importar_cookies(_cookies("nueva"))
    video, info = _bajar(tmp_path, VIDEO)                             # baja igual, sin sesión
    assert info["id"] == VIDEO
    assert not buscador.sesiones()["instagram"]["vencida"]


def test_una_foto_sin_sesion_dice_que_puede_ser_una_foto(ig, tmp_path):
    ig.publicar(FOTO, 1)
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, FOTO)
    assert "puede ser una foto" in str(e.value) and "Sesión de Instagram" in str(e.value)


def test_un_post_que_no_existe_no_queda_con_el_mensaje_mudo(ig, tmp_path):
    from hookline.servicios import buscador
    buscador.importar_cookies(_cookies("buena"))
    ig.sin_sesion = False
    with pytest.raises(ValueError) as e:
        _bajar(tmp_path, "N0Ex1st3aaa")
    assert str(e.value) == "Instagram dice que ese post no existe: lo borraron o el link está mal copiado."


# --- piezas sueltas ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("datos,que", [
    ({"items": [{"media_type": 1}]}, "foto"),
    ({"items": [{"media_type": 1, "video_versions": [{"url": "x"}]}]}, ""),           # dice foto pero trae video
    ({"items": [{"media_type": 2}]}, ""),                                              # video (aunque no traiga URL)
    ({"items": [{"media_type": 8, "carousel_media": [{"media_type": 1}, {"media_type": 1}]}]}, "carrusel"),
    ({"items": [{"media_type": 8, "carousel_media": [{"media_type": 1}, {"media_type": 2}]}]}, ""),
    ({"items": [{"media_type": 8, "carousel_media": [{"media_type": 1}, "raro"]}]}, ""),
    ({"items": [{"media_type": 8, "carousel_media": []}]}, ""),
    ({"items": [{}]}, ""), ({"items": []}, ""), ({"items": "x"}, ""), ({}, ""), (None, ""), ("x", ""), (False, ""),
    ({"items": [None]}, ""), ({"items": [{"media_type": "1"}]}, ""),
])
def test_que_es_solo_dice_fotos_si_esta_seguro(datos, que):
    from hookline.servicios import material
    assert material._que_es(datos) == que


def test_el_filtro_del_carrusel():
    from hookline.servicios import material
    f = material._un_solo_video()
    assert f({"playlist": "Post by x", "playlist_id": "C"}, incomplete=True) is None      # el carrusel mismo
    # yt-dlp mira cada uno dos veces antes de bajarlo: primero sin su número en el carrusel, después con el número
    for n in (None, 1):
        assert f({"id": "F", "formats": [], "playlist_index": n}, incomplete=True) == "es una foto"
    for n in (None, 2):
        assert f({"id": "V1", "formats": [{"url": "u"}], "playlist_index": n}, incomplete=True) is None
        assert f({"id": "V2", "formats": [{"url": "u"}], "playlist_index": n and 3}, incomplete=True) == "ya hay un video"
    assert f({"id": "V1", "formats": [{"url": "u"}], "url": "u", "playlist_index": 2}) is None   # el mismo, completo
    assert f({"id": None, "formats": [{"url": "u"}]}, incomplete=True) is None                # sin código: no lo frena
    g = material._un_solo_video()                                     # cada descarga empieza de cero
    assert g({"id": "V2", "formats": [{"url": "u"}]}, incomplete=True) is None


def test_los_mensajes_entran_en_lo_que_muestra_el_panel(monkeypatch):
    """La API corta los errores en 300 caracteres (la vista previa los muestra así): ningún mensaje se pasa, ni con el
    detalle más largo."""
    from hookline.servicios import buscador, material
    largo = "x" * 400
    for sesion in (True, False):
        monkeypatch.setattr(buscador, "estado_sesion", lambda s=sesion: {"instagram": s, "tiktok": s})
        for avisos in ([], [f"ERROR: [Instagram] X: Video info extraction failed: HTTP Error {c}: {largo}"
                            for c in (400, 403, 404, 429, 500)], ["ERROR: [Instagram] X: Video info extraction failed: "
                                                                  + largo],
                       ["WARNING: The provided Instagram account cookies are no longer valid"]):
            for error in (largo, "There is no video in this post " + largo, "No video formats found! " + largo,
                          "login required " + largo, "private " + largo, "[Errno 28] No space left on device"):
                for avs in ([a] for a in avisos or [""]):
                    for sin in (True, False):
                        for link in ("https://www.instagram.com/p/X/", "https://www.tiktok.com/@a/video/1",
                                     "https://www.pinterest.com/pin/1/"):
                            m = material.motivo_ytdlp(RuntimeError(error), link, [a for a in avs if a], sin_sesion=sin)
                            assert len(m) <= 300, (len(m), m)
    for m in [material.M_FOTO, material.M_CARRUSEL, material.M_SIN_ESPACIO] + [
            material.SIN_VIDEO + " " + c for c in material.CONSEJO.values()]:
        assert len(m) <= 300, m


def test_el_chip_del_error_al_bajar_no_se_sale_de_la_pantalla():
    """Revisión: los mensajes son más largos y el chip de Material → Ejemplos no cortaba renglón (se salía de la
    pantalla y lo que había que hacer quedaba afuera)."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html").read_text(encoding="utf-8")
    linea = next(r for r in html.splitlines() if "const bajando = (MT.bajando" in r)
    assert "white-space:normal" in linea


@pytest.mark.parametrize("error,queda", [
    ("ERROR: [Instagram] DgDwFQHByVM: No video formats found!; please report this issue on  https://github.com/yt-dlp/"
     "yt-dlp/issues?q= , filling out the appropriate issue template. Confirm you are on the latest version using  "
     "yt-dlp -U", "No video formats found!"),
    ("ERROR: [Instagram] X: Video info extraction failed: HTTP Error 400: Bad Request (caused by <HTTPError 400: Bad "
     "Request>)", "Video info extraction failed: HTTP Error 400: Bad Request"),
    ("ERROR: [generic] Unable to download webpage: HTTP Error 404: Not Found (caused by <HTTPError 404>)",
     "Unable to download webpage: HTTP Error 404: Not Found"),
    ("no se guardó", "no se guardó"), ("", ""), (None, ""),
    ("ERROR: " + "x" * 500, "x" * 140),
])
def test_el_detalle_es_corto_y_sin_links(error, queda):
    from hookline.servicios import material
    assert material._detalle(error) == queda


# --- con yt-dlp de mentira: cuándo se prueba sin sesión y cuándo no -------------------------------------------------------
class _YDLFalla:
    """Falla siempre con la sesión; sin sesión, según `sin`. Anota con qué cookies lo abrieron."""
    usados: list = []
    sin = "falla"

    def __init__(self, o):
        self.o = o
        _YDLFalla.usados.append(o.get("cookiefile"))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, link, download=False):
        if self.o.get("cookiefile") or _YDLFalla.sin == "falla":
            raise RuntimeError("ERROR: [Instagram] X: Video info extraction failed: HTTP Error 429: Too Many Requests")
        destino = self.o["outtmpl"].replace("%(ext)s", "mp4")
        open(destino, "wb").write(b"video")
        return {"id": "X", "requested_downloads": [{"filepath": destino}]}


@pytest.fixture
def falso(monkeypatch):
    from hookline.servicios import buscador, material
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setattr(material, "tiene_audio", lambda v: True)
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDLFalla))
    _YDLFalla.usados.clear()
    _YDLFalla.sin = "falla"
    return _YDLFalla


def test_tiktok_no_repite(falso, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    buscador.importar_cookies('[{"domain": ".tiktok.com", "name": "sessionid", "value": "tt"}]')
    with pytest.raises(ValueError):
        material.bajar_video("https://www.tiktok.com/@a/video/1", tmp_path)
    assert len(falso.usados) == 1 and falso.usados[0]


def test_sin_sesion_de_instagram_en_el_archivo_no_repite(falso, tmp_path):
    """El archivo tiene solo la sesión de TikTok: la primera vuelta ya fue sin sesión de Instagram."""
    from hookline.servicios import buscador, material
    buscador.importar_cookies('[{"domain": ".tiktok.com", "name": "sessionid", "value": "tt"}]')
    assert buscador.estado_sesion() == {"instagram": False, "tiktok": True}
    with pytest.raises(ValueError):
        material.bajar_video("https://www.instagram.com/p/X/", tmp_path)
    assert len(falso.usados) == 1 and falso.usados[0]


def test_con_sesion_de_instagram_prueba_una_vez_sin_sesion(falso, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    falso.sin = "anda"
    video, _ = material.bajar_video("https://www.instagram.com/p/X/", tmp_path, "ejemplo")
    assert video.read_bytes() == b"video"
    assert len(falso.usados) == 2 and falso.usados[0] and falso.usados[1] is None


def test_si_sin_sesion_tampoco_no_insiste_mas(falso, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    with pytest.raises(ValueError) as e:
        material.bajar_video("https://www.instagram.com/p/X/", tmp_path, "ejemplo")
    assert len(falso.usados) == 2 and "frenó la cuenta «para mirar»" in str(e.value)


def test_sin_cookies_no_hay_segunda_vuelta(falso, tmp_path):
    from hookline.servicios import material
    with pytest.raises(ValueError):
        material.bajar_video("https://www.instagram.com/p/X/", tmp_path, "ejemplo")
    assert falso.usados == [None]


def test_pinterest_no_prueba_sin_la_sesion_de_instagram(falso, tmp_path):
    from hookline.servicios import buscador, material
    buscador.importar_cookies(_cookies("buena"))
    with pytest.raises(ValueError):
        material.bajar_video("https://www.pinterest.com/pin/123/", tmp_path, "ejemplo")
    assert len(falso.usados) == 1
