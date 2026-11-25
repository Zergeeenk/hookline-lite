"""Iniciar sesión en Instagram con usuario y contraseña (en vez de pegar cookies), contra un Instagram de mentira
servido acá y con un Chromium de verdad: login directo, doble factor, «confirmá que sos vos» con código por mail,
contraseña incorrecta, la contraseña siempre cifrada y el reintento automático cuando la sesión vence."""
import http.server
import json
import socket
import subprocess
import threading
import time
import urllib.parse

import pytest

from test_buscador_real import HAY_PW, _chrome

HOST = "www.instagram-prueba.test"         # contiene «instagram»: las cookies cuentan como de Instagram


def _pagina(cuerpo: str) -> bytes:
    return f"<html><body>{cuerpo}</body></html>".encode()


LOGIN = ('<form method="post" action="/accounts/login/ajax/"><input name="username" aria-label="Phone number, '
         'username, or email"><input name="password" type="password"><button type="submit">Log in</button></form>')


class _IG(http.server.BaseHTTPRequestHandler):
    def _responder(self, cuerpo: bytes = b"", ir: str = "", cookies: bool = False):
        self.send_response(302 if ir else 200)
        if ir:
            self.send_header("Location", ir)
        if cookies:
            for c in ("sessionid=SES%3A1%3Axyz; Path=/; HttpOnly", "csrftoken=tok; Path=/", "ds_user_id=1; Path=/"):
                self.send_header("Set-Cookie", c)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta, _, qs = self.path.partition("?")
        if ruta == "/accounts/login/":
            error = "<p id='slfErrorAlert'>Sorry, your password was incorrect. Please double-check your password.</p>"
            return self._responder(_pagina(LOGIN + (error if "error=1" in qs else "")))
        if ruta == "/accounts/login/two_factor":
            error = "<p>This code isn't valid. Please check the code and try again.</p>" if "error=1" in qs else ""
            return self._responder(_pagina(
                "<h2>Enter Security Code</h2><p>Enter the 6-digit code we sent to your number ending in 12.</p>" + error
                + '<form method="post" action="/accounts/login/two_factor"><input name="verificationCode" '
                  'aria-label="Security Code"><button type="submit">Confirm</button></form>'))
        if ruta == "/challenge/inicio/":
            return self._responder(_pagina(
                "<h2>We Detected An Unusual Login Attempt</h2><p>To secure your account, we'll send you a security code."
                '</p><form method="post" action="/challenge/enviar"><button type="submit">Send Security Code</button></form>'))
        if ruta == "/challenge/codigo/":
            return self._responder(_pagina(
                "<p>Enter the 6-digit code we sent to the email address f***@gmail.com.</p>"
                '<form method="post" action="/challenge/codigo/"><input name="security_code"><button type="submit">'
                "Submit</button></form>"))
        if ruta == "/":
            return self._responder(_pagina("<p>Instagram inicio</p>"))
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        datos = urllib.parse.parse_qs(self.rfile.read(largo).decode())
        campo = lambda k: (datos.get(k) or [""])[0]  # noqa: E731
        if self.path == "/accounts/login/ajax/":
            usuario, clave = campo("username"), campo("password")
            if clave != "clave-buena":
                return self._responder(ir="/accounts/login/?error=1")
            if usuario == "dosfa":
                return self._responder(ir="/accounts/login/two_factor?next=%2F")
            if usuario == "reto":
                return self._responder(ir="/challenge/inicio/")
            return self._responder(ir="/", cookies=True)
        if self.path == "/accounts/login/two_factor":
            if campo("verificationCode") == "123456":
                return self._responder(ir="/", cookies=True)
            return self._responder(ir="/accounts/login/two_factor?error=1")
        if self.path == "/challenge/enviar":
            return self._responder(ir="/challenge/codigo/")
        if self.path == "/challenge/codigo/":
            if campo("security_code") == "654321":
                return self._responder(ir="/", cookies=True)
            return self._responder(ir="/challenge/codigo/")
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


con_chrome = pytest.mark.skipif(not (HAY_PW and _chrome()), reason="hace falta playwright y Chromium")


