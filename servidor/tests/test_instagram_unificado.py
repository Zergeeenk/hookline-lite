"""Inicio de sesión unificado de Meta (apps nuevas): facebook.com/dialog/oauth con config_id, canje en
graph.facebook.com, cuentas de /me/assigned_instagram_accounts y todas las llamadas a graph.facebook.com."""
import shutil
import time
import urllib.parse

import pytest

APP_ID, SECRET, CONFIG = "1498439915716479", "secreto-de-prueba", "1708091530933708"
IG_MALE, IG_OTRA = "17841438182077189", "17841400000000001"


class MetaUnificada:
    """Contesta como graph.facebook.com para el inicio de sesión unificado (y publica como Meta)."""

    def __init__(self):
        self.llamadas = []
        self.cuentas = [{"id": IG_MALE}, {"id": IG_OTRA}]
        self.scopes = ["instagram_basic", "instagram_content_publish", "instagram_manage_comments"]
        self.debug_falla = False

    def __call__(self, metodo, url, params=None, datos=None, timeout=60, headers=None):
        self.llamadas.append((metodo, url, dict(params or {}), dict(datos or {})))
        u = urllib.parse.urlsplit(url)
        p = u.path
        if p.endswith("/oauth/access_token"):
            assert params["client_id"] == APP_ID and params["client_secret"] == SECRET and params["code"] == "CODE"
            return {"access_token": "SUAT-1", "token_type": "bearer"}
        if p.endswith("/debug_token"):
            if self.debug_falla:
                raise RuntimeError("caído")
            assert params["access_token"] == f"{APP_ID}|{SECRET}"
            return {"data": {"scopes": self.scopes, "expires_at": 0, "is_valid": True}}
        if p.endswith("/me/assigned_instagram_accounts"):
            return {"data": self.cuentas}
        if p.endswith("/content_publishing_limit"):
            return {"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]}
        if p.endswith("/media_publish"):
            return {"id": "MEDIA-9"}
        if p.endswith("/media") and metodo == "POST":
            return {"id": "CONT-9"}
        if p.endswith("/media"):
            return {"data": []}
        if "CONT-9" in p:
            return {"status_code": "FINISHED"}
        if "MEDIA-9" in p:
            return {"permalink": "https://www.instagram.com/p/abc/", "timestamp": "2026-11-20T23:00:00+0000"}
        for i, usuario in ((IG_MALE, "male.urtiguez"), (IG_OTRA, "otra.cuenta")):
            if p.endswith(f"/{i}"):
                return {"id": i, "username": usuario, "name": usuario.title(), "followers_count": 1200,
                        "follows_count": 10, "media_count": 5, "profile_picture_url": ""}
        if "refresh_access_token" in p:
            raise AssertionError("un token unificado no se refresca")
        return {}


@pytest.fixture
def meta_u(monkeypatch):
    from hookline import comun, instagram
    m = MetaUnificada()
    monkeypatch.setattr(instagram, "pedir", m)
    comun.guardar_ajuste("meta_app_id", APP_ID)
    comun.guardar_ajuste("meta_app_secret", SECRET, cifrado=True)
    comun.guardar_ajuste("meta_config_id", CONFIG)
    return m


def _conectar(m):
    from hookline.servicios import cuentas
    url = cuentas.url_conectar()
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return cuentas.conectar_con_codigo("CODE", q["state"]), url, q


def test_el_link_de_conectar_es_el_del_inicio_de_sesion_unificado(meta_u):
    _, url, q = _conectar(meta_u)
    assert url.startswith("https://www.facebook.com/v") and "/dialog/oauth?" in url
    assert q["client_id"] == APP_ID and q["config_id"] == CONFIG and q["response_type"] == "code"
    assert q["override_default_response_type"] == "true" and q["redirect_uri"].endswith("/instagram/callback")
    assert "scope" not in q                     # con config_id mandar scope da "Invalid Scopes"


def test_sin_config_id_sigue_el_inicio_de_sesion_con_instagram(meta_u):
    from hookline import comun
    from hookline.servicios import cuentas
    comun.guardar_ajuste("meta_config_id", "")
    assert cuentas.url_conectar().startswith("https://www.instagram.com/oauth/authorize")


def test_conecta_todas_las_cuentas_elegidas_con_token_que_no_vence(meta_u):
    from hookline import instagram as ig
    from hookline.servicios import cuentas
    (cid, info), _, _ = _conectar(meta_u)
    assert cid == "male.urtiguez" and info == "2 cuentas"
    lista = {c["id"]: c for c in cuentas.listar()}
    for k, igid in (("male.urtiguez", IG_MALE), ("otra.cuenta", IG_OTRA)):
        c = lista[k]
        assert c["conectada"] and c["puede_publicar"] and c["puede_comentar"] and not c["puede_insights"]
        assert c["dias_token"] > 3650                                       # "no vence"
        cr = ig.Cliente(k).credencial()
        assert cr.via == "unificado" and "SUAT-1" not in (cr.token_cifrado or "")
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        assert s.get(Cuenta, "male.urtiguez").ig_user_id == IG_MALE


def test_reconoce_la_cuenta_creada_desde_pyro(meta_u):
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id="male.urtiguez", pyro_modelo_id=3, pyro_modelo="Malena"))
    _conectar(meta_u)
    with sesion() as s:
        c = s.get(Cuenta, "male.urtiguez")
        assert c.pyro_modelo_id == 3 and c.ig_user_id == IG_MALE           # la misma, no una nueva
        assert s.query(Cuenta).count() == 2


