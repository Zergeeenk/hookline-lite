"""Iniciar sesión en Instagram con usuario y contraseña (en vez de pegar cookies), contra un Instagram de mentira
servido acá y con un Chromium de verdad: login directo, doble factor (formulario clásico y app de una página que valida
por detrás y tarda), «confirmá que sos vos» con código por mail, aprobar desde la app, contraseña incorrecta, cuenta
suspendida, el cartel de cookies que tapa el botón, un campo escondido antes del visible, la contraseña y el código
nunca en un log, el estado atado al trabajo de la cola (en espera detrás de una búsqueda, worker que se murió),
cancelar y el reintento automático cuando la sesión vence (sin insistir con lo que necesita a una persona)."""
import http.server
import json
import logging
import socket
import subprocess
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from test_buscador_real import HAY_PW, _chrome

HOST = "www.instagram-prueba.test"         # contiene «instagram»: las cookies cuentan como de Instagram
OTRA = "www.tiktok-prueba.test"            # otra red: sus cookies no se tocan


def _pagina(cuerpo: str) -> bytes:
    return f"<html><body>{cuerpo}</body></html>".encode()


CARTEL = ("<script>document.querySelector('[name=password]').addEventListener('input', () => {"
          "const d = document.createElement('div'); d.style = 'position:fixed;inset:0;background:#fff;z-index:99';"
          "d.innerHTML = '<p>Allow the use of cookies from Instagram on this browser?</p>"
          "<button type=\"button\">Allow all cookies</button>';"
          "d.querySelector('button').onclick = () => { document.cookie = 'consentido=1; path=/'; d.remove(); };"
          "document.body.appendChild(d); }, {once: true});</script>")
SPA_2FA = ("<p>Enter the 6-digit code we sent to your number ending in 34.</p>"
           '<form id="f"><input name="verificationCode" aria-label="Security Code"><button type="submit">Confirm'
           '</button></form><p id="e"></p><script>document.getElementById("f").onsubmit = async ev => {'
           'ev.preventDefault(); const c = document.querySelector("[name=verificationCode]").value;'
           'const r = await fetch("/api/2fa", {method: "POST", body: "c=" + c}); const j = await r.json();'
           'if (j.ok) location.href = "/accounts/onetap/?next=%2F";'
           'else document.getElementById("e").textContent = "Please check the security code and try again."; };</script>')
APROBAR = ("<h2>Check your notifications on another device</h2><p>Approve this login to continue.</p><script>"
           "setInterval(async () => { const j = await (await fetch('/api/aprobado', {method: 'POST'})).json();"
           "if (j.ok) location.href = '/'; }, 500);</script>")


