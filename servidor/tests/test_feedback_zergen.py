"""Lo que reportaron al probar en producción (2026-11-18):
1. La sesión de Instagram pegada como «nombre=valor; ...» no se aceptaba, y la de cookies.txt daba «Algo falló»
   (la API intentaba abrir un Chrome propio, que no es el del buscador).
2. Las miniaturas del banco de memes quedaban en «cargando» para siempre.
3. Copiar video: «La API de Claude no respondió después de 3 intentos: 400 ... Schema ...» (el esquema de la lectura
   era demasiado complejo para structured outputs, y un 400 se reintentaba como si fuera un corte de red)."""
import json
import os
import stat
import threading
import time
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# valores inventados con la misma forma que los reales (nunca pegar cookies reales en un test)
HEADER = ("datr=AAAAAAAAAAAAAAAAAAAAAAAA; ig_did=00000000-0000-0000-0000-000000000000; mid=ZZZZZZZZZZZZZZZZZZZZZZZZ; "
          "ps_l=1; ps_n=1; ig_nrcb=1; ds_user_id=12345678901; csrftoken=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX; dpr=1.25; "
          "sessionid=12345678901%3AFAKEFAKEFAKE%3A12%3AAYfakefakefake; wd=1718x1304; "
          "rur=\"CCO\\05412345678901\\0541795000000:01fakefake\"")
NETSCAPE = "\n".join([
    "# Netscape HTTP Cookie File",
    ".instagram.com\tTRUE\t/\tTRUE\t1824350000\tcsrftoken\tXXXX",
    "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1795000000\tsessionid\t123%3Afake%3A12",
    ".instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t123",
    ".google.com\tTRUE\t/\tTRUE\t0\tNID\tno-va"])
JSON_EDITOR = json.dumps([
    {"domain": ".tiktok.com", "name": "sessionid", "value": "tt-fake", "path": "/", "secure": True, "httpOnly": True,
     "expirationDate": 1795000000.5},
    {"domain": ".tiktok.com", "name": "tt_csrf_token", "value": "x", "path": "/"},
    {"domain": ".facebook.com", "name": "c_user", "value": "no-va"}])


# --- 1. sesión de la cuenta "para mirar" ----------------------------------------------------------------------------
def test_acepta_la_linea_nombre_valor_como_la_que_pegaron():
    from hookline.servicios.buscador import leer_cookies
    cs = leer_cookies(HEADER)
    nombres = {c["name"]: c for c in cs}
    assert {"sessionid", "csrftoken", "ds_user_id", "rur"} <= set(nombres)
    assert all(c["domain"] == ".instagram.com" and c["secure"] for c in cs)
    assert nombres["sessionid"]["value"] == "12345678901%3AFAKEFAKEFAKE%3A12%3AAYfakefakefake"
    assert leer_cookies("Cookie: " + HEADER)[0]["name"] == "datr"          # con el «Cookie:» delante
    assert len(leer_cookies(HEADER.replace("; ", ";\n"))) == len(cs)       # partida en renglones al copiar


def test_acepta_cookies_txt_con_tabs_o_con_espacios_y_json():
    from hookline.servicios.buscador import leer_cookies
    cs = leer_cookies(NETSCAPE)
    assert {c["name"] for c in cs} == {"csrftoken", "sessionid", "ds_user_id"}        # google afuera
    s = next(c for c in cs if c["name"] == "sessionid")
    assert s["httpOnly"] and s["expires"] == 1795000000 and s["domain"] == ".instagram.com"
    con_espacios = NETSCAPE.replace("\t", "    ")                           # el textarea a veces cambia tabs
    assert {c["name"] for c in leer_cookies(con_espacios)} == {"csrftoken", "sessionid", "ds_user_id"}
    tt = leer_cookies(JSON_EDITOR)
    assert {c["name"] for c in tt} == {"sessionid", "tt_csrf_token"} and tt[0]["expires"] == 1795000000
    assert leer_cookies("sessionid=a; tt_chain_token=b; ttwid=c")[0]["domain"] == ".tiktok.com"


