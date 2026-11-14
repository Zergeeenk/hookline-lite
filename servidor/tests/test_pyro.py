"""Conexión con Pyro: un Pyro falso (httpx.MockTransport) con la forma de su API /api/v1."""
import io
import json

import httpx
import pytest

CLAVE = "pyro_" + "a1B2c3D4" * 5
URL = "https://pyro.test"


class PyroFalso:
    def __init__(self):
        self.pedidos = []
        self.modelos = [{"id": 1, "name": "Pamela", "niche": "humor"}, {"id": 2, "name": "Valentina", "niche": ""}]
        self.fichas = {
            1: {"id": 1, "name": "Pamela", "niche": "humor y lifestyle",
                "socialAccounts": [{"network": "instagram", "handle": "@Pamela.Ok"},
                                   {"network": "tiktok", "handle": "pamela"},
                                   {"network": "instagram", "url": "https://www.instagram.com/pame.backup/"}]},
            2: {"id": 2, "name": "Valentina", "socialAccounts": [{"network": "instagram", "handle": "valen.roux"}]},
        }
        self.archivos = []
        self.clave_ok = CLAVE
        self.fallar = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req)
        if self.fallar:
            return self.fallar(req)
        if req.url.path.startswith("/uploads/"):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0" + b"x" * 500)
        if req.headers.get("authorization") != f"Bearer {self.clave_ok}":
            return httpx.Response(401, json={"error": "invalid api key"})
        p = req.url.path
        if p == "/api/v1/models":
            return httpx.Response(200, json={"models": self.modelos})
        if p.startswith("/api/v1/models/") and p.endswith("/content"):
            mid = int(p.split("/")[4])
            cursor = req.url.params.get("cursor")
            mitad = len(self.archivos) // 2
            pagina = self.archivos[mitad:] if cursor else self.archivos[:mitad]
            return httpx.Response(200, json={"modelId": mid, "library": {
                "folders": [{"id": 3, "parentId": None, "name": "Playa", "path": "Playa"},
                            {"id": 4, "parentId": 3, "name": "Junio", "path": "Playa / Junio"},
                            {"id": 5, "parentId": None, "name": "Noche", "path": "Salidas de noche"}],
                "files": pagina, "total": len(self.archivos), "nextCursor": None if cursor else "c2"}})
        if p.startswith("/api/v1/models/"):
            mid = int(p.rsplit("/", 1)[1])
            if mid not in self.fichas:
                return httpx.Response(404, json={"error": "model not found"})
            return httpx.Response(200, json=self.fichas[mid])
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def pyro(monkeypatch):
    from hookline import pyro as cli
    from hookline.servicios import pyro as svc
    f = PyroFalso()
    monkeypatch.setattr(cli, "transporte", httpx.MockTransport(f))
    monkeypatch.setattr(svc, "PAUSA", 0)
    return f


def _cuenta(cid, **kw):
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id=cid, **kw))


def _conectar(cliente_api):
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": URL + "/", "clave": "  " + CLAVE + "\n"})
    assert r.status_code == 200, r.text
    return r.json()


# --- conexión ------------------------------------------------------------------------------------------------------
def test_conectar_guarda_cifrada_y_nunca_la_devuelve(cliente_api, pyro):
    from hookline.db import sesion
    from hookline.modelos import Ajuste
    r = _conectar(cliente_api)
    assert r["conectado"] and r["n_modelos"] == 2 and r["clave_fin"] == CLAVE[-4:] and r["url"] == URL
    for ruta in ("/api/pyro", "/api/ajustes", "/api/estado"):
        assert CLAVE not in cliente_api.get(ruta).text
    with sesion() as s:
        assert CLAVE not in json.dumps(s.get(Ajuste, "pyro_api_key").valor)


@pytest.mark.parametrize("url", ["http://pyro.test", "ftp://pyro.test", "https://", "https://u:p@pyro.test",
                                 "https://pyro.test/otra/ruta", "https://pyro.test/?x=1", "javascript:alert(1)",
                                 "https://pyro.test:99999", "https://pyro_test", "https:", "//pyro.test", "pyro.test/../x",
                                 "https://ру.test", "https://pyro.test#x"])