class _IG(http.server.BaseHTTPRequestHandler):
    op: dict = {}            # variantes de cada test
    vistos: dict = {}        # lo que llegó (cookies del inicio de sesión)

    def _responder(self, cuerpo: bytes = b"", ir: str = "", sesion: str = "", tipo: str = "text/html"):
        self.send_response(302 if ir else 200)
        if ir:
            self.send_header("Location", ir)
        if sesion:
            for c in (f"sessionid={sesion}%3A1%3Axyz; Path=/; HttpOnly", "csrftoken=tok; Path=/", "ds_user_id=1; Path=/"):
                self.send_header("Set-Cookie", c)
        self.send_header("Content-Type", tipo)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _login_html(self) -> str:
        clave = '<input name="password" type="password"%s>' % (" disabled" if self.op.get("clave_deshabilitada") else "")
        if self.op.get("escondido"):               # un input de usuario oculto ANTES del visible
            return ('<input name="username" style="display:none"><form method="post" action="/accounts/login/ajax/">'
                    f'<input name="email" aria-label="Email">{clave}<button type="submit">Log in</button></form>')
        return ('<form method="post" action="/accounts/login/ajax/"><input name="username" aria-label="Phone number, '
                f'username, or email">{clave}<button type="submit">Log in</button></form>'
                + (CARTEL if self.op.get("cartel") else ""))

    def do_GET(self):
        ruta, _, qs = self.path.partition("?")
        if ruta == "/accounts/login/":
            error = ("<p id='slfErrorAlert'>Sorry, your password was incorrect. Please double-check your password.</p>"
                     if "error=1" in qs else
                     "<p>Please wait a few minutes before you try again.</p>" if "error=esperar" in qs else "")
            return self._responder(_pagina(self._login_html() + error))
        if ruta == "/accounts/login/two_factor":
            error = "<p>This code isn't valid. Please check the code and try again.</p>" if "error=1" in qs else ""
            return self._responder(_pagina(
                "<h2>Enter Security Code</h2><p>Enter the 6-digit code we sent to your number ending in 12.</p>" + error
                + '<form method="post" action="/accounts/login/two_factor"><input name="verificationCode" '
                  'aria-label="Security Code"%s><button type="submit">Confirm</button></form>'
                % (" disabled" if self.op.get("codigo_deshabilitado") else "")))
        if ruta == "/accounts/login/two_factor_spa":
            return self._responder(_pagina(SPA_2FA))
        if ruta == "/accounts/login/aprobar":
            return self._responder(_pagina(APROBAR))
        if ruta == "/accounts/onetap/":
            return self._responder(_pagina("<p>Save your login info?</p><button>Save info</button><button>Not now</button>"))
        if ruta == "/accounts/suspended/":
            return self._responder(_pagina("<h2>We suspended your account</h2><button>Disagree with decision</button>"
                                           "<button>Log out</button>"))
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
            if "sessionid=suspendida" in (self.headers.get("Cookie") or ""):
                return self._responder(ir="/accounts/suspended/?next=%2F")
            return self._responder(_pagina("<p>Instagram inicio</p>"))
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        largo = int(self.headers.get("Content-Length") or 0)
        datos = urllib.parse.parse_qs(self.rfile.read(largo).decode())
        campo = lambda k: (datos.get(k) or [""])[0]  # noqa: E731
        if self.path == "/accounts/login/ajax/":
            self.vistos["cookie_login"] = self.headers.get("Cookie") or ""
            usuario, clave = campo("username") or campo("email"), campo("password")
            if clave != "clave-buena":
                return self._responder(ir="/accounts/login/?error=1")
            destinos = {"dosfa": "/accounts/login/two_factor?next=%2F", "reto": "/challenge/inicio/",
                        "spa": "/accounts/login/two_factor_spa?next=%2F", "aprobar": "/accounts/login/aprobar",
                        "frenada": "/accounts/login/?error=esperar"}
            if usuario in destinos:
                return self._responder(ir=destinos[usuario])
            if usuario == "suspendida":            # Instagram deja entrar y manda a «suspendimos tu cuenta»
                return self._responder(ir="/accounts/suspended/?next=%2F", sesion="suspendida")
            return self._responder(ir="/", sesion=usuario)
        if self.path == "/accounts/login/two_factor":
            if campo("verificationCode") == "123456":
                return self._responder(ir="/", sesion="dosfa")
            return self._responder(ir="/accounts/login/two_factor?error=1")
        if self.path == "/api/2fa":                 # la app de una página: valida por detrás y a veces tarda
            time.sleep(float(self.op.get("demora") or 0))
            ok = campo("c") == "123456"
            return self._responder(json.dumps({"ok": ok}).encode(), sesion="spa" if ok else "", tipo="application/json")
        if self.path == "/api/aprobado":
            ok = bool(self.op.get("aprobado"))
            return self._responder(json.dumps({"ok": ok}).encode(), sesion="aprobar" if ok else "",
                                   tipo="application/json")
        if self.path == "/challenge/enviar":
            return self._responder(ir="/challenge/codigo/")
        if self.path == "/challenge/codigo/":
            if campo("security_code") == "654321":
                return self._responder(ir="/", sesion="reto")
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
    _IG.op, _IG.vistos = {}, {}
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


def _worker():
    """Lo que hace el worker: toma el ig_login de la cola y lo corre (con latido y toma, como en el servidor)."""
    from hookline import trabajos
    t = trabajos.tomar("prueba:1", ["ig_login"])
    assert t is not None and t.tipo == "ig_login", "no había un ig_login en la cola"
    trabajos.correr(t)