@pytest.mark.parametrize("texto,motivo", [
    ("", "No encontré"), ("hola que tal", "No encontré"), ("[1, 2", "cortado"), ("{}", "No encontré"),
    ("csrftoken=abc; ds_user_id=1", "falta «sessionid»"),
    (".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tx", "falta «sessionid»"),
    (json.dumps([{"domain": ".tiktok.com", "name": "ttwid", "value": "x"}]), "Tiktok"),
    ("<script>alert(1)</script>=1; sessionid=x", None),
])
def test_textos_malos_explican_que_pasa(texto, motivo):
    from hookline.servicios.buscador import leer_cookies
    if motivo is None:
        assert [c["name"] for c in leer_cookies(texto)] == ["sessionid"]   # el nombre raro se ignora
        return
    with pytest.raises(ValueError, match=motivo):
        leer_cookies(texto)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(max_size=2000))
def test_fuzz_leer_cookies_solo_devuelve_cookies_o_explica(texto):
    from hookline.servicios.buscador import leer_cookies
    try:
        cs = leer_cookies(texto)
    except ValueError:
        return
    assert cs and all(("instagram" in c["domain"] or "tiktok" in c["domain"]) for c in cs)
    assert any(c["name"] == "sessionid" for c in cs)


def test_guardar_la_sesion_no_necesita_navegador_y_junta_las_dos_redes(monkeypatch):
    from hookline import almacen
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setattr(buscador, "_playwright", lambda: (_ for _ in ()).throw(AssertionError("no hace falta")))
    assert buscador.importar_cookies(HEADER) >= 10
    assert buscador.estado_sesion() == {"instagram": True, "tiktok": False}
    assert buscador.importar_cookies(JSON_EDITOR) == 2
    assert buscador.estado_sesion() == {"instagram": True, "tiktok": True}          # instagram quedó
    f = almacen.raiz() / "navegador" / "cookies.txt"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    texto = f.read_text()
    assert texto.startswith("# Netscape HTTP Cookie File") and "#HttpOnly_.tiktok.com\tTRUE\t/" in texto
    buscador.importar_cookies(NETSCAPE)                                         # reemplaza solo la de instagram
    cs = buscador.cookies_guardadas()
    assert {c["name"] for c in cs if "instagram" in c["domain"]} == {"csrftoken", "sessionid", "ds_user_id"}
    assert {c["name"] for c in cs if "tiktok" in c["domain"]} == {"sessionid", "tt_csrf_token"}
    # el archivo lo entiende yt-dlp (el mismo lector de cookies.txt que usa para bajar)
    from http.cookiejar import MozillaCookieJar
    jar = MozillaCookieJar(str(f))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert {c.name for c in jar} >= {"sessionid", "csrftoken", "tt_csrf_token"}


def test_el_buscador_carga_la_sesion_guardada_al_abrir(monkeypatch):
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    buscador.importar_cookies(HEADER)
    ctx = SimpleNamespace(puestas=[])
    ctx.add_cookies = lambda cs: ctx.puestas.extend(cs)
    buscador._cargar_sesion(ctx)
    assert any(c["name"] == "sessionid" for c in ctx.puestas)


def test_api_cookies_contesta_claro(cliente_api, monkeypatch):
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    r = cliente_api.post("/api/copiar", json={"op": "importar_cookies", "texto": HEADER})
    assert r.status_code == 200 and r.json()["redes"] == ["Instagram"] and r.json()["n"] >= 10
    r = cliente_api.post("/api/copiar", json={"op": "importar_cookies", "texto": "csrftoken=1"})
    assert r.status_code == 400 and "sessionid" in r.json()["error"]
    r = cliente_api.post("/api/copiar", json={"op": "importar_cookies", "texto": "x" * 500_000})
    assert r.status_code == 400


def test_un_error_inesperado_contesta_json_y_no_algo_fallo(cliente_api, monkeypatch):
    from hookline.servicios import buscador

    def explota(_t):
        raise KeyError("raro")
    monkeypatch.setattr(buscador, "importar_cookies", explota)
    from fastapi.testclient import TestClient
    from hookline import api
    c = TestClient(api.app, raise_server_exceptions=False)
    c.cookies.update(cliente_api.cookies)
    c.headers.update(cliente_api.headers)
    r = c.post("/api/copiar", json={"op": "importar_cookies", "texto": HEADER})
    assert r.status_code == 500 and "KeyError" in r.json()["error"] and "raro" not in r.json()["error"]


