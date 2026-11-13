"""Intentar romperlo: basura en cada endpoint, ataques, permisos, concurrencia, fallas de Meta y de Claude.

Regla general: NUNCA un 500, nunca una fuga de secretos, nunca publicar dos veces ni sin aprobación.
"""
import json
import shutil
import string
import threading
import time
import urllib.parse

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

SECRETOS = ("TOKEN-SECRETO", "MUY-SECRETO-META")

# JSON cualquiera (incluye tipos incorrectos, anidados, unicode raro, números enormes)
JSON = st.recursive(
    st.none() | st.booleans() | st.integers(min_value=-10**20, max_value=10**20) | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=40),
    lambda hijos: st.lists(hijos, max_size=4) | st.dictionaries(st.text(max_size=12), hijos, max_size=5),
    max_leaves=12)
OPS = ["juntar", "tipo", "orden", "separar", "caption", "programar", "cancelar", "borrar", "jueces", "crear_local",
       "guardar", "sincronizar", "desconectar", "borrar", "actualizar", "sugerir", "responder", "ocultar", "saltar",
       "link", "guardar_links", "votar", "previa", "borrar_link", "hacer_guardados", "armar", "ubicar", "a_bandeja",
       "borrar_version", "modelo_material", "audio_material", "nueva_carpeta", "bajar", "etiquetar", "descartar",
       "crear", "desactivar", "token", "revocar_token", "importar_cookies", "agregar_encontrados", "", None, 7]
CLAVES = ["op", "id", "ids", "cuenta", "tipo", "archivos", "archivo", "caption", "cuando", "comentario", "texto",
          "link", "n", "voto", "motivo", "sec", "nombre", "etiquetas", "main", "dia", "reels", "historias", "url",
          "email", "clave", "rol", "items", "app_id", "app_secret", "motor_ia", "encargo_aprueba", "nicho"]
CUERPO = st.fixed_dictionaries({"op": st.sampled_from(OPS)},
                               optional={k: JSON for k in CLAVES if k != "op"})
RUTAS_JSON = ["/api/pieza", "/api/cuenta", "/api/comentario", "/api/tendencias", "/api/chat", "/api/repartir",
              "/api/encargo", "/api/copiar", "/api/material", "/api/conectar", "/api/ajustes", "/api/usuarios"]


@pytest.fixture
def con_datos(cliente_api, cuenta_conectada, foto, meta):
    """Un sistema con cosas adentro: cuenta conectada, una pieza, una cuenta multi."""
    from hookline import almacen
    from hookline.servicios import piezas
    shutil.copy(foto, almacen.dir_de("subir", cuenta_conectada) / "post.jpg")
    piezas.escanear()
    cliente_api.post("/api/cuenta", json={"op": "crear_local", "id": "multi1", "main": cuenta_conectada})
    return cliente_api


def _sin_500_ni_fugas(r):
    assert r.status_code < 500, (r.status_code, r.text[:300])
    for s in SECRETOS:
        assert s not in r.text


# --- 1) basura en todos los endpoints --------------------------------------------------------------------------------------
@settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))
@given(ruta=st.sampled_from(RUTAS_JSON), cuerpo=CUERPO)
def test_fuzz_endpoints_json_nunca_500(con_datos, ruta, cuerpo):
    if ruta == "/api/usuarios" and cuerpo.get("op") == "desactivar":
        cuerpo["id"] = 999999          # no desactivar a la dueña (cerraría la sesión del fuzz)
    if cuerpo.get("op") in ("buscar",):
        return
    r = con_datos.post(ruta, json=cuerpo)
    _sin_500_ni_fugas(r)


