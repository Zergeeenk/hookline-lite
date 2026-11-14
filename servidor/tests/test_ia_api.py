"""Pedidos a Claude con los dos motores, y la API HTTP (login, CSRF, tokens)."""
import json
import shutil
from types import SimpleNamespace

import pytest

VEREDICTO = {"jueces": [{"nombre": "Público", "nota": 8, "comentario": "ok"},
                        {"nombre": "Gancho y tendencia", "nota": 7, "comentario": "ok"},
                        {"nombre": "Seguridad", "nota": 9, "comentario": "ok", "riesgo": "bajo"}],
             "veredicto": "aprobar", "resumen": "bien", "sugerencias": ["a", "b"], "caption": "Hola",
             "hashtags": ["pov", "#humor"], "horario": {"cuando": "2026-11-20T20:00", "motivo": "noche"}}


def _pieza_en_jueces(cuenta, foto):
    from hookline import almacen, trabajos
    from hookline.servicios import jueces, piezas
    shutil.copy(foto, almacen.dir_de("subir", cuenta) / "post.jpg")
    piezas.escanear()
    pid = piezas.listar(cuenta)[0]["id"]
    jueces.pedir_jueces(pid)
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    return pid


def test_motor_claude_code_jueces(cuenta_conectada, foto):
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.modelos import Pieza, TareaIA
    pid = _pieza_en_jueces(cuenta_conectada, foto)
    pend = tareas.pendientes_claude_code()
    assert len(pend) == 1 and pend[0].tipo == "jueces"
    pedido = json.loads((tareas.dir_tarea(pend[0].id) / "pedido.json").read_text())
    assert pedido["imagenes"] and pedido["esquema"]["properties"]["veredicto"]
    tareas.marcar_enviadas([pend[0].id])
    (tareas.dir_tarea(pend[0].id) / "resultado.json").write_text(json.dumps(VEREDICTO))
    assert tareas.recoger_claude_code() == 1
    with sesion() as s:
        x = s.get(Pieza, pid)
        assert x.estado == "juzgada" and x.jueces["promedio"] == 8.0
        assert x.caption.startswith("Hola") and "#pov" in x.caption and x.sugerido == "2026-11-20T20:00"
        assert s.get(TareaIA, pend[0].id).estado == "hecha"


def test_respuesta_invalida_vuelve_la_pieza(cuenta_conectada, foto):
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.modelos import Pieza
    pid = _pieza_en_jueces(cuenta_conectada, foto)
    t = tareas.pendientes_claude_code()[0]
    (tareas.dir_tarea(t.id) / "resultado.json").write_text(json.dumps({"veredicto": "quizas"}))
    tareas.recoger_claude_code()
    with sesion() as s:
        x = s.get(Pieza, pid)
        assert x.estado == "nueva" and "forma esperada" in x.error


class _ClienteFalso:
    def __init__(self, salida=None, stop="end_turn"):
        self.llamadas = []
        salida_ = salida

        def parse(**kw):
            self.llamadas.append(kw)
            esquema = kw["output_format"]
            return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="cyber"), model=kw["model"],
                                   parsed_output=esquema.model_validate(salida_) if salida_ else None,
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))
        self.messages = SimpleNamespace(parse=parse)