# --- 2. miniaturas del banco -----------------------------------------------------------------------------------------
class _YDL:
    abiertos, opciones = [0], []

    def __init__(self, o):
        _YDL.opciones.append(o)

    def __enter__(self):
        _YDL.abiertos[0] += 1
        return self

    def __exit__(self, *a):
        _YDL.abiertos[0] -= 1

    def extract_info(self, link, download=False):
        time.sleep(0.05)
        if "malo" in link:
            raise RuntimeError("login required")
        return {"id": link[-3:], "duration": 7.5, "title": "t", "uploader": "@alguien"}


def test_fichas_usan_la_sesion_tienen_tiempo_maximo_y_no_se_pisan(monkeypatch):
    import sys
    from hookline.servicios import buscador, fabrica
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    buscador.importar_cookies(HEADER)
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL))
    _YDL.opciones.clear()
    fabrica.guardar_links("https://www.instagram.com/reel/AAA111/\nhttps://www.instagram.com/reel/malo222/\n"
                          "https://www.tiktok.com/@a/video/333")
    hs = [threading.Thread(target=fabrica._fichas, args=({},)) for _ in range(4)]   # 4 trabajos a la vez
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert len(_YDL.opciones) == 3                         # cada link una sola vez, no 4
    assert all(o["cookiefile"].endswith("navegador/cookies.txt") and o["socket_timeout"] for o in _YDL.opciones)
    fs = {x["link"]: x for x in fabrica.links()}
    assert fs["https://www.instagram.com/reel/AAA111/"]["ficha"] == "ok"
    assert fs["https://www.instagram.com/reel/malo222/"]["ficha"] == "error"   # sale el ícono, no «cargando»


# --- 3. Copiar video: la lectura del ejemplo por la API -----------------------------------------------------------------
def _contar(schema):
    defs = schema.get("$defs", {})
    opc = union = 0

    def visitar(n):
        nonlocal opc, union
        if not isinstance(n, dict):
            return
        if "$ref" in n:
            return visitar(defs[n["$ref"].split("/")[-1]])
        if n.get("type") == "object":
            req = set(n.get("required") or [])
            for k, v in (n.get("properties") or {}).items():
                opc += k not in req
                union += "anyOf" in v or isinstance(v.get("type"), list)
                visitar(v)
        for k in ("items", "anyOf", "allOf"):
            for x in (n[k] if isinstance(n.get(k), list) else [n[k]] if k in n else []):
                visitar(x)
    visitar(schema)
    return opc, union


def test_ningun_esquema_que_va_a_la_api_es_demasiado_complejo():
    """Límites de structured outputs: 24 opcionales y 16 con dos tipos en todo el pedido; más un límite interno
    («Schema is too complex for compilation»). Nos quedamos muy por debajo para no volver a chocarlo."""
    from anthropic.lib._parse._transform import transform_schema
    from hookline.ia.esquemas import ESQUEMAS, para_api
    for tipo in ESQUEMAS:
        opc, union = _contar(transform_schema(para_api(tipo)[0]))
        assert opc <= 6 and union <= 6, (tipo, opc, union)
    assert _contar(transform_schema(para_api("lectura")[0])) == (0, 0)


class _Cliente:
    def __init__(self, salida=None, error=None):
        self.llamadas, self.salida, self.error = [], salida, error

        def parse(**kw):
            self.llamadas.append(kw)
            if self.error:
                raise self.error
            return SimpleNamespace(stop_reason="end_turn", stop_details=None, model=kw["model"],
                                   parsed_output=kw["output_format"].model_validate(self.salida),
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))
        self.messages = SimpleNamespace(parse=parse)