@settings(max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
@given(cuerpo=JSON)
def test_fuzz_cuerpos_que_no_son_objetos(con_datos, cuerpo):
    for ruta in RUTAS_JSON:
        _sin_500_ni_fugas(con_datos.post(ruta, json=cuerpo))


@settings(max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
@given(crudo=st.binary(max_size=300))
def test_fuzz_bytes_crudos_como_json(con_datos, crudo):
    for ruta in ("/api/pieza", "/api/cuenta", "/api/copiar"):
        _sin_500_ni_fugas(con_datos.post(ruta, content=crudo, headers={"Content-Type": "application/json"}))


@settings(max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
@given(q=st.dictionaries(st.sampled_from(["cuenta", "p", "nombre", "sec", "que", "id", "descargar"]),
                         st.text(max_size=60), max_size=4))
def test_fuzz_query_strings(con_datos, q):
    qs = urllib.parse.urlencode(q)
    for ruta in ("/api/stats", "/api/calendario", "/api/comentarios", "/api/tendencias", "/api/material", "/media",
                 "/mini", "/api/piezas"):
        _sin_500_ni_fugas(con_datos.get(f"{ruta}?{qs}"))
    for ruta in ("/api/subir", "/api/material_subir", "/api/copiar_subir"):
        _sin_500_ni_fugas(con_datos.post(f"{ruta}?{qs}", content=b"\x00\x01basura"))


# --- 2) ataques a archivos -------------------------------------------------------------------------------------------------
TRAVERSAL = ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "/etc/passwd", "para-subir/../hookline.db",
             "para-subir/modelo1/../../hookline.db", "para-subir/modelo1/%2e%2e/%2e%2e/hookline.db",
             "....//....//etc/passwd", "para-subir\\..\\hookline.db", "\x00", "material/../../../etc/hosts",
             ".candado-bandeja", "trabajo/ia", "hookline.db-wal", "C:\\Windows\\win.ini", "~/.ssh/id_rsa"]


@pytest.mark.parametrize("p", TRAVERSAL)
def test_traversal_media_mini_publico(con_datos, p):
    for ruta in ("/media", "/mini"):
        r = con_datos.get(f"{ruta}?p={urllib.parse.quote(p)}")
        assert r.status_code in (400, 404), (ruta, p, r.status_code)
    import time as _t
    from hookline import seguridad
    v = int(_t.time()) + 60
    r = con_datos.get(f"/publico/{urllib.parse.quote(p)}?v={v}&f={seguridad.firmar(p, v)}")   # aun con firma válida
    assert r.status_code in (400, 404)


def test_symlink_que_sale_del_volumen(con_datos, tmp_path):
    from hookline import almacen
    afuera = tmp_path / "secreto.txt"
    afuera.write_text("NO")
    link = almacen.dir_de("subir", "modelo1") / "link.jpg"
    link.symlink_to(afuera)
    assert con_datos.get("/media?p=para-subir/modelo1/link.jpg").status_code == 404


@pytest.mark.parametrize("nombre", ["../../x.jpg", "/abs/x.jpg", "..", ".", "", "a" * 400 + ".jpg", "foto.jpg.exe",
                                    "x.php", "con.jpg", "ñandú 🙂.jpg", "x\x00.jpg", "....jpg"])
def test_nombres_de_archivo_raros(con_datos, nombre):
    from hookline import almacen
    r = con_datos.post(f"/api/subir?cuenta=modelo1&nombre={urllib.parse.quote(nombre)}", content=b"\xff\xd8\xffbasura")
    _sin_500_ni_fugas(r)
    for f in almacen.raiz().rglob("*"):
        assert f.resolve().is_relative_to(almacen.raiz())


def test_publico_firma_vencida_o_ajena(con_datos):
    from hookline import almacen, seguridad
    rel = "para-subir/modelo1/post.jpg"
    url = almacen.url_publica(rel, 5)
    camino = urllib.parse.urlparse(url)
    assert con_datos.get(camino.path + "?" + camino.query).status_code == 200
    q = urllib.parse.parse_qs(camino.query)
    otro = "para-subir/modelo1/otro.jpg"
    assert con_datos.get(f"/publico/{otro}?v={q['v'][0]}&f={q['f'][0]}").status_code == 404
    viejo = int(time.time()) - 1
    assert con_datos.get(f"{camino.path}?v={viejo}&f={seguridad.firmar(rel, viejo)}").status_code == 404


# --- 3) permisos --------------------------------------------------------------------------------------------------------------
def _todas_las_rutas_post():
    from hookline import api
    return sorted({r.path for r in api.app.routes if "POST" in getattr(r, "methods", set())
                   and r.path not in ("/api/login", "/api/logout")})


def test_todo_post_pide_login_y_csrf(con_datos):
    from fastapi.testclient import TestClient
    from hookline import api
    anon = TestClient(api.app)
    for ruta in _todas_las_rutas_post():
        assert anon.post(ruta, json={"op": "x"}).status_code in (401, 415), ruta
        r = con_datos.post(ruta, json={"op": "x"}, headers={"X-CSRF-Token": "falso"})
        assert r.status_code in (403, 415), (ruta, r.status_code)


def test_lector_no_cambia_nada_y_editor_no_administra(con_datos):
    from fastapi.testclient import TestClient
    from hookline import api
    for email, rol in (("lector@x.com", "lector"), ("editor@x.com", "editor")):
        con_datos.post("/api/usuarios", json={"op": "crear", "email": email, "clave": "clave-larga-123", "rol": rol})
    lector, editor = TestClient(api.app), TestClient(api.app)
    lector.headers["X-CSRF-Token"] = lector.post("/api/login", json={"email": "lector@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    editor.headers["X-CSRF-Token"] = editor.post("/api/login", json={"email": "editor@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    for ruta in _todas_las_rutas_post():
        assert lector.post(ruta, json={"op": "x"}).status_code == 403, ruta
    for ruta in ("/api/ajustes", "/api/usuarios"):
        assert editor.post(ruta, json={"op": "token", "nombre": "x", "motor_ia": "api"}).status_code == 403
    assert editor.get("/api/usuarios").status_code == 403
    assert editor.get("/api/tareas_ia").status_code == 403
    assert editor.post("/api/cuenta", json={"op": "borrar", "id": "multi1"}).status_code == 403


def test_token_revocado_y_sesion_cerrada(con_datos):
    from fastapi.testclient import TestClient
    from hookline import api
    tok = con_datos.post("/api/usuarios", json={"op": "token", "nombre": "pyro"}).json()["token"]
    pyro = TestClient(api.app)
    h = {"Authorization": f"Bearer {tok}"}
    assert pyro.get("/api/estado", headers=h).status_code == 200
    tid = con_datos.get("/api/usuarios").json()["tokens"][0]["id"]
    con_datos.post("/api/usuarios", json={"op": "revocar_token", "id": tid})
    assert pyro.get("/api/estado", headers=h).status_code == 401
    con_datos.post("/api/logout")
    assert con_datos.get("/api/estado").status_code == 401


def test_login_fuerza_bruta_no_entra(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    c = TestClient(api.app)
    for clave in ["", "a", "una-clave-larg", "UNA-CLAVE-LARGA", "una-clave-larga ", None, 123, ["x"], {"$ne": ""}]:
        r = c.post("/api/login", json={"email": "duena@hookline.test", "clave": clave})
        assert r.status_code in (401, 429), clave
    for email in ["", None, "' OR 1=1 --", "DUENA@hookline.test "]:
        assert c.post("/api/login", json={"email": email, "clave": "x"}).status_code in (401, 429)
    # después de muchos intentos fallidos, ni la clave correcta entra por un rato (frena la fuerza bruta)
    assert c.post("/api/login", json={"email": "duena@hookline.test", "clave": "una-clave-larga"}).status_code == 429


def test_secretos_nunca_en_respuestas(con_datos):
    con_datos.post("/api/ajustes", json={"app_id": "1", "app_secret": "MUY-SECRETO-META"})
    for ruta in ("/api/estado", "/api/ajustes", "/api/usuarios", "/api/tareas_ia", "/api/stats?cuenta=modelo1",
                 "/api/piezas", "/api/copias", "/api/actividad"):
        _sin_500_ni_fugas(con_datos.get(ruta))


# --- 4) publicar: concurrencia y fallas de Meta ------------------------------------------------------------------------------
def _programada(cliente, cuando=""):
    pid = cliente.get("/api/piezas").json()["piezas"][0]["id"]
    r = cliente.post("/api/pieza", json={"op": "programar", "id": pid, "caption": "hola", "cuando": cuando})
    assert r.status_code == 200, r.text
    return pid


def test_muchos_workers_una_sola_publicacion(con_datos, meta):
    from hookline import trabajos
    from hookline.servicios import piezas
    _programada(con_datos)
    hs = [threading.Thread(target=piezas.revisar_cola) for _ in range(8)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    corridos = []

    def worker(n):
        while True:
            t = trabajos.tomar(f"w{n}", ["publicar"])
            if not t:
                return
            corridos.append(t.id)
            trabajos.correr(t)
    hs = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert len(corridos) == 1
    assert sum(1 for _, u, _, _ in meta.llamadas if u.endswith("/media_publish")) == 1


def test_programar_y_publicar_dos_veces_rapido(con_datos, meta):
    from hookline import trabajos
    from hookline.servicios import piezas
    pid = _programada(con_datos)
    for _ in range(5):
        con_datos.post("/api/pieza", json={"op": "programar", "id": pid, "caption": "hola"})
        piezas.revisar_cola()
    while (t := trabajos.tomar("w", ["publicar"])):
        trabajos.correr(t)
    assert sum(1 for _, u, _, _ in meta.llamadas if u.endswith("/media_publish")) == 1


@pytest.mark.parametrize("falla", ["token", "error_contenedor", "500", "sin_red", "limite"])
def test_fallas_de_meta_dejan_error_claro(con_datos, monkeypatch, falla):
    from hookline import instagram, trabajos
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    real = instagram.pedir

    def falso(metodo, url, params=None, datos=None, **kw):
        if url.endswith("/content_publishing_limit") and falla == "limite":
            return {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
        if url.endswith("/media") and metodo == "POST":
            if falla == "token":
                raise instagram.TokenVencido("expirado")
            if falla == "500":
                raise instagram.ErrorInstagram("Instagram respondió 500: algo")
            if falla == "sin_red":
                raise RuntimeError("Sin conexión con Instagram: timeout")
            return {"id": "CONT-MAL"}
        if "CONT-MAL" in url:
            return {"status_code": "ERROR", "status": "formato"}
        return real(metodo, url, params, datos, **kw)
    monkeypatch.setattr(instagram, "pedir", falso)
    pid = _programada(con_datos)
    piezas.revisar_cola()
    trabajos.correr(trabajos.tomar("w", ["publicar"]))
    with sesion() as s:
        x = s.get(Pieza, pid)
        assert x.estado == "error" and x.error and not x.media_id
        assert "TOKEN" not in x.error
    piezas.revisar_cola()                       # una pieza en error no se reintenta sola
    assert trabajos.tomar("w", ["publicar"]) is None


def test_borrar_mientras_espera_la_hora_no_publica(con_datos, meta, monkeypatch):
    from datetime import timedelta
    from hookline import trabajos
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    pid = _programada(con_datos, "2099-01-01T20:00")
    with sesion() as s:
        s.get(Pieza, pid).cuando = ahora() + timedelta(seconds=3)
    piezas.revisar_cola()
    t = trabajos.tomar("w", ["publicar"])
    hilo = threading.Thread(target=trabajos.correr, args=(t,))
    hilo.start()
    time.sleep(1.0)
    with sesion() as s:                           # alguien la borra de la base mientras espera
        s.delete(s.get(Pieza, pid))
    hilo.join(30)
    assert not any(u.endswith("/media_publish") for _, u, _, _ in meta.llamadas)


def test_nada_se_publica_sin_aprobar(con_datos, meta):
    """Ni jueces, ni repartir, ni encargo, ni sincronizar publican: solo 'programar'."""
    from hookline import trabajos
    from hookline.servicios import piezas
    for op in ("jueces",):
        con_datos.post("/api/pieza", json={"op": op, "id": con_datos.get("/api/piezas").json()["piezas"][0]["id"]})
    con_datos.post("/api/repartir", json={"dia": "2099-01-02"})
    con_datos.post("/api/encargo", json={"dia": "2099-01-02", "reels": 3, "historias": 3})
    con_datos.post("/api/cuenta", json={"op": "sincronizar", "id": "modelo1"})
    piezas.revisar_cola()
    for _ in range(20):
        t = trabajos.tomar("w", [x for x in trabajos.MANEJADORES if x != "render"])
        if not t:
            break
        trabajos.correr(t)
    assert not any(u.endswith("/media_publish") for _, u, _, _ in meta.llamadas)


# --- 5) Claude: respuestas basura y caídas -----------------------------------------------------------------------------------
@pytest.mark.parametrize("contenido", ["", "no es json", "[]", "null", '{"veredicto": 5}', "{" * 5000,
                                       json.dumps({"jueces": "x" * 100000}), '{"jueces": [], "veredicto": "aprobar"}',
                                       "\x00\xff\xfe", json.dumps({"jueces": [{"nombre": "a", "nota": "diez"}]})])
def test_claude_code_responde_basura(con_datos, contenido):
    from hookline import trabajos
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.modelos import Pieza, TareaIA
    pid = con_datos.get("/api/piezas").json()["piezas"][0]["id"]
    con_datos.post("/api/pieza", json={"op": "jueces", "id": pid})
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    t = tareas.pendientes_claude_code()[0]
    (tareas.dir_tarea(t.id) / "resultado.json").write_bytes(contenido.encode("utf-8", "surrogatepass")
                                                             if isinstance(contenido, str) else contenido)
    tareas.recoger_claude_code()
    with sesion() as s:
        assert s.get(TareaIA, t.id).estado == "error"
        x = s.get(Pieza, pid)
        assert x.estado == "nueva" and x.error            # vuelve a la bandeja con el motivo, no queda trabada
    assert con_datos.get("/api/estado").status_code == 200


def test_api_de_claude_se_cae_y_se_recupera(con_datos, monkeypatch):
    from types import SimpleNamespace
    from hookline import comun, trabajos
    from hookline.db import sesion
    from hookline.ia import motor_api
    from hookline.modelos import Pieza, TareaIA, Trabajo
    comun.guardar_ajuste("motor_ia", "api")
    llamadas = []

    def parse(**kw):
        llamadas.append(1)
        if len(llamadas) < 3:
            raise ConnectionError("se cortó")
        from tests.test_ia_api import VEREDICTO
        return SimpleNamespace(stop_reason="end_turn", model=kw["model"], stop_details=None,
                               parsed_output=kw["output_format"].model_validate(VEREDICTO),
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))
    falso = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)),
                            messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    pid = con_datos.get("/api/piezas").json()["piezas"][0]["id"]
    con_datos.post("/api/pieza", json={"op": "jueces", "id": pid})
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    for _ in range(5):
        with sesion() as s:
            for x in s.query(Trabajo).filter_by(tipo="ia", estado="pendiente"):
                x.ejecutar_en = x.creado            # no esperar el backoff
        t = trabajos.tomar("w", ["ia"])
        if not t:
            break
        trabajos.correr(t)
    with sesion() as s:
        assert s.query(TareaIA).one().estado == "hecha"
        assert s.get(Pieza, pid).estado == "juzgada"
    assert len(llamadas) == 3


def test_api_de_claude_caida_para_siempre_no_traba(con_datos, monkeypatch):
    from types import SimpleNamespace
    from hookline import comun, trabajos
    from hookline.db import sesion
    from hookline.ia import motor_api
    from hookline.modelos import Pieza, TareaIA, Trabajo
    comun.guardar_ajuste("motor_ia", "api")

    def parse(**kw):
        raise TimeoutError("nada")
    falso = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)), messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    pid = con_datos.get("/api/piezas").json()["piezas"][0]["id"]
    con_datos.post("/api/pieza", json={"op": "jueces", "id": pid})
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    for _ in range(6):
        with sesion() as s:
            for x in s.query(Trabajo).filter_by(tipo="ia", estado="pendiente"):
                x.ejecutar_en = x.creado
        t = trabajos.tomar("w", ["ia"])
        if not t:
            break
        trabajos.correr(t)
    with sesion() as s:
        assert s.query(TareaIA).one().estado == "error"
        assert s.get(Pieza, pid).estado == "nueva"


# --- 6) volumen y cosas rotas en disco ------------------------------------------------------------------------------------------
def test_mil_archivos_en_la_bandeja(con_datos, foto):
    from hookline import almacen
    from hookline.servicios import piezas
    d = almacen.dir_de("subir", "modelo1")
    for i in range(1000):
        shutil.copy(foto, d / f"f{i}.jpg")
    hs = [threading.Thread(target=piezas.escanear) for _ in range(6)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    ps = piezas.listar("modelo1")
    assert len(ps) == 1001 and len({p["archivos"][0] for p in ps}) == 1001
    t0 = time.time()
    assert con_datos.get("/api/estado").status_code == 200
    assert time.time() - t0 < 15


def test_archivo_borrado_a_mano_antes_de_publicar(con_datos, meta):
    from hookline import almacen, trabajos
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    pid = _programada(con_datos)
    with sesion() as s:
        rel = s.get(Pieza, pid).archivos[0]
    almacen.ruta(rel).unlink()
    piezas.revisar_cola()
    t = trabajos.tomar("w", ["publicar"])
    if t:
        trabajos.correr(t)
    with sesion() as s:
        x = s.get(Pieza, pid)
        assert x is None or x.estado in ("error", "programada")
    assert not any(u.endswith("/media_publish") for _, u, _, _ in meta.llamadas)


@pytest.mark.parametrize("contenido", [b"", b"no soy una imagen", b"\xff\xd8\xff" + b"\x00" * 50, b"GIF89a" + b"x" * 10])
def test_imagen_corrupta_en_jueces_y_mini(con_datos, contenido):
    from hookline import almacen, trabajos
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    (almacen.dir_de("subir", "modelo1") / "rota.jpg").write_bytes(contenido)
    piezas.escanear()
    p = next(p for p in piezas.listar("modelo1") if p["archivos"][0].endswith("rota.jpg"))
    _sin_500_ni_fugas(con_datos.get(p["vistas_previas"][0]))
    con_datos.post("/api/pieza", json={"op": "jueces", "id": p["id"]})
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    with sesion() as s:
        x = s.get(Pieza, p["id"])
        assert x.estado == "nueva" and "Jueces" in x.error


def test_migrar_lite_con_archivos_rotos(tmp_path):
    from hookline.migrar_lite import migrar
    lite = tmp_path / "lite"
    (lite / "datos").mkdir(parents=True)
    (lite / "cuentas" / "rota").mkdir(parents=True)
    (lite / "cuentas" / "rota" / "cuenta.json").write_text("{ esto no es json")
    (lite / "cuentas" / "buena").mkdir(parents=True)
    (lite / "cuentas" / "buena" / "cuenta.json").write_text(json.dumps({"usuario": "buena"}))
    (lite / "cuentas" / "buena" / "token.json").write_text("[]")
    (lite / "cuentas" / "buena" / "medios.json").write_text(json.dumps({"medios": [{"sin_id": 1}, {"id": "9"}]}))
    (lite / "datos" / "piezas.json").write_text(json.dumps([{"id": "p1"}, {"cuenta": "buena"}, "basura", None]))
    (lite / "datos" / "chat.json").write_text(json.dumps([{"texto": None}, 5]))
    (lite / "datos" / "config.json").write_text('"no es un objeto"')
    r = migrar(str(lite))
    assert r["cuentas"] == 1


# --- 7) fábrica: entradas absurdas ----------------------------------------------------------------------------------------------
@settings(max_examples=120, deadline=None, suppress_health_check=list(HealthCheck))
@given(campos=st.dictionaries(st.sampled_from(["texto", "texto_y", "texto_x", "texto_tam", "corte", "audio", "chat",
                                               "pegar", "noti", "textos", "recorte_remate", "audio_desde", "fuente",
                                               "estilo", "color", "ubicacion_auto"]), JSON, max_size=8))
def test_limpiar_campos_nunca_explota_mal(campos):
    from hookline.servicios import fabrica
    try:
        out = fabrica._limpiar_campos(campos, 10.0)
    except ValueError:
        return
    json.dumps(out)
    for k in ("texto_y", "texto_x", "texto_tam"):
        if k in out:
            assert 0 <= out[k] <= 1
    if "corte" in out:
        assert 0.3 <= out["corte"] <= 10.0


@settings(max_examples=60, deadline=None, suppress_health_check=list(HealthCheck))
@given(texto=st.text(alphabet=string.printable + "áéíóúñ🙂'\"\\:%{}[];,=", max_size=300))
def test_texto_del_reel_no_rompe_el_filtro_ffmpeg(texto):
    """El texto va por archivo (textfile=) y no dentro del filtro: comillas, dos puntos o barras no lo rompen."""
    from hookline.servicios import fabrica
    c = fabrica.normalizar({"id": "abcdef12", "texto": texto, "texto_tam": 0.04, "texto_y": 0.2, "texto_x": 0.5})
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        filtros = fabrica._texto_filtros(c, Path(d), 720, 1280)
        for f in filtros:
            assert texto[:1] not in ("",) or True
            assert "textfile=" in f and "'" not in f.split("textfile=")[1].split(":")[0]


@settings(max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
@given(cuenta=st.dictionaries(st.sampled_from(["usuario", "nombre", "nicho", "tono", "main", "activa_desde", "creada"]), JSON),
       token=st.dictionaries(st.sampled_from(["access_token", "permisos", "obtenido", "refrescado", "vence", "vencido", "ig_id"]), JSON),
       medio=st.dictionaries(st.sampled_from(["id", "caption", "publicado", "vistas", "likes", "reel"]), JSON),
       pieza=st.dictionaries(st.sampled_from(["id", "cuenta", "tipo", "archivos", "estado", "cuando", "caption", "jueces",
                                              "plan", "creado"]), JSON),
       comentario=st.dictionaries(st.sampled_from(["texto", "fecha", "likes", "estado", "usuario"]), JSON))
def test_fuzz_migrar_lite_no_se_cae(tmp_path_factory, cuenta, token, medio, pieza, comentario):
    from hookline.migrar_lite import migrar
    lite = tmp_path_factory.mktemp("lite")
    d = lite / "cuentas" / "modelo1"
    d.mkdir(parents=True)
    (lite / "datos").mkdir()
    (d / "cuenta.json").write_text(json.dumps(cuenta))
    (d / "token.json").write_text(json.dumps(token))
    (d / "medios.json").write_text(json.dumps({"medios": [medio]}))
    (d / "comentarios.json").write_text(json.dumps({"items": {"1": comentario}}))
    (lite / "datos" / "piezas.json").write_text(json.dumps([{**pieza, "cuenta": pieza.get("cuenta", "modelo1")}]))
    r = migrar(str(lite), copiar_archivos=False)
    assert r["cuentas"] in (0, 1)
