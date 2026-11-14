"""Sección Fuentes: ver, subir (.ttf/.otf/.zip), ocultar/borrar, predeterminada y que Claude elija."""
import io
import zipfile
from pathlib import Path

import pytest

TTF = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
TTF2 = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
pytestmark = pytest.mark.skipif(not TTF.is_file() or not TTF2.is_file(), reason="faltan fuentes DejaVu en el sistema")


def _subir(c, nombre, datos):
    return c.post(f"/api/fuentes_subir?nombre={nombre}", content=datos)


def _zip(archivos: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in archivos.items():
            z.writestr(n, d)
    return b.getvalue()


def _estado(c):
    return c.get("/api/fuentes").json()


def test_lista_las_del_sistema_y_la_clasica_es_predeterminada(cliente_api):
    d = _estado(cliente_api)
    claves = [f["clave"] for f in d["fuentes"]]
    assert "clasica" in claves and d["predeterminada"] == "clasica" and d["claude"] is False
    assert all(f["origen"] == "sistema" for f in d["fuentes"])
    r = cliente_api.get("/api/fuente?k=clasica")
    assert r.status_code == 200 and r.headers["content-type"].startswith("font/") and len(r.content) > 1000


def test_subir_ttf_suelto_y_en_zip_sin_repetir(cliente_api):
    r = _subir(cliente_api, "DejaVuSerif-Bold.ttf", TTF.read_bytes())
    assert r.status_code == 200 and len(r.json()["nuevas"]) == 1
    nueva = r.json()["nuevas"][0]
    assert nueva["clave"].startswith("f_") and "DejaVu Serif" in nueva["nombre"]
    z = _zip({"fonts/DejaVuSerif-Bold.ttf": TTF.read_bytes(), "fonts/Mono.ttf": TTF2.read_bytes(), "LEEME.txt": b"hola",
              "__MACOSX/._Mono.ttf": b"basura"})
    r = _subir(cliente_api, "pack.zip", z)
    assert r.status_code == 200 and len(r.json()["nuevas"]) == 1 and r.json()["repetidas"] == 1
    d = _estado(cliente_api)
    assert len([f for f in d["fuentes"] if f["origen"] == "subida"]) == 2
    r = cliente_api.get(f"/api/fuente?k={nueva['clave']}")
    assert r.status_code == 200 and r.content == TTF.read_bytes()


@pytest.mark.parametrize("nombre,datos", [
    ("x.ttf", b""),
    ("x.ttf", b"\x00\x01\x00\x00" + b"basura" * 100),
    ("x.otf", b"hola"),
    ("x.woff2", b"wOF2" + b"0" * 100),
    ("x.exe", b"MZ" + b"0" * 100),
    ("x.ttc", b"ttcf" + b"0" * 100),
    ("x.zip", b"PK\x03\x04roto"),
    ("../../hookline.db.ttf", b"\x00\x01\x00\x00"),
])
def test_archivos_que_no_son_fuentes_se_rechazan(cliente_api, nombre, datos):
    r = _subir(cliente_api, nombre, datos)
    assert r.status_code == 400
    assert not [f for f in _estado(cliente_api)["fuentes"] if f["origen"] == "subida"]


def test_zip_sin_fuentes_o_con_rutas_raras(cliente_api):
    assert _subir(cliente_api, "a.zip", _zip({"a.txt": b"x"})).status_code == 400
    r = _subir(cliente_api, "b.zip", _zip({"../../../../etc/evil.ttf": TTF.read_bytes()}))
    assert r.status_code == 200
    from hookline import almacen
    base = almacen.dir_de("fuentes").resolve()
    for p in almacen.raiz().rglob("*.ttf"):
        assert p.resolve().is_relative_to(base) and p.name.startswith("f_")


def test_fuente_gigante_se_rechaza(cliente_api, monkeypatch):
    from hookline import fuentes
    monkeypatch.setattr(fuentes, "MAX_ARCHIVO", 1000)
    assert _subir(cliente_api, "g.ttf", TTF.read_bytes()).status_code == 400


def test_no_se_puede_pedir_otro_archivo_por_la_ruta_de_fuentes(cliente_api):
    for k in ("../hookline.db", "f_0000000000", "/etc/passwd", "texto", ""):
        assert cliente_api.get("/api/fuente", params={"k": k}).status_code == 404


def test_sacar_agregar_borrar_y_predeterminada(cliente_api):
    k = _subir(cliente_api, "s.ttf", TTF.read_bytes()).json()["nuevas"][0]["clave"]
    assert cliente_api.post("/api/fuentes", json={"op": "predeterminada", "clave": k}).json()["predeterminada"] == k
    d = cliente_api.post("/api/fuentes", json={"op": "borrar", "clave": "clasica"}).json()     # de siempre: al catálogo
    assert "clasica" not in [f["clave"] for f in d["fuentes"]]
    cat = cliente_api.get("/api/fuentes_catalogo", params={"q": "clásica"}).json()
    assert [x["clave"] for x in cat["items"]] == ["clasica"]
    assert cliente_api.get("/api/fuente?k=clasica").status_code == 200                       # sigue existiendo
    assert cliente_api.post("/api/fuentes", json={"op": "predeterminada", "clave": "clasica"}).status_code == 400
    d = cliente_api.post("/api/fuentes", json={"op": "borrar", "clave": k}).json()            # subida: se borra
    assert k not in [f["clave"] for f in d["fuentes"]] and d["predeterminada"] != k
    assert cliente_api.get(f"/api/fuente?k={k}").status_code == 404
    from hookline import almacen
    assert not list(almacen.dir_de("fuentes").glob(f"{k}.*"))
    d = cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": "clasica"}).json()
    assert d["predeterminada"] == "clasica"


def test_tiene_que_quedar_al_menos_una(cliente_api):
    for f in _estado(cliente_api)["fuentes"]:
        r = cliente_api.post("/api/fuentes", json={"op": "borrar", "clave": f["clave"]})
    assert r.status_code == 400 and "al menos una" in r.json()["error"]
    assert len(_estado(cliente_api)["fuentes"]) == 1


def test_renombrar_solo_las_subidas(cliente_api):
    k = _subir(cliente_api, "s.ttf", TTF.read_bytes()).json()["nuevas"][0]["clave"]
    d = cliente_api.post("/api/fuentes", json={"op": "renombrar", "clave": k, "nombre": "  La <b>linda</b>\x07 "}).json()
    assert next(f for f in d["fuentes"] if f["clave"] == k)["nombre"] == "La blinda/b"
    assert cliente_api.post("/api/fuentes", json={"op": "renombrar", "clave": "clasica", "nombre": "x"}).status_code == 400


def test_permisos(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    cliente_api.post("/api/usuarios", json={"op": "crear", "email": "le@x.com", "clave": "clave-larga-123", "rol": "lector"})
    le = TestClient(api.app)
    le.headers["X-CSRF-Token"] = le.post("/api/login", json={"email": "le@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    assert le.get("/api/fuentes").status_code == 200
    assert le.post("/api/fuentes_subir?nombre=a.ttf", content=TTF.read_bytes()).status_code == 403
    assert le.post("/api/fuentes", json={"op": "borrar", "clave": "clasica"}).status_code == 403
    assert TestClient(api.app).get("/api/fuente?k=clasica").status_code == 401


# --- la fábrica ---------------------------------------------------------------------------------------------------------
def test_los_reels_nuevos_usan_la_predeterminada_y_se_puede_elegir_a_mano(cliente_api):
    from hookline.servicios import fabrica
    k = _subir(cliente_api, "s.ttf", TTF.read_bytes()).json()["nuevas"][0]["clave"]
    vieja = fabrica._nueva(link="https://x/1")
    assert fabrica._datos(vieja)["fuente"] == "clasica"
    cliente_api.post("/api/fuentes", json={"op": "predeterminada", "clave": k})
    nueva = fabrica._nueva(link="https://x/2")
    assert fabrica._datos(nueva)["fuente"] == k
    assert fabrica._datos(vieja)["fuente"] == "clasica"                   # lo ya hecho no cambia
    assert fabrica._limpiar_campos({"fuente": "clasica"}, 10) == {"fuente": "clasica"}
    assert fabrica._limpiar_campos({"fuente": "../x"}, 10) == {}
    assert fabrica._limpiar_campos({"fuente": ["clasica"]}, 10) == {}
    assert k in fabrica.opciones()["fuentes"]
    cliente_api.post("/api/fuentes", json={"op": "borrar", "clave": k})   # borrada: cae a la predeterminada
    assert fabrica._datos(nueva)["fuente"] == "clasica"
    assert fabrica.fuentes.ruta_obligatoria(k).is_file()


def test_claude_elige_la_letra_solo_si_esta_prendido(cliente_api, monkeypatch):
    from types import SimpleNamespace
    from hookline import fuentes
    from hookline.ia import esquemas, prompts
    from hookline.servicios import fabrica
    monkeypatch.setattr(fabrica, "_auto", lambda cid: None)
    cid = fabrica._nueva(link="https://x/1", estado="leyendo")
    fabrica._leido(SimpleNamespace(referencia=cid), {"texto": "hola", "fuente": "maquina"})
    assert fabrica._datos(cid)["fuente"] == "clasica"                     # apagado: se ignora
    cliente_api.post("/api/fuentes", json={"op": "claude", "valor": True})
    fabrica._leido(SimpleNamespace(referencia=cid), {"texto": "hola", "fuente": "maquina"})
    assert fabrica._datos(cid)["fuente"] == "maquina"
    fabrica._leido(SimpleNamespace(referencia=cid), {"texto": "hola", "fuente": "inventada"})
    assert fabrica._datos(cid)["fuente"] == "maquina"                     # una que no existe no se aplica
    img, vis = fuentes.muestrario()
    assert img.is_file() and len(vis) >= 3
    texto = prompts.lectura({"duracion": 5, "fuentes": [{"clave": x["clave"], "nombre": x["nombre"]} for x in vis]})
    assert "muestrario" in texto and "maquina" in texto
    assert "muestrario" not in prompts.lectura({"duracion": 5})
    assert "fuente" in esquemas.Lectura.model_json_schema()["properties"]


# --- a romper ----------------------------------------------------------------------------------------------------------
def test_subidas_y_borrados_a_la_vez_no_pierden_nada(cliente_api):
    """Varias personas subiendo y borrando a la vez: ninguna fuente se pierde ni queda un archivo huérfano."""
    import threading
    from hookline import almacen, fuentes
    datos = [TTF.read_bytes(), TTF2.read_bytes()]
    # 8 fuentes distintas (mismo archivo con un byte de relleno distinto al final: otra huella, sigue siendo válida)
    variantes = [datos[i % 2] + bytes([i]) * 4 for i in range(8)]
    errores = []

    def subir(b):
        try:
            fuentes.subir("x.ttf", b, "t")
        except Exception as ex:
            errores.append(ex)
    hs = [threading.Thread(target=subir, args=(b,)) for b in variantes]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert not errores
    subidas = [f for f in fuentes.lista() if f["origen"] == "subida"]
    assert len(subidas) == 8
    archivos = {p.stem for p in almacen.dir_de("fuentes").glob("f_*")}
    assert archivos == {f["clave"] for f in subidas}
    hs = [threading.Thread(target=fuentes.borrar, args=(f["clave"],)) for f in subidas[:5]]
    [h.start() for h in hs]
    [h.join() for h in hs]
    quedan = [f for f in fuentes.lista() if f["origen"] == "subida"]
    assert len(quedan) == 3
    assert {p.stem for p in almacen.dir_de("fuentes").glob("f_*")} == {f["clave"] for f in quedan}


def test_muestrario_con_muchas_fuentes_y_tras_cambios(cliente_api):
    from hookline import fuentes
    for i in range(12):
        fuentes.subir("x.ttf", TTF2.read_bytes() + bytes([i]) * 3, "t")
    img, vis = fuentes.muestrario()
    from PIL import Image
    with Image.open(img) as im:
        assert im.height > 92 * len(vis)
    fuentes.borrar(vis[-1]["clave"])
    img2, _ = fuentes.muestrario()
    assert img2 != img and not img.exists()                     # se regenera y el viejo no queda


def test_fuzz_operaciones_de_fuentes_nunca_500(cliente_api):
    from hypothesis import HealthCheck, given, settings, strategies as st
    raro = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=300), st.lists(st.text(max_size=5), max_size=3),
                     st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=3))

    @settings(max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
    @given(op=st.one_of(st.sampled_from(["borrar", "mostrar", "predeterminada", "renombrar", "claude", "x"]), raro),
           clave=st.one_of(st.sampled_from(["clasica", "maquina", "f_0000000000", "../x", ""]), raro),
           nombre=raro, valor=raro)
    def correr(op, clave, nombre, valor):
        r = cliente_api.post("/api/fuentes", json={"op": op, "clave": clave, "nombre": nombre, "valor": valor})
        assert r.status_code in (200, 400, 422), r.text
        assert cliente_api.get("/api/fuentes").json()["fuentes"]                                   # siempre queda una
    correr()


def test_fuzz_nombres_y_bytes_al_subir_nunca_500(cliente_api):
    from hypothesis import HealthCheck, given, settings, strategies as st

    @settings(max_examples=120, deadline=None, suppress_health_check=list(HealthCheck))
    @given(nombre=st.text(max_size=200), datos=st.binary(max_size=3000),
           cabeza=st.sampled_from([b"", b"\x00\x01\x00\x00", b"OTTO", b"true", b"PK\x03\x04", b"ttcf"]))
    def correr(nombre, datos, cabeza):
        r = cliente_api.post("/api/fuentes_subir", params={"nombre": nombre}, content=cabeza + datos)
        assert r.status_code in (200, 400), r.text
    correr()


# --- catálogo ("como en Word") ------------------------------------------------------------------------------------------
def test_catalogo_buscar_paginar_agregar_y_sacar(cliente_api):
    from hookline import fuentes
    cat = cliente_api.get("/api/fuentes_catalogo").json()
    assert cat["total"] >= 5 and len(cat["items"]) <= cat["por_pagina"]
    assert _estado(cliente_api)["catalogo_total"] == cat["total"]
    assert all(x["clave"].startswith("s_") for x in cat["items"])
    mias = {f["clave"] for f in _estado(cliente_api)["fuentes"]}
    assert not mias & {x["clave"] for x in cat["items"]}                     # lo de Mis fuentes no aparece
    serif = cliente_api.get("/api/fuentes_catalogo", params={"q": "serif bold"}).json()
    assert serif["items"] and all("serif" in (x["nombre"] + x["archivo"]).lower() for x in serif["items"])
    ultima = cliente_api.get("/api/fuentes_catalogo", params={"pagina": 9999}).json()
    assert ultima["items"] == [] and ultima["total"] == cat["total"]
    k = cat["items"][0]["clave"]
    assert cliente_api.get(f"/api/fuente?k={k}").status_code == 200
    d = cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": k}).json()
    f = next(x for x in d["fuentes"] if x["clave"] == k)
    assert f["origen"] == "catalogo" and d["catalogo_total"] == cat["total"] - 1
    assert cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": k}).status_code == 200   # dos veces: igual
    assert len([x for x in _estado(cliente_api)["fuentes"] if x["clave"] == k]) == 1
    assert cliente_api.post("/api/fuentes", json={"op": "predeterminada", "clave": k}).json()["predeterminada"] == k
    assert fuentes.ruta_obligatoria(k) == fuentes.ruta(k) and fuentes.ruta(k).is_file()
    d = cliente_api.post("/api/fuentes", json={"op": "borrar", "clave": k}).json()           # sacar: vuelve al catálogo
    assert k not in [x["clave"] for x in d["fuentes"]] and d["predeterminada"] == "clasica"
    assert fuentes.ruta(k).is_file()                                                       # el archivo no se toca


@pytest.mark.parametrize("q", ["", "x" * 500, "\x00", "../../", "%", "' OR 1=1", "日本語"])
def test_buscar_cualquier_cosa_no_rompe(cliente_api, q):
    for pag in (-5, 0, "a", 10 ** 9):
        r = cliente_api.get("/api/fuentes_catalogo", params={"q": q, "pagina": pag})
        assert r.status_code in (200, 400, 422)


@pytest.mark.parametrize("clave", ["s_", "s_../../etc/passwd", "s_no-existe", "texto", "texto_negrita", "f_zzz", ""])
def test_agregar_claves_raras(cliente_api, clave):
    r = cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": clave})
    assert r.status_code == 400
    assert cliente_api.get("/api/fuente", params={"k": clave}).status_code == 404


def test_tope_de_fuentes_del_catalogo(cliente_api, monkeypatch):
    from hookline import fuentes
    monkeypatch.setattr(fuentes, "MAX_ACTIVAS", 2)
    items = cliente_api.get("/api/fuentes_catalogo").json()["items"]
    for x in items[:2]:
        assert cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": x["clave"]}).status_code == 200
    r = cliente_api.post("/api/fuentes", json={"op": "agregar", "clave": items[2]["clave"]})
    assert r.status_code == 400 and "sacá alguna" in r.json()["error"]


def test_solo_letras_latinas_en_el_catalogo():
    from hookline import fuentes
    for x in fuentes._catalogo_sistema():
        assert fuentes.LATINAS <= fuentes._cmap(x["ruta"])
    rutas = [x["ruta"] for x in fuentes._catalogo_sistema()]
    assert len(rutas) == len(set(rutas)) and len({x["clave"] for x in fuentes._catalogo_sistema()}) == len(rutas)


# --- emojis y caracteres que la letra no tiene ------------------------------------------------------------------------
def test_dibujable_saca_lo_que_la_letra_no_tiene():
    from hookline import fuentes
    p = fuentes.ruta_obligatoria("clasica")
    assert fuentes.dibujable("Cuando me dice 😅 «ya llego» 🔥🔥", p) == "Cuando me dice «ya llego»"
    assert fuentes.dibujable("hola 😅\n🔥\nchau ñandú ¿qué?", p) == "hola\n\nchau ñandú ¿qué?"
    assert fuentes.dibujable("solo 😅🔥", p) == "solo"
    assert fuentes.dibujable("", p) == "" and fuentes.dibujable(None, p) == ""
    assert fuentes.dibujable("igual 😅", None) == "igual 😅"                    # sin fuente: no toca nada
    assert fuentes.dibujable("igual 😅", Path("/no/existe.ttf")) == "igual 😅"
    assert fuentes.faltan("hola 😅🔥😅", "clasica") == "😅🔥"
    assert fuentes.faltan("hola", "clasica") == "" and fuentes.faltan("x😅", "no-existe") == ""


def test_captura_de_chat_con_emojis_no_rompe(tmp_path):
    from hookline import chat_img
    chat_img.dibujar([{"de": "yo", "texto": "hola 😅🔥", "hora": "01:34"}, {"de": "otro", "texto": "😂😂😂"},
                      {"de": "aviso", "texto": "HOY 🎉"}], 500, tmp_path / "c.png")
    chat_img.notificacion("te mandó un mensaje 💌", 500, tmp_path / "n.png")
    assert (tmp_path / "c.png").stat().st_size > 0 and (tmp_path / "n.png").stat().st_size > 0