def test_direcciones_invalidas(cliente_api, pyro, url):
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": url, "clave": CLAVE})
    assert r.status_code == 400
    assert not pyro.pedidos


@pytest.mark.parametrize("clave", ["", "hola", "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxx", "pyro_corta", "pyro_" + "a" * 200,
                                   "pyro_aaaaaaaaaaaaaaaaaaaaaa\x00"])
def test_claves_invalidas(cliente_api, pyro, clave):
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": URL, "clave": clave})
    assert r.status_code == 400
    assert not pyro.pedidos


@pytest.mark.parametrize("url", ["pyro.test", "pyro.test/", "https://pyro.test/api/v1", "  https://pyro.test  "])
def test_direcciones_que_se_aceptan(cliente_api, pyro, url):
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": url, "clave": CLAVE})
    assert r.status_code == 200 and r.json()["url"] == URL


def test_clave_rechazada_por_pyro_no_se_guarda(cliente_api, pyro):
    pyro.clave_ok = "otra"
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": URL, "clave": CLAVE})
    assert r.status_code == 400 and "rechazó" in r.json()["error"]
    assert cliente_api.get("/api/pyro").json()["conectado"] is False


def test_cambiar_la_direccion_pide_la_clave_de_nuevo(cliente_api, pyro):
    _conectar(cliente_api)
    r = cliente_api.post("/api/pyro", json={"op": "guardar", "url": "https://otro-servidor.test"})
    assert r.status_code == 400 and "clave" in r.json()["error"]
    assert all(q.url.host == "pyro.test" for q in pyro.pedidos)          # la clave nunca fue al otro
    assert cliente_api.post("/api/pyro", json={"op": "guardar", "url": URL}).status_code == 200   # misma dirección: sí


