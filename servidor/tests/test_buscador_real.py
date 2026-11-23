"""El buscador con un Chromium de verdad contra páginas que imitan a Instagram, TikTok y Pinterest (servidas acá).
Prueba el navegador, los selectores, el scroll, la detección de login y de sesión cortada, Pinterest, y que no se
presente como «HeadlessChrome». Contra las redes reales se prueba en el servidor: `hookline buscador-probar`."""
import http.server
import io
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image

try:
    import playwright.sync_api  # noqa: F401
    HAY_PW = True
except ImportError:
    HAY_PW = False


def _chrome():
    for p in ("/opt/pw-browsers/chromium-1243/chrome-linux64/chrome",):
        if Path(p).is_file():
            return p
    for n in ("chromium", "chromium-browser", "google-chrome"):
        if shutil.which(n):
            return shutil.which(n)
    return ""


pytestmark = pytest.mark.skipif(not (HAY_PW and _chrome()), reason="hace falta playwright y Chromium")
AGENTES = []


def _jpg():
    b = io.BytesIO()
    Image.new("RGB", (90, 160), (200, 40, 90)).save(b, "JPEG")
    return b.getvalue()


_MP4 = []


def _mp4():
    if not _MP4:
        from hookline import medios
        import tempfile
        f = Path(tempfile.mkdtemp()) / "m.mp4"
        subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=360x640:rate=30:duration=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(f)], check=True)
        _MP4.append(f.read_bytes())
    return _MP4[0]


def _paginas(base):
    img = lambda n: f"{base}/img/{n}.jpg"  # noqa: E731
    return {
        "/ig": "".join(f'<a href="/reel/RE{n}/"><img src="{img(n)}" alt="pov {n}"></a>' for n in range(5))
        + f'<a href="/p/FOTO1/"><img src="{img(90)}"></a>'                                     # foto: no entra
        + f'<a href="/p/VID1/"><svg aria-label="Clip"></svg><img src="{img(91)}"></a>'         # video en /p/: entra
        + f'<a href="/reel/SINIMG/"></a>',                                                       # sin miniatura: no
        "/pin-vacio": "<p>Pinterest · Registrate para ver más ideas</p>",
        "/ig-muro": '<form id="loginForm"><input name="username"></form>',
        "/tt": "".join(f'<a href="https://www.tiktok.com/@memero/video/7{n}00"><img src="{img(10 + n)}"></a>'
                       for n in range(4)),
        "/pin": '<div class="registrate">Registrate para ver más</div>'
        + "".join(f'<a href="/pin/9876543210{n}/"><img src="{img(20 + n)}" alt="meme pin {n}"></a>' for n in range(6))
        + '<a href="/pin/abc/"><img src="x"></a>',
    }