def test_todo_va_a_graph_facebook_con_el_id_de_la_cuenta(meta_u):
    from hookline import instagram as ig
    from hookline.servicios import cuentas
    _conectar(meta_u)
    meta_u.llamadas.clear()
    cuentas.sincronizar("male.urtiguez")
    cli = ig.Cliente("male.urtiguez")
    cli.token(forzar_refresco=True)                                        # no intenta refrescar
    urls = [u for _, u, _, _ in meta_u.llamadas]
    assert urls and all(u.startswith("https://graph.facebook.com/") for u in urls)
    assert any(u.endswith(f"/{IG_MALE}") for u in urls)                    # perfil por id, no /me
    assert not any(u.rstrip("/").endswith("/me") or "/me/" in u for u in urls), urls


def test_publica_por_graph_facebook(meta_u, foto):
    from hookline import almacen, trabajos
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    _conectar(meta_u)
    shutil.copy(foto, almacen.dir_de("subir", "male.urtiguez") / "post.jpg")
    piezas.escanear()
    pid = piezas.listar("male.urtiguez")[0]["id"]
    meta_u.llamadas.clear()
    piezas.accion({"op": "programar", "id": pid})
    piezas.revisar_cola()
    while (t := trabajos.tomar("w", ["publicar"])):
        trabajos.correr(t)
    with sesion() as s:
        assert s.get(Pieza, pid).estado == "publicada"
    pub = [u for _, u, _, _ in meta_u.llamadas if u.endswith("/media_publish")]
    assert pub == [f"https://graph.facebook.com/{ig_version()}/{IG_MALE}/media_publish"]


def ig_version():
    from hookline.config import config
    return config().ig_version


def test_sin_cuentas_elegidas_explica_que_hacer(meta_u):
    meta_u.cuentas = []
    with pytest.raises(ValueError, match="ninguna cuenta de Instagram"):
        _conectar(meta_u)


def test_ids_raros_de_meta_se_ignoran(meta_u):
    meta_u.cuentas = [{"id": "../../me"}, {"id": None}, "basura", {"id": IG_MALE}, {"id": IG_MALE}]
    (cid, info), _, _ = _conectar(meta_u)
    assert cid == "male.urtiguez" and info == ""


def test_si_debug_token_falla_igual_conecta(meta_u):
    from hookline.servicios import cuentas
    meta_u.debug_falla = True
    _conectar(meta_u)
    c = next(c for c in cuentas.listar() if c["id"] == "male.urtiguez")
    assert c["conectada"] and c["dias_token"] > 3650


def test_el_state_sigue_siendo_obligatorio_y_de_un_uso(meta_u):
    from hookline.servicios import cuentas
    with pytest.raises(ValueError):
        cuentas.conectar_con_codigo("CODE", "inventado")
    url = cuentas.url_conectar()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["state"]
    cuentas.conectar_con_codigo("CODE", state)
    with pytest.raises(ValueError):
        cuentas.conectar_con_codigo("CODE", state)


def test_ajustes_guardan_el_id_de_configuracion(cliente_api):
    r = cliente_api.post("/api/ajustes", json={"config_id": " 1708-0915 30933708x "})
    assert r.status_code == 200 and r.json()["config_id"] == "170809153093370"[:0] + "1708091530933708"
    assert cliente_api.post("/api/ajustes", json={"config_id": ""}).json()["config_id"] == ""


def test_permisos_normalizados():
    from hookline import instagram as ig
    p = ig.normalizar_permisos(["instagram_content_publish", "instagram_basic", "ads_read"])
    assert ig.P_PUBLICAR in p and "instagram_business_basic" in p and "ads_read" in p
    assert ig.normalizar_permisos("") == "" and ig.normalizar_permisos(None) == ""
    assert ig.normalizar_permisos("instagram_basic,instagram_basic") == "instagram_basic,instagram_business_basic"


def test_el_token_viejo_de_instagram_sigue_refrescandose(cuenta_conectada, meta):
    """Las cuentas conectadas con el método de antes no cambian."""
    from hookline import instagram as ig
    from hookline.db import sesion
    from hookline.modelos import CredencialIG
    with sesion() as s:
        cr = s.get(CredencialIG, cuenta_conectada)
        cr.obtenido = cr.refrescado = time.time() - 10 * 86400
    assert ig.Cliente(cuenta_conectada).token() == "TOKEN-NUEVO"
    assert ig.Cliente(cuenta_conectada)._url("x").startswith("https://graph.instagram.com/")