def test_solo_admin_cambia_la_conexion(cliente_api, pyro):
    from fastapi.testclient import TestClient
    from hookline import api
    _conectar(cliente_api)
    cliente_api.post("/api/usuarios", json={"op": "crear", "email": "ed@x.com", "clave": "clave-larga-123", "rol": "editor"})
    ed = TestClient(api.app)
    ed.headers["X-CSRF-Token"] = ed.post("/api/login", json={"email": "ed@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    for op in ("guardar", "desconectar", "material_auto"):
        assert ed.post("/api/pyro", json={"op": op, "url": URL, "clave": CLAVE}).status_code == 403
    assert ed.post("/api/pyro", json={"op": "sincronizar"}).status_code == 200       # un editor sí sincroniza
    lector = TestClient(api.app)
    cliente_api.post("/api/usuarios", json={"op": "crear", "email": "le@x.com", "clave": "clave-larga-123", "rol": "lector"})
    lector.headers["X-CSRF-Token"] = lector.post("/api/login", json={"email": "le@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    assert lector.get("/api/pyro").status_code == 200
    assert lector.post("/api/pyro", json={"op": "sincronizar"}).status_code == 403


def test_errores_de_pyro_se_explican(cliente_api, pyro):
    _conectar(cliente_api)
    casos = [(lambda r: httpx.Response(500, json={}), "error (500)"),
             (lambda r: httpx.Response(200, text="<html>"), "no es JSON"),
             (lambda r: httpx.Response(200, json=[1, 2]), "inesperado"),
             (lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")), "Sin conexión"),
             (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x")), "tardó")]
    for fallar, texto in casos:
        pyro.fallar = fallar
        r = cliente_api.post("/api/pyro", json={"op": "probar"})
        assert r.status_code == 400 and texto in r.json()["error"], (texto, r.text)


def test_espera_cuando_pyro_pide_esperar(pyro, monkeypatch):
    from hookline import pyro as cli
    esperas, n = [], {"v": 0}
    monkeypatch.setattr(cli.time, "sleep", esperas.append)

    def una_vez_429(req):
        n["v"] += 1
        if n["v"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "too many requests"})
        pyro.fallar = None
        return pyro(req)
    pyro.fallar = una_vez_429
    assert len(cli.modelos(URL, CLAVE)) == 2 and esperas == [2.0]


# --- sincronizar ------------------------------------------------------------------------------------------------------
def test_sincronizar_ata_por_usuario_y_hereda_multicuentas(cliente_api, pyro):
    _cuenta("pamela.ok", usuario_ig="Pamela.Ok")
    _cuenta("pame_multi", main_id="pamela.ok")
    _cuenta("valen.roux", nicho="ya tenía nicho")
    _cuenta("suelta")
    _conectar(cliente_api)
    r = cliente_api.post("/api/pyro", json={"op": "sincronizar"}).json()
    cs = {c["id"]: c for c in cliente_api.get("/api/estado").json()["cuentas"]}
    assert cs["pamela.ok"]["pyro_modelo_id"] == 1 and cs["pamela.ok"]["pyro_modelo"] == "Pamela"
    assert cs["pamela.ok"]["nicho"] == "humor y lifestyle"                 # nicho vacío: lo trae de Pyro
    assert cs["pame_multi"]["pyro_modelo_id"] == 1                         # multicuenta hereda de su main
    assert cs["valen.roux"]["pyro_modelo_id"] == 2 and cs["valen.roux"]["nicho"] == "ya tenía nicho"
    assert cs["suelta"]["pyro_modelo_id"] is None
    assert r["atadas"] == 3
    assert [x["usuario"] for x in r["sin_cuenta"]] == ["pame.backup"]      # Pyro la tiene, Hookline no


def test_crear_la_cuenta_que_falta(cliente_api, pyro):
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    r = cliente_api.post("/api/pyro", json={"op": "crear_cuenta", "usuario": "pame.backup", "modelo_id": 1})
    assert r.status_code == 200
    c = next(c for c in cliente_api.get("/api/estado").json()["cuentas"] if c["id"] == "pame.backup")
    assert c["pyro_modelo_id"] == 1 and not c["conectada"]
    assert cliente_api.post("/api/pyro", json={"op": "crear_cuenta", "usuario": "x", "modelo_id": 999}).status_code == 400


def test_atadura_a_mano_se_respeta_y_se_suelta_si_la_modelo_desaparece(cliente_api, pyro):
    _cuenta("otra.cuenta")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    assert cliente_api.post("/api/pyro", json={"op": "atar", "cuenta": "otra.cuenta", "modelo_id": 2}).status_code == 200
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    c = lambda: next(c for c in cliente_api.get("/api/estado").json()["cuentas"] if c["id"] == "otra.cuenta")
    assert c()["pyro_modelo_id"] == 2
    pyro.modelos = pyro.modelos[:1]                                         # la borraron en Pyro
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    assert c()["pyro_modelo_id"] is None


def test_sincronizar_sin_conexion_y_con_error(cliente_api, pyro):
    assert cliente_api.post("/api/pyro", json={"op": "sincronizar"}).status_code == 400
    _conectar(cliente_api)
    pyro.fallar = lambda r: httpx.Response(500)
    assert cliente_api.post("/api/pyro", json={"op": "sincronizar"}).status_code == 400
    assert "500" in cliente_api.get("/api/pyro").json()["error"]


def test_fichas_raras_de_pyro_no_rompen(cliente_api, pyro):
    pyro.modelos = [{"id": 1, "name": None}, {"id": "x"}, "basura", {"name": "sin id"}, {"id": 2, "name": "V" * 500}]
    pyro.fichas = {1: {"socialAccounts": None}, 2: {"socialAccounts": [None, 5, {"network": "instagram", "handle": "../../etc"},
                                                                       {"network": "instagram", "handle": "a" * 300}]}}
    _cuenta("etc")
    _cuenta("a" * 30)
    _conectar(cliente_api)
    r = cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    assert r.status_code == 200
    cs = {c["id"]: c for c in cliente_api.get("/api/estado").json()["cuentas"]}
    assert cs["etc"]["pyro_modelo_id"] is None                          # '../../etc' no se confunde con 'etc'
    assert cs["a" * 30]["pyro_modelo_id"] == 2 and len(cs["a" * 30]["pyro_modelo"]) <= 200


# --- material -------------------------------------------------------------------------------------------------------------
def test_traer_material_respeta_carpetas_y_no_repite(cliente_api, pyro):
    from hookline.servicios import material, pyro as svc
    _cuenta("pamela.ok")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    pyro.archivos = [
        {"id": 10, "kind": "image", "name": "playa-1.jpg", "folderId": 4, "url": URL + "/uploads/1/playa-1.jpg"},
        {"id": 11, "kind": "video", "name": "baile.mp4", "folderId": None, "url": URL + "/uploads/1/baile.mp4"},
        {"id": 12, "kind": "image", "name": "salida", "mimetype": "image/jpeg", "folderId": 5, "url": URL + "/uploads/1/s"},
        {"id": 13, "kind": "audio", "name": "hola.ogg", "url": URL + "/uploads/1/hola.ogg"},
        {"id": 14, "kind": "image", "name": "raro.exe", "url": URL + "/uploads/1/raro.exe"},
    ]
    r = svc.importar_material("pamela.ok")
    assert r["bajados"] == 3 and r["errores"] == 0
    m = material.listar("pamela.ok")
    assert [x["nombre"] for x in m["Playa - Junio"]] == ["playa-1.jpg"]
    assert [x["nombre"] for x in m["Pyro"]] == ["baile.mp4"]
    assert m["Salidas de noche"][0]["etiquetas"] == ["noche"] and m["Salidas de noche"][0]["pyro_id"] == 12
    assert svc.importar_material("pamela.ok")["bajados"] == 0               # no repite
    bajadas = [q for q in pyro.pedidos if q.url.path.startswith("/uploads/")]
    assert len(bajadas) == 3 and all(q.headers.get("authorization") == f"Bearer {CLAVE}" for q in bajadas)


def test_archivos_de_otro_host_se_bajan_sin_la_clave(cliente_api, pyro):
    from hookline.servicios import pyro as svc
    _cuenta("pamela.ok")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    pyro.archivos = [{"id": 20, "kind": "image", "name": "a.jpg", "url": "https://cdn.otro.test/uploads/a.jpg"},
                     {"id": 21, "kind": "image", "name": "b.jpg", "url": "http://pyro.test/uploads/b.jpg"},
                     {"id": 22, "kind": "image", "name": "c.jpg", "url": "file:///etc/passwd"}]
    r = svc.importar_material("pamela.ok")
    assert r["bajados"] == 1 and r["errores"] == 2
    q = next(q for q in pyro.pedidos if q.url.host == "cdn.otro.test")
    assert "authorization" not in q.headers


def test_nombres_y_carpetas_peligrosos_no_salen_del_material(cliente_api, pyro, monkeypatch):
    from hookline import almacen
    from hookline.servicios import pyro as svc
    _cuenta("pamela.ok")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    monkeypatch.setattr(PyroFalso, "__call__", PyroFalso.__call__)
    pyro.archivos = [{"id": 30, "kind": "image", "name": "../../../hookline.db.jpg", "folderId": 99,
                      "url": URL + "/uploads/x"},
                     {"id": 31, "kind": "image", "name": "x.jpg", "folderId": 6, "url": URL + "/uploads/y"}]
    real = pyro.__class__.__call__

    def con_carpeta_mala(self, req):
        r = real(self, req)
        if req.url.path.endswith("/content"):
            d = r.json()
            d["library"]["folders"].append({"id": 6, "path": "../../_secreto / .. / modelo"})
            return httpx.Response(200, json=d)
        return r
    monkeypatch.setattr(PyroFalso, "__call__", con_carpeta_mala)
    assert svc.importar_material("pamela.ok")["bajados"] == 2
    base = almacen.dir_de("material", "pamela.ok").resolve()
    for p in almacen.raiz().rglob("*.jpg"):
        assert p.resolve().is_relative_to(base)


def test_archivo_gigante_se_corta(cliente_api, pyro, monkeypatch):
    from hookline import pyro as cli
    from hookline.servicios import pyro as svc
    monkeypatch.setattr(cli, "MAX_ARCHIVO", 1000)
    _cuenta("pamela.ok")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    pyro.archivos = [{"id": 40, "kind": "image", "name": "g.jpg", "url": URL + "/uploads/g.jpg"}]
    real = PyroFalso.__call__
    monkeypatch.setattr(PyroFalso, "__call__", lambda self, req: httpx.Response(200, content=io.BytesIO(b"x" * 5000).read())
                        if req.url.path.startswith("/uploads/") else real(self, req))
    r = svc.importar_material("pamela.ok")
    assert r["bajados"] == 0 and r["errores"] == 1
    from hookline import almacen
    assert not list(almacen.dir_de("material", "pamela.ok").rglob("g*"))


def test_material_de_una_multicuenta_va_a_su_main_y_sin_atar_avisa(cliente_api, pyro):
    _cuenta("pamela.ok")
    _cuenta("multi", main_id="pamela.ok")
    _cuenta("suelta")
    _conectar(cliente_api)
    cliente_api.post("/api/pyro", json={"op": "sincronizar"})
    assert cliente_api.post("/api/pyro", json={"op": "material", "cuenta": "multi"}).status_code == 200
    r = cliente_api.post("/api/pyro", json={"op": "material", "cuenta": "suelta"})
    assert r.status_code == 400 and "atada" in r.json()["error"]
    assert cliente_api.post("/api/pyro", json={"op": "material", "cuenta": "../x"}).status_code == 404


# --- lo que ve Pyro ---------------------------------------------------------------------------------------------------------
def test_los_eventos_llevan_la_modelo_de_pyro(monkeypatch):
    from hookline import config
    from hookline.comun import evento
    from hookline.db import sesion
    from hookline.modelos import Evento
    monkeypatch.setenv("HOOKLINE_WEBHOOK_URL", "https://pyro.test/h")
    config.recargar()
    _cuenta("pamela.ok", pyro_modelo_id=7)
    evento("pieza.publicada", pieza="p1", cuenta="pamela.ok")
    evento("pieza.publicada", pieza="p2", cuenta="no-existe")
    with sesion() as s:
        ds = [e.datos for e in s.query(Evento).order_by(Evento.id)]
    assert ds[0]["pyro_modelo_id"] == 7 and ds[1]["pyro_modelo_id"] is None


def test_el_puente_de_pyro_firma_quien_aprobo(cliente_api, cuenta_conectada, foto):
    import shutil
    from fastapi.testclient import TestClient
    from hookline import almacen, api
    from hookline.servicios import piezas
    tok = cliente_api.post("/api/usuarios", json={"op": "token", "nombre": "pyro", "rol": "editor"}).json()["token"]
    shutil.copy(foto, almacen.dir_de("subir", cuenta_conectada) / "post.jpg")
    piezas.escanear()
    pid = piezas.listar(cuenta_conectada)[0]["id"]
    c = TestClient(api.app)
    h = {"Authorization": f"Bearer {tok}", "X-Hookline-Actor": "Carla (pyro:2) <script>alert(1)</script>" + "x" * 200}
    assert c.post("/api/pieza", json={"op": "programar", "id": pid, "cuando": "2099-01-01T20:00"}, headers=h).status_code == 200
    from hookline.db import sesion
    from hookline.modelos import Pieza
    with sesion() as s:
        por = s.get(Pieza, pid).aprobada_por
    assert por.startswith("token:pyro · Carla (pyro:2) scriptalert(1)") and "<" not in por and len(por) < 120