@pytest.fixture
def web():
    paginas = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            AGENTES.append(self.headers.get("User-Agent", ""))
            ruta = self.path.split("?")[0]
            if ruta == "/vid/meme.mp4":
                cuerpo, tipo = _mp4(), "video/mp4"
            elif ruta.startswith("/img/"):
                cuerpo, tipo = _jpg(), "image/jpeg"
            elif ruta == "/accounts/login/":
                cuerpo, tipo = b"<p>login</p>", "text/html"
            elif ruta == "/bloqueado":                       # como Instagram al servidor: 429 sin cuerpo
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif ruta == "/prohibido":
                cuerpo = b"<html><body>Access Denied</body></html>"
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(cuerpo)
                return
            elif ruta == "/resource/BaseSearchResource/get/":    # la búsqueda interna de Pinterest
                import urllib.parse as up
                qs = up.parse_qs(self.path.split("?", 1)[1])
                opts = json.loads(qs["data"][0])["options"]
                assert opts["scope"] == "videos" and self.headers.get("X-Requested-With") == "XMLHttpRequest"
                base = f"http://127.0.0.1:{self.server.server_address[1]}"
                if opts["query"] == "meme real":            # lo que devolvió Pinterest de verdad al servidor (23/11)
                    res = [{"id": f"e1_AfqPShzuSG4ScyIabfQ-WkP-mJr4wmDATl4b8VqXHBM1hQ-Po3O{n}", "type": "pin",
                            "grid_title": f"pov real {n}", "story_pin_data": None, "is_video": None,
                            "images": {"236x": {"url": f"{base}/img/5{n}.jpg"}, "474x": {"url": f"{base}/img/6{n}.jpg"}},
                            "videos": {"id": "5332127969571817029", "video_list": {
                                "V_HLSV4": {"url": f"{base}/vid/x.m3u8", "width": 576, "duration": 5666,
                                            "thumbnail": f"{base}/img/7{n}.jpg"},
                                "V_720P": {"url": f"{base}/vid/meme.mp4", "width": 720, "duration": 5666}}}}
                           for n in range(3)]
                    res.append({"id": "e1_foto_sin_video", "type": "pin", "images": {"474x": {"url": f"{base}/img/9.jpg"}},
                                "videos": None, "story_pin_data": None})
                    cuerpo = json.dumps({"resource_response": {"data": {"results": res}}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(cuerpo)
                    return
                res = [{"id": "111111111", "images": {"474x": {"url": f"{base}/img/40.jpg"}}, "videos": {"x": 1},
                        "grid_title": "pov " + opts["query"]},
                       {"id": "222222222", "images": {"236x": {"url": f"{base}/img/41.jpg"}}, "story_pin_data": {}},
                       {"id": "333333333", "images": {"474x": {"url": f"{base}/img/42.jpg"}}},          # foto
                       {"id": "malo", "images": {}}, "basura"]
                cuerpo, tipo = json.dumps({"resource_response": {"data": {"results": res}}}).encode(), "application/json"
            elif ruta == "/corta":                           # corta la conexión sin contestar (chrome-error)
                self.close_connection = True
                return
            elif ruta == "/ig-redirige":
                self.send_response(302)
                self.send_header("Location", "/accounts/login/?next=/reels/")
                self.end_headers()
                return
            elif ruta in paginas:
                cuerpo, tipo = f"<html><body>{paginas[ruta]}</body></html>".encode(), "text/html"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    paginas.update(_paginas(base))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield base
    srv.shutdown()


@pytest.fixture
def buscador(monkeypatch, web):
    from hookline.servicios import buscador as b
    monkeypatch.setattr(b, "_chrome", _chrome)
    monkeypatch.setattr(b, "PUERTO", 9391)
    rutas = {("ig", "@memes"): "/ig", ("ig", "@privada"): "/ig-redirige", ("ig", "#muro"): "/ig-muro",
             ("tt", "@memero"): "/tt", ("pin", "meme pov"): "/pin", ("ig", "meme pov"): "/bloqueado",
             ("tt", "meme pov"): "/corta", ("ig", "@prohibida"): "/prohibido", ("pin", "sin dom"): "/pin-vacio",
             ("pin", "meme real"): "/pin-vacio"}
    monkeypatch.setattr(b, "_url", lambda red, q: web + rutas.get((red, q), "/nada"))
    AGENTES.clear()
    yield b
    from hookline import almacen
    subprocess.run(["pkill", "-f", str(almacen.dir_de("navegador", "perfil"))], check=False)


def _con_pagina(b, fn):
    sp = b._playwright()
    b.abrir()
    with sp() as p:
        _, ctx = b._contexto(p)
        b._cargar_sesion(ctx)
        pg = ctx.new_page()
        try:
            return fn(pg, ctx)
        finally:
            pg.close()


def test_junta_reels_tiktoks_y_pines_y_no_es_headless(buscador):
    b = buscador
    def correr(pg, ctx):
        hay = b.estado_sesion()
        ig, m1, _ = b._juntar(ctx, "ig", "@memes", b._url("ig", "@memes"), set(), hay, pasadas=2)
        tt, m2, _ = b._juntar(ctx, "tt", "@memero", b._url("tt", "@memero"), set(), hay, pasadas=2)
        pin, m3, info = b._juntar(ctx, "pin", "meme pov", b._url("pin", "meme pov"), set(), hay, pasadas=2)
        assert info["estado"] == 200 and info["final"].endswith("/pin")
        return ig, tt, pin, (m1, m2, m3)
    ig, tt, pin, muros = _con_pagina(b, correr)
    assert muros == (False, False, False)
    assert sorted(x["link"] for x in ig) == sorted([f"https://www.instagram.com/reel/RE{n}/" for n in range(5)]
                                                    + ["https://www.instagram.com/reel/VID1/"])
    assert [x["link"] for x in tt] == [f"https://www.tiktok.com/@memero/video/7{n}00" for n in range(4)]
    assert [x["link"] for x in pin] == [f"https://www.pinterest.com/pin/9876543210{n}/" for n in range(6)]
    assert all(x["src"].endswith(".jpg") for x in ig + tt + pin) and pin[0]["alt"] == "meme pin 0"
    assert pin[0]["fuente"] == "pin meme pov"
    assert AGENTES and not any("Headless" in a for a in AGENTES), set(AGENTES)


def test_login_y_sesion_cortada(buscador):
    b = buscador
    b.importar_cookies("sessionid=111%3Aabc%3A22; csrftoken=tok; ds_user_id=111")
    def correr(pg, ctx):
        hay = b.estado_sesion()
        r1 = b._juntar(ctx, "ig", "@privada", b._url("ig", "@privada"), set(), hay, pasadas=1)[:2]   # redirige a login
        r2 = b._juntar(ctx, "ig", "#muro", b._url("ig", "#muro"), set(), hay, pasadas=1)[:2]         # formulario de login
        assert not b.sesiones()["instagram"]["vencida"]                  # todavía tiene la cookie: no es «vencida»
        ctx.clear_cookies(name="sessionid")                              # Instagram borra la sesión (la cerró)
        r3 = b._juntar(ctx, "ig", "@memes", b._url("ig", "@memes"), set(), hay, pasadas=1)[:2]
        return r1, r2, r3
    r1, r2, r3 = _con_pagina(b, correr)
    assert r1 == ([], True) and r2 == ([], True) and r3 == ([], True)
    s = b.sesiones()["instagram"]
    assert s["vencida"] and "cerró la sesión" in s["motivo"]


def test_busqueda_completa_con_pinterest_hasta_el_banco(buscador, monkeypatch):
    from hookline.ia import tareas
    from hookline.servicios import fabrica
    b = buscador
    b.sumar_fuentes(["ig @memes", "tt @memero", "pin: meme pov", "pin: meme pov", "xx: nada"])
    assert [f["texto"] for f in b.fuentes()] == ["ig @memes", "tt @memero", "pin: meme pov"]
    b.estado(estado="buscando", objetivo=1, vuelta=1, acumulado=0, lote="", hojas=0, hechas=0)
    b._buscar({})
    e = b.estado()
    assert e["estado"] == "filtrando", e
    assert e["juntados"] == 16 and e["hojas"] == 1                     # 6 de Instagram, 4 de TikTok, 6 de Pinterest
    t = [x for x in tareas.pendientes_claude_code() if x.tipo == "filtrar"]
    assert len(t) == 1
    cands = json.loads((b.almacen.dir_de("candidatos", e["lote"]) / "candidatos.json").read_text())
    n_pin = next(c["n"] for c in cands if "pinterest.com/pin/" in c["link"])
    n_tt = next(c["n"] for c in cands if "tiktok.com" in c["link"])
    b._tanda_hecha(e["lote"], 1, {"sirven": [{"n": n_pin, "titulo": "pov pin", "cat": "pov"},
                                             {"n": n_tt, "titulo": "pov tt", "cat": "pov"}]})
    links = {x["link"]: x for x in fabrica.links()}
    assert any("pinterest.com/pin/" in k for k in links) and any("tiktok.com" in k for k in links)
    assert all(x["ficha"] == "ok" and x["mini"] for x in links.values())
    assert b.estado()["estado"] == "listo"
    fs = {f["texto"]: f for f in b.fuentes()}
    assert fs["pin: meme pov"]["vistos"] == 6 and fs["pin: meme pov"]["sirven"] == 1


def test_probar_en_el_servidor(buscador):
    b = buscador
    rs = b.probar(["ig @memes", "tt @memero", "pin: meme pov", "ig @privada", "cualquier cosa"])
    assert [(r["fuente"], r.get("ok"), r.get("videos")) for r in rs] == [
        ("ig @memes", True, 6), ("tt @memero", True, 4), ("pin meme pov", True, 6), ("ig @privada", False, 0),
        ("cualquier cosa", False, None)]
    assert rs[3]["pidio_login"] and "/accounts/login/" in rs[3]["final"]
    assert rs[2]["ejemplos"][0].startswith("https://www.pinterest.com/pin/")
    from hookline.servicios import fabrica
    assert fabrica.links() == []                                      # probar no guarda nada


def test_abre_aunque_haya_quedado_el_candado_de_otro_contenedor(buscador):
    """Lo que pasó en el server: el perfil está en el volumen y después de un rebuild quedaba SingletonLock apuntando
    al contenedor viejo; Chrome salía con código 21 («perfil en uso por otra computadora») y el buscador no abría."""
    import os
    from hookline import almacen
    b = buscador
    perfil = almacen.dir_de("navegador", "perfil")
    os.symlink("contenedor-viejo-12345-99", perfil / "SingletonLock")
    (perfil / "SingletonCookie").write_text("x")
    b.abrir()
    assert b._abierto()
    assert os.readlink(perfil / "SingletonLock") != "contenedor-viejo-12345-99"     # el candado ahora es el nuevo


def test_si_chrome_no_arranca_dice_por_que(buscador, monkeypatch, tmp_path):
    b = buscador
    falso = tmp_path / "chrome-roto"
    falso.write_text("#!/bin/sh\necho '[1:1:ERROR:algo.cc:1] Falta una biblioteca: libnss3.so' >&2\nexit 127\n")
    falso.chmod(0o755)
    monkeypatch.setattr(b, "_chrome", lambda: str(falso))
    with pytest.raises(ValueError) as e:
        b.abrir()
    assert "código 127" in str(e.value) and "libnss3" in str(e.value) and "chrome.log" in str(e.value)


def test_errores_de_las_redes_se_explican_y_no_arrastran_a_la_siguiente(buscador):
    """Lo que dio el server (23/11): Instagram contestó con error, TikTok no cargó y Pinterest quedó «interrumpida por
    otra navegación» porque usaban la misma pestaña. Cada fuente va en su pestaña y el error dice el código."""
    b = buscador
    rs = b.probar(["ig: meme pov", "tt: meme pov", "pin: meme pov", "ig @prohibida", "ig @memes"])
    por = {r["fuente"]: r for r in rs}
    assert "429" in por["ig meme pov"]["error"] and "limitando" in por["ig meme pov"]["error"]
    assert "TikTok no cargó la página (ERR_" in por["tt meme pov"]["error"], por["tt meme pov"]
    assert por["pin meme pov"]["ok"] and por["pin meme pov"]["videos"] == 6          # no la arrastró la anterior
    assert "403" in por["ig @prohibida"]["error"] and "bloqueó" in por["ig @prohibida"]["error"]
    assert por["ig @memes"]["ok"] and por["ig @memes"]["estado"] == 200


def test_la_busqueda_avisa_por_que_no_encontro(buscador):
    b = buscador
    b.sumar_fuentes(["ig: meme pov", "tt: meme pov"])
    b.estado(estado="buscando", objetivo=1, vuelta=1, acumulado=0, lote="", hojas=0, hechas=0)
    b._buscar({})
    e = b.estado()
    assert e["estado"] == "error" and "429" in e["mensaje"] and "TikTok no cargó" in e["mensaje"], e["mensaje"]


def test_pinterest_sin_pines_en_la_pagina_usa_su_busqueda_interna_y_probar_deja_captura(buscador):
    b = buscador
    rs = b.probar(["pin: sin dom", "ig @privada"])
    r = rs[0]
    assert r["ok"] and r["api"] and r["videos"] == 2, r                       # solo los que tienen video
    assert r["ejemplos"] == ["https://www.pinterest.com/pin/111111111/", "https://www.pinterest.com/pin/222222222/"]
    assert Path(r["captura"]).is_file() and Path(r["captura"]).stat().st_size > 1000
    assert "Registrate" in r["texto"]
    assert not rs[1]["ok"] and "login" in rs[1]["texto"] and Path(rs[1]["captura"]).is_file()


def test_que_paso_explica_cada_error():
    from hookline.servicios import buscador as b
    assert "pide sesión o que bloquea" in b.que_paso("ig", 0, "net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://x")
    assert "ERR_CONNECTION_RESET" in b.que_paso("tt", 0, "net::ERR_CONNECTION_RESET")
    assert "429" in b.que_paso("pin", 429) and "403" in b.que_paso("ig", 403) and "502" in b.que_paso("tt", 502)


def test_pinterest_sin_sesion_ids_codificados_hasta_bajar_el_video(buscador, monkeypatch, web, tmp_path):
    """Lo que dio el servidor: sin sesión Pinterest no pone links en la página y su búsqueda interna manda ids
    codificados («e1_…», que yt-dlp no entiende) con el video directo en video_list. Se guardan el pin y su video, y la
    descarga usa el video directo."""
    from hookline.ia import tareas
    from hookline.servicios import fabrica, material
    monkeypatch.setattr(material, "ESQUEMAS_VIDEO", ("https://", web + "/"))
    b = buscador
    r = b.probar(["pin: meme real"])[0]
    assert r["ok"] and r["api"] and r["videos"] == 3, r                      # la foto queda afuera
    assert r["ejemplos"][0] == "https://www.pinterest.com/pin/e1_AfqPShzuSG4ScyIabfQ-WkP-mJr4wmDATl4b8VqXHBM1hQ-Po3O0/"
    b.sumar_fuentes(["pin: meme real"])
    b.estado(estado="buscando", objetivo=1, vuelta=1, acumulado=0, lote="", hojas=0, hechas=0)
    b._buscar({})
    e = b.estado()
    assert e["estado"] == "filtrando" and e["juntados"] == 3, e
    assert [t for t in tareas.pendientes_claude_code() if t.tipo == "filtrar"]
    b._tanda_hecha(e["lote"], 1, {"sirven": [{"n": 1, "titulo": "pov", "cat": "pov"}]})
    [x] = fabrica.links()
    from hookline.db import sesion
    from hookline.modelos import LinkBanco
    with sesion() as s:
        d = s.get(LinkBanco, x["link"]).datos
    assert d["video"] == web + "/vid/meme.mp4" and d["dur"] == 5.7 and d["mini"]   # el MP4, no el HLS
    video, info = material.bajar_video(x["link"], tmp_path)                   # yt-dlp de verdad, contra el MP4
    assert video.is_file() and video.stat().st_size > 1000 and info.get("url", "").endswith("/vid/meme.mp4")


def test_video_de_pin_con_hls_solo_y_con_idea_pin():
    from hookline.servicios import buscador as b
    hls = {"videos": {"video_list": {"V_HLSV3_MOBILE": {"url": "https://v1.pinimg.com/x.m3u8", "duration": 3000,
                                                          "thumbnail": "https://i.pinimg.com/t.jpg"}}}}
    assert b.video_de_pin(hls) == ("https://v1.pinimg.com/x.m3u8", 3.0, "https://i.pinimg.com/t.jpg")
    idea = {"videos": None, "story_pin_data": {"pages": [{"blocks": [{"video": {"video_list": {
        "V_EXP7": {"url": "https://v1.pinimg.com/a.mp4", "width": 720, "duration": 8000}}}}]}]}}
    assert b.video_de_pin(idea) == ("https://v1.pinimg.com/a.mp4", 8.0, "")
    assert b.video_de_pin({"videos": {"video_list": {"X": {"url": "http://evil/x.mp4"}}}}) == ("", 0.0, "")
    assert b.video_de_pin({}) == ("", 0.0, "")