def _esperar(s, estado, segundos=60):
    fin = time.time() + segundos
    while time.time() < fin:
        if s.publico()["estado"] == estado:
            return s.publico()
        time.sleep(0.3)
    raise AssertionError(f"no llegó a {estado}: {s.publico()}")


def _seguir(s, hilo, segundos=90) -> list:
    """Los estados por los que pasa mientras corre el worker."""
    vistos, fin = [], time.time() + segundos
    while hilo.is_alive() and time.time() < fin:
        e = s.publico()["estado"]
        if not vistos or vistos[-1] != e:
            vistos.append(e)
        time.sleep(0.2)
    hilo.join(5)
    return vistos


def _ig_logins(pendientes: bool = False) -> int:
    from hookline.db import sesion
    from hookline.modelos import Trabajo
    with sesion() as s:
        q = s.query(Trabajo).filter(Trabajo.tipo == "ig_login")
        if pendientes:
            q = q.filter(Trabajo.estado == "pendiente")
        return q.count()


def _terminar_logins() -> None:
    from hookline.db import sesion
    from hookline.modelos import Trabajo
    with sesion() as s:
        for t in s.query(Trabajo).filter(Trabajo.tipo == "ig_login"):
            t.estado = "hecho"


# --- con Chromium -------------------------------------------------------------------------------------------------------------
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
def test_doble_factor_codigo_malo_pide_otro_y_el_bueno_entra(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("dosfa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    p = _esperar(ig, "pide_codigo")
    assert "ending in 12" in p["mensaje"]
    ig.dar_codigo("111 111")
    p = _esperar(ig, "pide_codigo")                               # no se corta: pide escribirlo de nuevo
    assert p["mensaje"] == ig.M_OTRO_CODIGO, p
    assert not buscador.estado_sesion()["instagram"]
    ig.dar_codigo("123456")
    h.join(60)
    assert ig.publico()["estado"] == "listo" and buscador.estado_sesion()["instagram"]


@con_chrome
def test_tres_codigos_malos_y_se_corta(ig, monkeypatch):
    ig.guardar_credenciales("dosfa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    for _ in range(3):
        _esperar(ig, "pide_codigo")
        ig.dar_codigo("000000")
    h.join(60)
    p = ig.publico()
    assert p["estado"] == "error" and p["mensaje"] == ig.M_MUCHOS_CODIGOS, p


@con_chrome
def test_si_instagram_tarda_en_validar_el_codigo_no_pide_otro(ig):
    from hookline.servicios import buscador
    _IG.op["demora"] = 6                                          # más que los 4 s que esperaba antes
    ig.guardar_credenciales("spa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    _esperar(ig, "pide_codigo")
    ig.dar_codigo("123456")
    vistos = _seguir(ig, h)
    assert ig.publico()["estado"] == "listo", ig.publico()
    assert "pide_codigo" not in vistos, vistos
    assert buscador.estado_sesion()["instagram"]


@con_chrome
def test_codigo_malo_en_la_app_de_una_pagina_pide_otro(ig):
    _IG.op["demora"] = 1
    ig.guardar_credenciales("spa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    _esperar(ig, "pide_codigo")
    ig.dar_codigo("111111")
    p = _esperar(ig, "pide_codigo")
    assert p["mensaje"] == ig.M_OTRO_CODIGO, p
    ig.dar_codigo("123456")
    h.join(90)
    assert ig.publico()["estado"] == "listo", ig.publico()


@con_chrome
def test_confirma_que_sos_vos_manda_el_codigo_por_mail(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("reto", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    p = _esperar(ig, "pide_codigo")
    assert "f***@gmail.com" in p["mensaje"]
    ig.dar_codigo("654321")
    h.join(60)
    assert ig.publico()["estado"] == "listo" and buscador.estado_sesion()["instagram"]


@con_chrome
def test_aprobar_el_inicio_desde_la_app(ig):
    ig.guardar_credenciales("aprobar", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    fin = time.time() + 60
    while ig.publico()["mensaje"] != ig.M_APROBAR:
        assert time.time() < fin, ig.publico()
        time.sleep(0.3)
    assert ig.publico()["estado"] == "iniciando"
    _IG.op["aprobado"] = True
    h.join(60)
    assert ig.publico()["estado"] == "listo", ig.publico()


@con_chrome
def test_contrasena_incorrecta_no_se_reintenta_sola_hasta_cargar_otra(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("bien", "otra-clave")
    ig.iniciar(auto=True)
    _worker()
    p = ig.publico()
    assert p["estado"] == "error" and "contraseña no es correcta" in p["mensaje"], p
    ig.estado(ultimo_auto=time.time() - 7 * 3600)
    buscador.importar_cookies("sessionid=1%3Aa%3A2; csrftoken=t")
    for motivo in ("Instagram cerró la sesión al buscar", "Venció al bajar un video de Instagram"):
        buscador.marcar_sesion("instagram", True, motivo)          # alterna buscador y descargas: igual no insiste
    assert _ig_logins(pendientes=True) == 0
    ig.guardar_credenciales("bien", "clave-buena")                 # la corrigieron en el panel
    buscador.marcar_sesion("instagram", True, "Instagram cerró la sesión al buscar")
    assert _ig_logins(pendientes=True) == 1


@con_chrome
def test_si_instagram_pide_esperar_no_se_insiste_enseguida(ig):
    ig.guardar_credenciales("frenada", "clave-buena")
    ig.iniciar()
    _worker()
    p = ig.publico()
    assert p["estado"] == "error" and "esperar unos minutos" in p["mensaje"], p
    with pytest.raises(ValueError, match="probá de nuevo en 3 minutos"):
        ig.iniciar()
    ig.estado(ultimo_auto=0)
    assert not ig.reintentar_solo()
    ig.estado(espera_hasta=time.time() - 1)
    assert ig.iniciar()["estado"] == "iniciando"


@con_chrome
def test_cuenta_suspendida_no_queda_como_iniciada(ig):
    from hookline.servicios import buscador
    ig.guardar_credenciales("suspendida", "clave-buena")
    ig._login({})
    p = ig.publico()
    assert p["estado"] == "error" and "suspendió" in p["mensaje"], p
    assert not buscador.estado_sesion()["instagram"]
    assert ig.estado().get("bloqueado")


@con_chrome
def test_el_cartel_de_cookies_no_tapa_el_boton(ig):
    _IG.op["cartel"] = True                                       # aparece recién al escribir la contraseña
    ig.guardar_credenciales("bien", "clave-buena")
    t0 = time.time()
    ig._login({})
    assert ig.publico()["estado"] == "listo", ig.publico()
    assert time.time() - t0 < 25


@con_chrome
def test_campo_de_usuario_escondido_antes_del_visible(ig):
    _IG.op["escondido"] = True
    ig.guardar_credenciales("bien", "clave-buena")
    ig._login({})
    assert ig.publico()["estado"] == "listo", ig.publico()


@con_chrome
def test_conserva_el_dispositivo_y_las_otras_redes(ig):
    from playwright.sync_api import sync_playwright

    from hookline.servicios import buscador
    buscador.abrir()
    with sync_playwright() as p:
        _, ctx = buscador._contexto(p)
        ctx.add_cookies([{"name": "mid", "value": "dispositivo-viejo", "domain": HOST, "path": "/"},
                         {"name": "sessionid", "value": "sesion-vencida", "domain": HOST, "path": "/"},
                         {"name": "sid_tt", "value": "de-tiktok", "domain": OTRA, "path": "/"}])
    ig.guardar_credenciales("bien", "clave-buena")
    ig._login({})
    assert ig.publico()["estado"] == "listo", ig.publico()
    enviada = _IG.vistos["cookie_login"]                          # Instagram ve el mismo dispositivo, sin la sesión vieja
    assert "mid=dispositivo-viejo" in enviada and "sesion-vencida" not in enviada, enviada
    with sync_playwright() as p:
        _, ctx = buscador._contexto(p)
        hay = {(c["name"], c["value"]) for c in ctx.cookies()}
    assert ("sid_tt", "de-tiktok") in hay and ("mid", "dispositivo-viejo") in hay, hay


@con_chrome
def test_si_falla_el_formulario_la_clave_no_va_a_ningun_log(ig, caplog):
    from hookline.db import sesion
    from hookline.modelos import Ajuste, Trabajo
    _IG.op["clave_deshabilitada"] = True
    ig.guardar_credenciales("bien", "Clave-Secreta-987")
    with caplog.at_level(logging.DEBUG):
        ig.iniciar()
        _worker()
    p = ig.publico()
    assert p["estado"] == "error" and "No pude escribir la contraseña" in p["mensaje"], p
    assert "Clave-Secreta-987" not in caplog.text
    with sesion() as s:
        assert "Clave-Secreta-987" not in json.dumps(s.get(Ajuste, ig.ESTADO).valor)
        assert all("Clave-Secreta-987" not in (t.error or "") for t in s.query(Trabajo))


@con_chrome
def test_si_falla_al_escribir_el_codigo_no_va_a_ningun_log(ig, caplog):
    _IG.op["codigo_deshabilitado"] = True
    ig.guardar_credenciales("dosfa", "clave-buena")
    with caplog.at_level(logging.DEBUG):
        ig.iniciar()
        h = _en_hilo(_worker)
        _esperar(ig, "pide_codigo")
        ig.dar_codigo("975310")
        h.join(90)
    p = ig.publico()
    assert p["estado"] == "error" and "No pude escribir el código" in p["mensaje"], p
    assert "975310" not in caplog.text and "clave-buena" not in caplog.text


@con_chrome
def test_cancelar_mientras_espera_el_codigo(ig):
    ig.guardar_credenciales("dosfa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    _esperar(ig, "pide_codigo")
    p = ig.cancelar()
    assert p["estado"] == "parado" and "canceló" in p["mensaje"], p
    h.join(30)
    assert not h.is_alive() and ig.publico()["estado"] == "parado"
    assert ig.iniciar()["estado"] == "iniciando"                  # se puede volver a probar enseguida


@con_chrome
def test_olvidar_la_cuenta_mientras_espera_el_codigo(ig):
    ig.guardar_credenciales("dosfa", "clave-buena")
    ig.iniciar()
    h = _en_hilo(_worker)
    _esperar(ig, "pide_codigo")
    ig.olvidar()
    h.join(30)
    assert not h.is_alive()
    p = ig.publico()
    assert p["estado"] == "parado" and not p["tiene_credenciales"], p


@con_chrome
def test_contrasena_incorrecta(ig):
    ig.guardar_credenciales("bien", "otra-clave")
    ig._login({})
    p = ig.publico()
    assert p["estado"] == "error" and "contraseña no es correcta" in p["mensaje"], p


# --- el panel (el diálogo de verdad, sacado del index.html) -----------------------------------------------------------------
PANEL = """<!doctype html><html><body><div id="contenido"></div><script>
let CP = {sesion_ig: {estado: "pide_codigo", mensaje: "Enter the 6-digit code we sent to your number ending in 12.",
          usuario: "para_mirar", tiene_credenciales: true}, sesiones: {}};
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const icn = () => ""; window.TOASTS = []; const toast = m => window.TOASTS.push(m); const fallo = e => toast("fallo: " + e);
const render = () => {}; const cargarCopias = () => {}; window.LLAMADAS = [];
async function api(url, body) { window.LLAMADAS.push([url, body || null]);
  await new Promise(r => setTimeout(r, 60));
  if (body && body.op === "ig_codigo") CP.sesion_ig = {...CP.sesion_ig, estado: "iniciando", mensaje: "Probando el código…"};
  if (body && body.op === "ig_cancelar") CP.sesion_ig = {...CP.sesion_ig, estado: "parado", mensaje: "Se canceló el inicio de sesión."};
  return {sesion_ig: CP.sesion_ig, sesiones: {}}; }
%s
</script></body></html>"""


@pytest.fixture
def panel(tmp_path):
    from playwright.sync_api import sync_playwright
    src = (Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html").read_text(encoding="utf-8")
    ini = src.index("function cargarCookies() {")
    fn = src[ini:src.index("\n}\n", ini) + 3]
    html = tmp_path / "panel.html"
    html.write_text(PANEL % fn, encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=_chrome(), args=["--no-sandbox"])
        pg = b.new_page()
        pg.goto(html.as_uri())
        yield pg
        b.close()


@con_chrome
def test_el_codigo_no_se_borra_mientras_el_panel_consulta(panel):
    pg = panel
    pg.evaluate("cargarCookies()")
    pg.wait_for_selector("#igCodigo")
    pg.wait_for_timeout(300)
    assert pg.evaluate("document.activeElement.id") == "igCodigo"
    antes = pg.evaluate("window.LLAMADAS.filter(l => l[0] === '/api/sesion_ig').length")
    pg.keyboard.type("123456", delay=450)                       # como una persona leyendo el SMS: más de 2 s
    consultas = pg.evaluate("window.LLAMADAS.filter(l => l[0] === '/api/sesion_ig').length") - antes
    assert consultas >= 1                                       # el panel consultó mientras escribía
    assert pg.input_value("#igCodigo") == "123456"
    assert pg.evaluate("document.activeElement.id") == "igCodigo"
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    enviados = pg.evaluate("window.LLAMADAS.filter(l => l[1] && l[1].op === 'ig_codigo').map(l => l[1].codigo)")
    assert enviados == ["123456"]


@con_chrome
def test_el_panel_no_manda_un_codigo_vacio_y_se_puede_cancelar(panel):
    pg = panel
    pg.evaluate("cargarCookies()")
    pg.wait_for_selector("#igCodigo")
    pg.click("[data-igcodigo]")
    pg.wait_for_timeout(200)
    assert not pg.evaluate("window.LLAMADAS.some(l => l[1] && l[1].op === 'ig_codigo')")
    assert any("código" in t for t in pg.evaluate("window.TOASTS"))
    pg.click("[data-igcancelar]")
    pg.wait_for_function("document.querySelector('#igEstado').innerText.includes('canceló')")
    assert pg.evaluate("window.LLAMADAS.some(l => l[1] && l[1].op === 'ig_cancelar')")
    assert not pg.query_selector("#igCodigo")


# --- sin navegador ------------------------------------------------------------------------------------------------------------
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
    assert p == {"estado": "iniciando", "mensaje": sesion_ig.M_EN_ESPERA, "usuario": "para_mirar",   # nadie lo tomó aún
                 "tiene_credenciales": True, "auto": False}
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirar"})   # ya está en marcha
    assert r.status_code == 400 and "Ya está iniciando" in r.json()["error"]
    r = cliente_api.post("/api/copiar", json={"op": "ig_codigo", "codigo": "123456"})       # no pidió código
    assert r.status_code == 400
    for malo in ({"usuario": "", "clave": "secreta-123456"}, {"usuario": "a b", "clave": "secreta-123456"},
                 {"usuario": "ok", "clave": "corta"}, {"usuario": ["x"], "clave": "secreta-123456"},
                 {"usuario": "ok", "clave": ["secreta-123456"]}, {"usuario": {"a": 1}}):
        assert cliente_api.post("/api/copiar", json={"op": "ig_login", **malo}).status_code == 400, malo
    assert sesion_ig.credenciales() == ("para_mirar", "secreta-123456")
    assert cliente_api.post("/api/copiar", json={"op": "ig_olvidar"}).status_code == 200
    assert sesion_ig.credenciales() is None and not sesion_ig.publico()["tiene_credenciales"]
    assert _ig_logins(pendientes=True) == 0                        # el que esperaba en la cola se canceló


def test_corregir_el_usuario_con_la_clave_guardada(cliente_api):
    from hookline.servicios import sesion_ig as s
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirrar", "clave": "secreta-123456"})
    assert r.status_code == 200
    _terminar_logins()
    s.estado(estado="error", mensaje=s.M_USUARIO)                  # Instagram: «ese usuario no existe»
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirar", "clave": ""})
    assert r.status_code == 200 and r.json()["sesion_ig"]["usuario"] == "para_mirar", r.text
    assert s.credenciales() == ("para_mirar", "secreta-123456")


def test_si_cambia_la_clave_secreta_la_pagina_de_memes_sigue_andando(cliente_api, monkeypatch):
    from hookline import config
    from hookline.servicios import sesion_ig as s
    s.guardar_credenciales("para_mirar", "secreta-123456")
    monkeypatch.setenv("HOOKLINE_CLAVE_SECRETA", Fernet.generate_key().decode())   # otra clave (base restaurada)
    config.recargar()
    r = cliente_api.get("/api/copias")
    assert r.status_code == 200 and r.json()["sesion_ig"]["tiene_credenciales"] is False, r.text
    assert cliente_api.get("/api/sesion_ig").status_code == 200
    r = cliente_api.post("/api/copiar", json={"op": "ig_login", "usuario": "para_mirar", "clave": "otra-clave-123"})
    assert r.status_code == 200, r.text
    assert s.credenciales() == ("para_mirar", "otra-clave-123")


def test_en_cola_detras_de_una_busqueda_no_se_corta_ni_se_duplica():
    from hookline import trabajos
    from hookline.servicios import sesion_ig as s
    s.guardar_credenciales("para_mirar", "clave-larga")
    trabajos.encolar("buscar", {"q": "meme"}, clave="buscar:1")
    assert trabajos.tomar("w:1", ["buscar", "ig_login"]).tipo == "buscar"     # una búsqueda corriendo…
    trabajos.encolar("buscar", {"q": "otra"}, clave="buscar:2")                # …y otra esperando
    s.iniciar()
    s.guardar_ajuste(s.ESTADO, {**s.estado(), "cuando": time.time() - 900})    # 15 min esperando su turno
    p = s.publico()
    assert p["estado"] == "iniciando" and p["mensaje"] == s.M_EN_ESPERA, p
    with pytest.raises(ValueError, match="Ya está iniciando"):
        s.iniciar()
    assert _ig_logins() == 1
    assert trabajos.tomar("w:2", ["buscar", "ig_login"]).tipo == "ig_login"   # pasa adelante de la otra búsqueda


def test_si_el_worker_se_muere_en_el_medio_queda_en_error_y_se_reintenta_solo(monkeypatch):
    from hookline import trabajos
    from hookline.db import ahora, sesion
    from hookline.modelos import Trabajo
    from hookline.servicios import buscador
    from hookline.servicios import sesion_ig as s
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    s.guardar_credenciales("para_mirar", "clave-larga")
    s.iniciar(auto=True)
    t = trabajos.tomar("otro-host:999", ["ig_login"])
    s.estado(estado="pide_codigo", mensaje="Enter the code")       # y ahí se murió (un deploy)
    with sesion() as db:
        db.get(Trabajo, t.id).tomado_hasta = ahora() - timedelta(seconds=1)   # ya no late
    assert s.publico()["estado"] == "pide_codigo"                  # recién cambió: todavía vale
    s.guardar_ajuste(s.ESTADO, {**s.estado(), "cuando": time.time() - 120, "ultimo_auto": time.time() - 7 * 3600})
    p = s.publico()
    assert p["estado"] == "error" and "se cortó" in p["mensaje"], p
    assert s.estado()["estado"] == "error"                         # quedó guardado: lo ven todos igual
    with pytest.raises(ValueError):
        s.dar_codigo("123456")
    buscador.importar_cookies("sessionid=1%3Aa%3A2; csrftoken=t")
    buscador.marcar_sesion("instagram", True, "Instagram cerró la sesión al buscar")
    assert _ig_logins(pendientes=True) == 1


def test_el_reintento_solo_vuelve_a_probar_pasadas_6_horas_con_el_mismo_motivo(monkeypatch):
    from hookline.servicios import buscador
    from hookline.servicios import sesion_ig as s
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    buscador.marcar_sesion("instagram", True, "venció")            # sin usuario y contraseña: nada
    assert _ig_logins() == 0
    s.guardar_credenciales("para_mirar", "clave-larga")
    buscador.importar_cookies("sessionid=1%3Aa%3A2; csrftoken=t")
    motivo = "Instagram cerró la sesión al buscar"
    buscador.marcar_sesion("instagram", True, motivo)
    assert s.publico()["estado"] == "iniciando" and s.publico()["auto"] and _ig_logins() == 1
    _terminar_logins()
    s.estado(estado="error", mensaje="Instagram no contestó al iniciar sesión. Probá de nuevo en un rato.")  # pasajero
    buscador.marcar_sesion("instagram", True, motivo)              # dentro de las 6 h: no insiste
    assert _ig_logins() == 1
    s.estado(ultimo_auto=time.time() - 6 * 3600 - 60)
    buscador.marcar_sesion("instagram", True, motivo)              # el mismo motivo, 6 h después: sí
    assert _ig_logins() == 2


@con_chrome
def test_un_codigo_que_nadie_escribio_frena_el_reintento_automatico(ig, monkeypatch):
    monkeypatch.setattr(ig, "ESPERA_CODIGO", 3)
    ig.guardar_credenciales("dosfa", "clave-buena")
    ig.iniciar()                                                   # a mano: estaba alguien, no frena el automático
    _worker()
    p = ig.publico()
    assert p["estado"] == "error" and p["mensaje"] == ig.M_SIN_CODIGO, p
    assert not ig.estado().get("bloqueado")
    ig.estado(ultimo_auto=0)
    assert ig.reintentar_solo()                                    # solo (como cuando la sesión vence de noche)…
    _worker()
    assert ig.publico()["mensaje"] == ig.M_SIN_CODIGO and ig.estado().get("bloqueado")
    ig.estado(ultimo_auto=0)
    assert not ig.reintentar_solo()                                # …y nadie escribió el código: no insiste
    assert ig.iniciar()["estado"] == "iniciando"                   # a mano sí, y destraba el automático
    assert not ig.estado().get("bloqueado")


def test_codigo_solo_numeros():
    from hookline.servicios import sesion_ig
    sesion_ig.guardar_credenciales("x", "clave-larga")
    sesion_ig.estado(estado="pide_codigo", mensaje="m")
    for malo in ("", "12", "abcdef", "123456789"):
        with pytest.raises(ValueError):
            sesion_ig.dar_codigo(malo)
    assert sesion_ig.dar_codigo(" 12 34 56 ")["estado"] == "iniciando"


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
             "Please check the security code and try again.": "código no es correcto",
             "Your account has been disabled for violating our terms.": "suspendió esa cuenta",
             "We suspended your account": "suspendió esa cuenta",
             "Enter the 6-digit code we sent to your number ending in 12.": None}
    for texto, espera in casos.items():
        r = s._error(texto)
        assert (r is None) if espera is None else (espera in r), (texto, r)
    assert s._pista("Hola. Enter the 6-digit code we sent to f***@gmail.com. Gracias") == \
        "Enter the 6-digit code we sent to f***@gmail.com."
    assert [s._tipo(m) for m in (s.M_CLAVE, s.M_SUSPENDIDA, s.M_CODIGO, s.M_ESPERAR, s.M_NO_DEJA, "otra")] == \
        ["definitivo", "definitivo", "codigo", "freno", "freno", "pasajero"]


def test_lo_que_va_al_log_no_trae_la_clave_ni_el_codigo():
    from hookline.servicios import sesion_ig as s
    s._secretos[:] = ["Mi\"Clave\\987", "246813"]
    try:
        t = s._limpio('ElementHandle.fill: Timeout 10000ms exceeded. Mi"Clave\\987\nCall log:\n  - fill("Mi"Clave\\987")')
        assert t == "ElementHandle.fill: Timeout 10000ms exceeded. •••"
        assert s._limpio("Error: 246813 no") == "Error: ••• no"
    finally:
        s._secretos.clear()