@pytest.fixture
def ig(monkeypatch):
    from hookline import almacen
    from hookline.servicios import buscador, sesion_ig
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IG)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    puerto = srv.server_address[1]
    monkeypatch.setattr(buscador, "_chrome", _chrome)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        monkeypatch.setattr(buscador, "PUERTO", so.getsockname()[1])
    monkeypatch.setattr(buscador, "ARGS_EXTRA", [f"--host-resolver-rules=MAP {HOST} 127.0.0.1", "--no-proxy-server"])
    monkeypatch.setattr(sesion_ig, "URL_LOGIN", f"http://{HOST}:{puerto}/accounts/login/")
    yield sesion_ig
    srv.shutdown()
    subprocess.run(["pkill", "-f", str(almacen.dir_de("navegador", "perfil"))], check=False)


def _en_hilo(fn):
    h = threading.Thread(target=fn, daemon=True)
    h.start()
    return h


def _esperar(s, estado, segundos=60):
    fin = time.time() + segundos
    while time.time() < fin:
        if s.publico()["estado"] == estado:
            return s.publico()
        time.sleep(0.3)
    raise AssertionError(f"no llegó a {estado}: {s.publico()}")


@con_chrome
def test_login_directo_guarda_la_sesion(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("@bien", "clave-buena")
    ig._login({})
    p = ig.publico()
    assert p["estado"] == "listo" and "@bien" in p["mensaje"], p
    assert buscador.estado_sesion()["instagram"]
    se = buscador.sesiones()["instagram"]
    assert se["cargada"] and not se["vencida"] and se["cargada_el"]


@con_chrome
def test_doble_factor_codigo_malo_y_despues_bueno(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("dosfa", "clave-buena")
    h = _en_hilo(lambda: ig._login({}))
    p = _esperar(ig, "pide_codigo")
    assert "ending in 12" in p["mensaje"]
    ig.dar_codigo("111 111")
    h.join(60)
    p = ig.publico()
    assert p["estado"] == "error" and "código no es correcto" in p["mensaje"], p
    assert not buscador.estado_sesion()["instagram"]
    ig.iniciar()                                                  # de nuevo, ahora con el código bueno
    h = _en_hilo(lambda: ig._login({}))
    _esperar(ig, "pide_codigo")
    ig.dar_codigo("123456")
    h.join(60)
    assert ig.publico()["estado"] == "listo" and buscador.estado_sesion()["instagram"]


@con_chrome
def test_confirma_que_sos_vos_manda_el_codigo_por_mail(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("reto", "clave-buena")
    h = _en_hilo(lambda: ig._login({}))
    p = _esperar(ig, "pide_codigo")
    assert "f***@gmail.com" in p["mensaje"]
    ig.dar_codigo("654321")
    h.join(60)
    assert ig.publico()["estado"] == "listo" and buscador.estado_sesion()["instagram"]


@con_chrome
def test_contrasena_incorrecta(ig):
    ig.guardar_credenciales("bien", "otra-clave")
    ig._login({})
    p = ig.publico()
    assert p["estado"] == "error" and "contraseña no es correcta" in p["mensaje"], p


def test_la_contrasena_queda_cifrada_y_no_sale_por_la_api(cliente_api):
    from hookline.db import sesion
    from hookline.modelos import Ajuste, Trabajo
    from hookline.servicios import sesion_ig
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirar", "clave": "secreta-123456"})
    assert r.status_code == 200 and r.json()["sesion_ig"]["estado"] == "iniciando"
    with sesion() as s:
        a = s.get(Ajuste, sesion_ig.CRED)
        assert a.cifrado and "secreta-123456" not in json.dumps(a.valor)
        assert [t.tipo for t in s.query(Trabajo).filter(Trabajo.tipo == "ig_login")] == ["ig_login"]
    for url in ("/api/sesion_ig", "/api/copias", "/api/ajustes"):
        assert "secreta-123456" not in cliente_api.get(url).text, url
    p = cliente_api.get("/api/sesion_ig").json()["sesion_ig"]
    assert p == {"estado": "iniciando", "mensaje": "Iniciando sesión en Instagram…", "usuario": "para_mirar",
                 "tiene_credenciales": True, "auto": False}
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirar"})   # ya está corriendo
    assert r.status_code == 400 and "Ya está iniciando" in r.json()["error"]
    r = cliente_api.post("/api/copiar", json={"op": "ig_codigo", "codigo": "123456"})       # no pidió código
    assert r.status_code == 400
    for malo in ({"usuario": "", "clave": "secreta-123456"}, {"usuario": "a b", "clave": "secreta-123456"},
                 {"usuario": "ok", "clave": "corta"}, {"usuario": ["x"], "clave": "secreta-123456"}):
        assert cliente_api.post("/api/copiar", json={"op": "ig_login", **malo}).status_code == 400, malo
    assert cliente_api.post("/api/copiar", json={"op": "ig_olvidar"}).status_code == 200
    assert sesion_ig.credenciales() is None and not sesion_ig.publico()["tiene_credenciales"]


def test_codigo_solo_numeros():
    from hookline.servicios import sesion_ig
    sesion_ig.guardar_credenciales("x", "clave-larga")
    sesion_ig.estado(estado="pide_codigo", mensaje="m")
    for malo in ("", "12", "abcdef", "123456789"):
        with pytest.raises(ValueError):
            sesion_ig.dar_codigo(malo)
    assert sesion_ig.dar_codigo(" 12 34 56 ")["estado"] == "iniciando"


def test_si_la_sesion_vence_se_vuelve_a_iniciar_sola_una_vez(monkeypatch):
    from hookline.db import sesion
    from hookline.modelos import Trabajo
    from hookline.servicios import buscador, sesion_ig
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    buscador.marcar_sesion("instagram", True, "venció")                   # sin usuario y contraseña: nada
    with sesion() as s:
        assert not s.query(Trabajo).filter(Trabajo.tipo == "ig_login").count()
    sesion_ig.guardar_credenciales("para_mirar", "clave-larga")
    buscador.importar_cookies("sessionid=1%3Aa%3A2; csrftoken=t")        # vuelve a estar «cargada»
    buscador.marcar_sesion("instagram", True, "Instagram cerró la sesión al buscar")
    assert sesion_ig.publico()["estado"] == "iniciando" and sesion_ig.publico()["auto"]
    sesion_ig.estado(estado="listo")
    buscador.importar_cookies("sessionid=1%3Aa%3A2; csrftoken=t")
    buscador.marcar_sesion("instagram", True, "otra vez")                 # dentro de las 6 h: no insiste
    with sesion() as s:
        assert s.query(Trabajo).filter(Trabajo.tipo == "ig_login").count() == 1


def test_un_inicio_de_sesion_cortado_se_puede_reintentar():
    from hookline.servicios import sesion_ig
    sesion_ig.guardar_credenciales("x", "clave-larga")
    sesion_ig.estado(estado="iniciando")
    sesion_ig.guardar_ajuste(sesion_ig.ESTADO, {**sesion_ig.estado(), "cuando": time.time() - 3600})
    assert sesion_ig.publico()["estado"] == "error"
    assert sesion_ig.iniciar()["estado"] == "iniciando"


def test_errores_de_instagram_en_criollo():
    from hookline.servicios import sesion_ig as s
    casos = {"Sorry, your password was incorrect. Please double-check your password.": "contraseña no es correcta",
             "Tu contraseña no es correcta. Compruébala.": "contraseña no es correcta",
             "The username you entered doesn't belong to an account.": "usuario no existe",
             "Please wait a few minutes before you try again.": "esperar unos minutos",
             "There was a problem logging you into Instagram. Please try again soon.": "no dejó iniciar sesión",
             "Hubo un problema al iniciar sesión en Instagram.": "no dejó iniciar sesión",
             "This code isn't valid. Please check the code and try again.": "código no es correcto",
             "Your account has been disabled for violating our terms.": "suspendió esa cuenta",
             "Enter the 6-digit code we sent to your number ending in 12.": None}
    for texto, espera in casos.items():
        r = s._error(texto)
        assert (r is None) if espera is None else (espera in r), (texto, r)
    assert s._pista("Hola. Enter the 6-digit code we sent to f***@gmail.com. Gracias") == \
        "Enter the 6-digit code we sent to f***@gmail.com."
