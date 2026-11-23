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


def _paginas(base):
    img = lambda n: f"{base}/img/{n}.jpg"  # noqa: E731
    return {
        "/ig": "".join(f'<a href="/reel/RE{n}/"><img src="{img(n)}" alt="pov {n}"></a>' for n in range(5))
        + f'<a href="/p/FOTO1/"><img src="{img(90)}"></a>'                                     # foto: no entra
        + f'<a href="/p/VID1/"><svg aria-label="Clip"></svg><img src="{img(91)}"></a>'         # video en /p/: entra
        + f'<a href="/reel/SINIMG/"></a>',                                                       # sin miniatura: no
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
            if ruta.startswith("/img/"):
                cuerpo, tipo = _jpg(), "image/jpeg"
            elif ruta == "/accounts/login/":
                cuerpo, tipo = b"<p>login</p>", "text/html"
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
             ("tt", "@memero"): "/tt", ("pin", "meme pov"): "/pin"}
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
        ig, m1 = b._juntar(pg, ctx, "ig", "@memes", b._url("ig", "@memes"), set(), hay, pasadas=2)
        tt, m2 = b._juntar(pg, ctx, "tt", "@memero", b._url("tt", "@memero"), set(), hay, pasadas=2)
        pin, m3 = b._juntar(pg, ctx, "pin", "meme pov", b._url("pin", "meme pov"), set(), hay, pasadas=2)
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
        r1 = b._juntar(pg, ctx, "ig", "@privada", b._url("ig", "@privada"), set(), hay, pasadas=1)   # redirige a login
        r2 = b._juntar(pg, ctx, "ig", "#muro", b._url("ig", "#muro"), set(), hay, pasadas=1)         # formulario de login
        assert not b.sesiones()["instagram"]["vencida"]                  # todavía tiene la cookie: no es «vencida»
        ctx.clear_cookies(name="sessionid")                              # Instagram borra la sesión (la cerró)
        r3 = b._juntar(pg, ctx, "ig", "@memes", b._url("ig", "@memes"), set(), hay, pasadas=1)
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
