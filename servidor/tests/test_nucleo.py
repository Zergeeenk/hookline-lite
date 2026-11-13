"""Seguridad, almacenamiento, cola de trabajos, OAuth y token de Instagram."""
import time

import pytest


def test_cifrado_y_claves():
    from hookline import seguridad as sg
    c = sg.cifrar("secreto")
    assert c != "secreto" and sg.descifrar(c) == "secreto"
    h = sg.hash_clave("una-clave-larga")
    assert sg.verificar_clave("una-clave-larga", h)
    assert not sg.verificar_clave("otra-clave-larga", h)
    with pytest.raises(ValueError):
        sg.hash_clave("corta")


def test_url_publica_firmada_y_vence():
    from hookline import almacen
    p = almacen.dir_de("subir", "modelo1") / "a.jpg"
    p.write_bytes(b"x")
    rel = almacen.relativa(p)
    url = almacen.url_publica(rel, minutos=5)
    assert url.startswith("https://hookline.test/publico/")
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert almacen.verificar_publica(rel, q["v"][0], q["f"][0]) == p
    assert almacen.verificar_publica(rel, q["v"][0], "mala") is None
    assert almacen.verificar_publica(rel, str(int(time.time()) - 1), almacen.seguridad.firmar(rel, int(time.time()) - 1)) is None


def test_ruta_no_sale_del_volumen():
    from hookline import almacen
    with pytest.raises(ValueError):
        almacen.ruta("../../etc/passwd")
    with pytest.raises(ValueError):
        almacen.dir_de("subir", "..")


def test_trabajos_clave_unica_y_reintento():
    from hookline import trabajos
    from hookline.db import sesion
    from hookline.modelos import Trabajo
    corridas = []

    @trabajos.manejador("prueba")
    def _f(d):
        corridas.append(d["n"])
        if len(corridas) == 1:
            raise RuntimeError("falla la primera")

    assert trabajos.encolar("prueba", {"n": 1}, clave="k1") is not None
    assert trabajos.encolar("prueba", {"n": 1}, clave="k1") is None      # duplicado: no se crea
    t = trabajos.tomar("w1", ["prueba"])
    assert t and trabajos.tomar("w2", ["prueba"]) is None                  # nadie más lo toma
    trabajos.correr(t)
    with sesion() as s:
        x = s.get(Trabajo, t.id)
        assert x.estado == "pendiente" and x.intentos == 1
        x.ejecutar_en = x.creado                                          # que se pueda reintentar ya
    t2 = trabajos.tomar("w1", ["prueba"])
    trabajos.correr(t2)
    with sesion() as s:
        assert s.get(Trabajo, t.id).estado == "hecho"
    assert corridas == [1, 1]


def test_oauth_state_obligatorio_y_de_un_uso(meta, monkeypatch):
    from hookline import comun
    from hookline.servicios import cuentas
    comun.guardar_ajuste("meta_app_id", "123")
    comun.guardar_ajuste("meta_app_secret", "s3cr3t", cifrado=True)
    url = cuentas.url_conectar()
    assert "redirect_uri=https%3A%2F%2Fhookline.test%2Finstagram%2Fcallback" in url
    import urllib.parse
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    with pytest.raises(ValueError):
        cuentas.conectar_con_codigo("CODE", "")          # sin state: no (en Lite sí pasaba)
    with pytest.raises(ValueError):
        cuentas.conectar_con_codigo("CODE", "inventado")
    monkeypatch.setattr(cuentas.ig, "canjear_codigo", lambda *a: {"access_token": "T", "user_id": "9", "permisos": "x",
                                                                  "obtenido": time.time(), "refrescado": time.time(),
                                                                  "vence": time.time() + 86400 * 59})
    monkeypatch.setattr(cuentas.ig, "perfil_con_token", lambda t: {"username": "nueva.cuenta", "user_id": 9,
                                                                   "account_type": "BUSINESS"})
    assert cuentas.conectar_con_codigo("CODE", state)[0] == "nueva.cuenta"
    with pytest.raises(ValueError):
        cuentas.conectar_con_codigo("CODE", state)       # de un solo uso


def test_token_cifrado_y_refresco(cuenta_conectada, meta):
    from hookline import instagram as ig
    from hookline.db import sesion
    from hookline.modelos import CredencialIG
    with sesion() as s:
        c = s.get(CredencialIG, cuenta_conectada)
        assert "TOKEN-SECRETO" not in c.token_cifrado
        c.obtenido = c.refrescado = time.time() - 6 * 86400                 # viejo: toca refrescar
    assert ig.Cliente(cuenta_conectada).token() == "TOKEN-NUEVO"
    assert any(u.endswith("/refresh_access_token") for _, u, _, _ in meta.llamadas)


def test_token_vencido_se_marca(cuenta_conectada):
    from hookline import instagram as ig
    from hookline.db import sesion
    from hookline.modelos import CredencialIG
    with sesion() as s:
        s.get(CredencialIG, cuenta_conectada).vence = time.time() - 10
    with pytest.raises(ig.TokenVencido):
        ig.Cliente(cuenta_conectada).token()
    with sesion() as s:
        assert s.get(CredencialIG, cuenta_conectada).vencido


def test_firma_webhook():
    from hookline.eventos import firmar
    import hashlib
    import hmac
    assert firmar(b"{}", "1", "k") == "sha256=" + hmac.new(b"k", b"1.{}", hashlib.sha256).hexdigest()