LECTURA_API = {"descartar": "", "corte": 2.4, "texto": "Cuando me dice «ya llego»", "texto_y": 0.18,
               "texto_tam": 0.042, "modelo": 3, "notas": "remate de perro", "marca_agua": False,
               "chat": {"hay": False, "mensajes": [], "x0": 0, "x1": 0, "y0": 0},
               "pegar": [{"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.4, "cuadro": 1.5, "a_y0": -1}],
               "noti": {"hay": False, "texto": "", "titulo": "", "x0": -1, "x1": -1, "y0": -1},
               "textos": [{"texto": "jaja", "x": 0.5, "y": 0.8, "tam": 0}],
               "recorte_remate": {"x0": 0, "y0": 0, "x1": 0, "y1": 0}, "fuente": ""}


def _tarea_lectura():
    return SimpleNamespace(id="lectura-x", tipo="lectura",
                           entrada={"instrucciones": "leé", "imagenes": [], "web": False, "rapido": False})


def test_la_lectura_va_a_la_api_simple_y_vuelve_en_la_forma_de_siempre(monkeypatch):
    from hookline.ia import motor_api
    from hookline.ia.esquemas import ESQUEMAS, LecturaAPI
    falso = _Cliente(LECTURA_API)
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    r, uso = motor_api.resolver(_tarea_lectura())
    assert falso.llamadas[0]["output_format"] is LecturaAPI
    assert r["corte"] == 2.4 and r["modelo"] == 3 and r["texto_y"] == 0.18
    assert "chat" not in r and "noti" not in r and "recorte_remate" not in r and r["fuente"] is None
    assert r["pegar"][0]["a_y0"] is None and r["textos"][0]["tam"] is None
    v = ESQUEMAS["lectura"].model_validate(r).model_dump()
    assert v["descartar"] is None and v["texto"] == "Cuando me dice «ya llego»"
    falso.salida = {**LECTURA_API, "descartar": "menor"}
    assert motor_api.resolver(_tarea_lectura())[0] == {"descartar": "menor"}
    falso.salida = {**LECTURA_API, "modelo": 0, "corte": -1, "texto_y": -1, "texto_tam": 0, "fuente": "meme",
                    "chat": {"hay": True, "mensajes": [{"de": "yo", "texto": "hola", "hora": ""}], "x0": 0.1, "x1": 0.9,
                             "y0": 0.3},
                    "noti": {"hay": True, "texto": "te siguió", "titulo": "", "x0": -1, "x1": 0.9, "y0": 0.1},
                    "recorte_remate": {"x0": 0, "y0": 0.1, "x1": 1, "y1": 0.9}}
    r = motor_api.resolver(_tarea_lectura())[0]
    assert r["modelo"] is None and r["corte"] is None and r["texto_y"] is None and r["texto_tam"] is None
    assert r["chat"]["mensajes"][0]["texto"] == "hola" and r["noti"]["x0"] is None and r["noti"]["x1"] == 0.9
    assert r["noti"]["titulo"] == "Instagram" and r["recorte_remate"]["y1"] == 0.9 and r["fuente"] == "meme"
    ESQUEMAS["lectura"].model_validate(r)


_num = st.floats(min_value=-5, max_value=5, allow_nan=False)


@settings(max_examples=200, deadline=None)
@given(st.fixed_dictionaries({
    "descartar": st.sampled_from(["", "menor"]), "corte": _num, "texto": st.text(max_size=40), "texto_y": _num,
    "texto_tam": _num, "modelo": st.integers(-3, 40), "notas": st.text(max_size=20), "marca_agua": st.booleans(),
    "chat": st.fixed_dictionaries({"hay": st.booleans(), "mensajes": st.lists(st.fixed_dictionaries({
        "de": st.sampled_from(["yo", "otro", "aviso"]), "texto": st.text(max_size=10), "hora": st.text(max_size=5)}),
        max_size=3), "x0": _num, "x1": _num, "y0": _num}),
    "pegar": st.lists(st.fixed_dictionaries({k: _num for k in ("x0", "y0", "x1", "y1", "cuadro", "a_y0")}), max_size=3),
    "noti": st.fixed_dictionaries({"hay": st.booleans(), "texto": st.text(max_size=10), "titulo": st.text(max_size=10),
                                   "x0": _num, "x1": _num, "y0": _num}),
    "textos": st.lists(st.fixed_dictionaries({"texto": st.text(max_size=10), "x": _num, "y": _num, "tam": _num}),
                       max_size=3),
    "recorte_remate": st.fixed_dictionaries({k: _num for k in ("x0", "y0", "x1", "y1")}),
    "fuente": st.text(max_size=10)}))
def test_fuzz_cualquier_lectura_de_la_api_se_convierte_en_una_valida(d):
    from hookline.ia.esquemas import ESQUEMAS, LecturaAPI, para_api
    _, desde = para_api("lectura")
    v = ESQUEMAS["lectura"].model_validate(desde(LecturaAPI.model_validate(d).model_dump())).model_dump()
    for k in ("texto_y",):
        assert v[k] is None or 0 <= v[k] <= 1
    assert v["modelo"] is None or v["modelo"] > 0


def test_un_400_no_se_reintenta_y_muestra_el_motivo_real(monkeypatch):
    import anthropic
    import httpx
    from hookline import comun, trabajos
    from hookline.db import sesion
    from hookline.ia import motor_api, tareas
    from hookline.modelos import TareaIA
    try:
        import httpx2 as hx
    except ImportError:
        hx = httpx
    resp = hx.Response(400, request=hx.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.BadRequestError("Error code: 400", response=resp, body={
        "type": "error", "error": {"type": "invalid_request_error", "message": "Schema is too complex for compilation."}})
    falso = _Cliente(error=err)
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    comun.guardar_ajuste("motor_ia", "api")
    tid = tareas.crear("chat", {"texto": "hola", "contexto": {}, "historial": []})
    trabajos.correr(trabajos.tomar("w", ["ia"]))
    with sesion() as s:
        t = s.get(TareaIA, tid)
        assert t.estado == "error" and t.intentos == 1 and len(falso.llamadas) == 1
        assert "rechazó el pedido" in t.error and "Schema is too complex" in t.error
        assert "no respondió después de 3 intentos" not in t.error
    resp = hx.Response(401, request=hx.Request("POST", "https://api.anthropic.com/v1/messages"))
    falso.error = anthropic.AuthenticationError("401", response=resp, body={"error": {"message": "invalid x-api-key"}})
    tid = tareas.crear("chat", {"texto": "hola", "contexto": {}, "historial": []})
    trabajos.correr(trabajos.tomar("w", ["ia"]))
    with sesion() as s:
        assert "clave" in s.get(TareaIA, tid).error and s.get(TareaIA, tid).intentos == 1


@pytest.mark.skipif(not __import__("hookline.medios", fromlist=["x"]).ffmpeg(), reason="hace falta ffmpeg")
def test_copiar_video_completo_por_la_api(tmp_path, foto, monkeypatch):
    """El camino que falló en producción, con el motor API: ejemplo → lectura → video armado → juez."""
    import shutil
    from hookline import almacen, comun, medios
    from hookline.db import sesion
    from hookline.ia import motor_api
    from hookline.ia.esquemas import Juicio, LecturaAPI
    from hookline.modelos import Cuenta, TareaIA
    from hookline.servicios import fabrica
    from test_fabrica import _ejemplo, _vaciar_cola
    with sesion() as s:
        s.add(Cuenta(id="modelo1", activa_desde="2026-01-01"))
    shutil.copy(foto, almacen.dir_de("material", "modelo1", "modelo") / "selfie.jpg")
    comun.guardar_ajuste("motor_ia", "api")
    pedidos = []

    def parse(**kw):
        pedidos.append(kw["output_format"])
        esquema = kw["output_format"]
        salida = {**LECTURA_API, "corte": 2.0, "modelo": 1} if esquema is LecturaAPI else \
            {"nota_texto": 4, "nota_tecnica": 4, "motivo": "bien", "arreglo": None, "foto": None, "texto_y": None}
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, model=kw["model"],
                               parsed_output=esquema.model_validate(salida),
                               usage=SimpleNamespace(input_tokens=900, output_tokens=120, cache_read_input_tokens=0))
    falso = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)),
                            messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    cid = fabrica.desde_archivo("ejemplo.mp4", _ejemplo(tmp_path).read_bytes(), "modelo1")
    for _ in range(4):
        _vaciar_cola()
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert LecturaAPI in pedidos and Juicio in pedidos, pedidos
    assert not c.get("error"), c.get("error")
    assert c["resultado"] and 4.5 <= medios.duracion(fabrica.resultado(cid)) <= 5.5
    assert c["juez"]["estado"] == "aprobado"
    with sesion() as s:
        assert {t.estado for t in s.query(TareaIA)} == {"hecha"}