def test_motor_api_jueces(cuenta_conectada, foto, monkeypatch):
    from hookline import comun, trabajos
    from hookline.db import sesion
    from hookline.ia import motor_api
    from hookline.modelos import Pieza, TareaIA
    comun.guardar_ajuste("motor_ia", "api")
    falso = _ClienteFalso(VEREDICTO)
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    pid = _pieza_en_jueces(cuenta_conectada, foto)
    trabajos.correr(trabajos.tomar("w", ["ia"]))
    kw = falso.llamadas[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["messages"][0]["content"][0]["type"] == "image"
    with sesion() as s:
        assert s.get(Pieza, pid).estado == "juzgada"
        assert s.query(TareaIA).one().uso["entrada"] == 10


def test_motor_api_rechazo_no_reintenta(cuenta_conectada, foto, monkeypatch):
    from hookline import comun, trabajos
    from hookline.db import sesion
    from hookline.ia import motor_api
    from hookline.modelos import Pieza, TareaIA
    comun.guardar_ajuste("motor_ia", "api")
    monkeypatch.setattr(motor_api, "cliente", lambda: _ClienteFalso(None, stop="refusal"))
    pid = _pieza_en_jueces(cuenta_conectada, foto)
    trabajos.correr(trabajos.tomar("w", ["ia"]))
    with sesion() as s:
        t = s.query(TareaIA).one()
        assert t.estado == "error" and "no quiso" in t.error
        assert s.get(Pieza, pid).estado == "nueva"


def test_respuestas_filtradas_por_reglas(cuenta_conectada):
    from hookline.db import sesion
    from hookline.modelos import Comentario
    from hookline.servicios import comentarios
    with sesion() as s:
        s.add(Comentario(id="1", cuenta_id=cuenta_conectada, texto="hermosa"))
        s.add(Comentario(id="2", cuenta_id=cuenta_conectada, texto="pasame fotos"))
    comentarios.aplicar_sugerencias(cuenta_conectada, [
        {"id": "1", "clase": "positivo", "respuesta": "escribime al privado"},
        {"id": "2", "clase": "acoso", "respuesta": "ok"}])
    d = comentarios.de_cuenta(cuenta_conectada)["items"]
    assert d["1"]["alerta"] == "revisá la respuesta antes de mandar"
    assert d["2"]["sugerencia"] == "" and "ocultarlo" in d["2"]["alerta"]


def test_api_login_csrf_y_tokens(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    anon = TestClient(api.app)
    assert anon.get("/api/estado").status_code == 401
    r = cliente_api.post("/api/cuenta", json={"op": "crear_local", "id": "@modelo2"})
    assert r.status_code == 200 and r.json()["id"] == "modelo2"
    sin = cliente_api.post("/api/cuenta", json={"op": "crear_local", "id": "x"}, headers={"X-CSRF-Token": "mal"})
    assert sin.status_code == 403
    # text/plain (el ataque CSRF de Lite) se rechaza
    assert cliente_api.post("/api/pieza", content='{"op":"x"}', headers={"Content-Type": "text/plain"}).status_code == 415
    tok = cliente_api.post("/api/usuarios", json={"op": "token", "nombre": "pyro"}).json()["token"]
    pyro = TestClient(api.app)
    r = pyro.post("/api/cuenta", json={"op": "crear_local", "id": "desde.pyro"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert pyro.get("/api/ajustes", headers={"Authorization": "Bearer hl_falso"}).status_code == 401
    # el secret nunca vuelve por la API
    cliente_api.post("/api/ajustes", json={"app_id": "12a3", "app_secret": "MUY-SECRETO"})
    aj = cliente_api.get("/api/ajustes").json()
    assert aj["app_id"] == "123" and aj["tiene_secret"] and "MUY-SECRETO" not in json.dumps(aj)


def test_lector_no_puede_cambiar(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    cliente_api.post("/api/usuarios", json={"op": "crear", "email": "ver@x.com", "clave": "clave-de-lector", "rol": "lector"})
    c = TestClient(api.app)
    csrf = c.post("/api/login", json={"email": "ver@x.com", "clave": "clave-de-lector"}).json()["csrf"]
    assert c.get("/api/estado").status_code == 200
    assert c.post("/api/cuenta", json={"op": "crear_local", "id": "y"}, headers={"X-CSRF-Token": csrf}).status_code == 403


def test_media_solo_dentro_del_volumen(cliente_api, foto):
    from hookline import almacen
    shutil.copy(foto, almacen.dir_de("subir", "modelo9") / "a.jpg")
    assert cliente_api.get("/media?p=para-subir/modelo9/a.jpg").status_code == 200
    assert cliente_api.get("/media?p=../../etc/passwd").status_code == 404
    assert cliente_api.get("/media?p=hookline.db").status_code == 404


def test_rutas_de_fabrica_material_y_buscador(cliente_api, foto):
    from hookline import almacen
    assert cliente_api.post("/api/cuenta", json={"op": "crear_local", "id": "modelo5"}).status_code == 200
    assert cliente_api.post("/api/material", json={"cuenta": "modelo5", "op": "nueva_carpeta", "nombre": "Fotos"}).status_code == 200
    r = cliente_api.post("/api/material_subir?cuenta=modelo5&sec=Fotos&nombre=selfie.jpg", content=foto.read_bytes())
    assert r.status_code == 200, r.text
    m = cliente_api.get("/api/material?cuenta=modelo5").json()
    assert m["Fotos"][0]["archivo"].startswith("material/modelo5/Fotos/")
    assert cliente_api.get("/api/modelos").json()["modelos"][0]["id"] == "modelo5"
    r = cliente_api.post("/api/material", json={"cuenta": "modelo5", "op": "a_bandeja", "sec": "Fotos", "nombre": "selfie.jpg"})
    assert r.status_code == 200
    estado = cliente_api.get("/api/estado").json()
    assert len([p for p in estado["piezas"] if p["cuenta"] == "modelo5"]) == 1
    c = cliente_api.get("/api/copias").json()
    assert {"items", "opciones", "links", "busqueda"} <= set(c)
    assert cliente_api.post("/api/copiar", json={"op": "guardar_links",
                                                 "link": "https://www.tiktok.com/@a/video/1 | hola | pov"}).json()["n"] == 1
    r = cliente_api.post("/api/copiar", json={"op": "importar_cookies",
                                              "texto": ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tabc"})
    assert r.status_code in (200, 400)   # 400 si no hay Chrome en la máquina de tests
    assert cliente_api.get("/media?p=" + m["Fotos"][0]["archivo"]).status_code == 200
