"""Un test por cada bug que encontraron las revisiones (para que no vuelvan)."""
import shutil
import threading
import time
from datetime import timedelta

import pytest


@pytest.fixture
def pieza(cuenta_conectada, foto):
    from hookline import almacen
    from hookline.servicios import piezas
    shutil.copy(foto, almacen.dir_de("subir", cuenta_conectada) / "post.jpg")
    piezas.escanear()
    return piezas.listar(cuenta_conectada)[0]["id"]


def _correr_publicar():
    from hookline import trabajos
    while (t := trabajos.tomar("w", ["publicar"])):
        trabajos.correr(t)


def _publicaciones(meta):
    return sum(1 for _, u, _, _ in meta.llamadas if u.endswith("/media_publish"))


def test_no_se_pueden_bajar_archivos_internos_por_agregar_encontrados(cliente_api):
    r = cliente_api.post("/api/copiar", json={"op": "agregar_encontrados", "items": [{"link": "https://x", "img": "hookline.db"}]})
    assert r.status_code == 400


def test_cancelar_y_reprogramar_a_la_misma_hora_publica(pieza, meta):
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    piezas.accion({"op": "programar", "id": pieza, "cuando": "2099-01-01T20:00"})
    with sesion() as s:
        s.get(Pieza, pieza).cuando = ahora() + timedelta(minutes=5)
    piezas.revisar_cola()
    piezas.accion({"op": "cancelar", "id": pieza})
    _correr_publicar()                                  # el trabajo viejo corre y no hace nada
    with sesion() as s:
        cuando = s.get(Pieza, pieza).cuando
    from hookline.comun import iso_local
    time.sleep(1.1)                                     # otra aprobación (en otro segundo)
    piezas.accion({"op": "programar", "id": pieza, "cuando": iso_local(cuando)})
    with sesion() as s:
        s.get(Pieza, pieza).cuando = ahora() + timedelta(seconds=1)
    piezas.revisar_cola()
    _correr_publicar()
    with sesion() as s:
        assert s.get(Pieza, pieza).estado == "publicada"
    assert _publicaciones(meta) == 1


def test_worker_caido_mientras_sube_no_publica_tarde(pieza, meta):
    from hookline import trabajos
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza, Trabajo
    from hookline.servicios import piezas
    piezas.accion({"op": "programar", "id": pieza, "cuando": "2099-01-01T20:00"})
    with sesion() as s:
        s.get(Pieza, pieza).cuando = ahora() + timedelta(minutes=3)
    piezas.revisar_cola()
    t = trabajos.tomar("muerto:999999", ["publicar"])
    assert t
    # el worker se muere justo después de pasar la pieza a 'subiendo'
    piezas._pasar(pieza, "programada", estado="subiendo", etapa="arrancando")
    with sesion() as s:   # pasan 40 minutos
        s.get(Trabajo, t.id).tomado_hasta = ahora() - timedelta(seconds=1)
        s.get(Pieza, pieza).cuando = ahora() - timedelta(minutes=37)
    assert trabajos.tomar("otro", ["publicar"]) is None        # no se reabre (max_intentos=1)
    piezas.revisar_cola()
    with sesion() as s:
        x = s.get(Pieza, pieza)
        assert x.estado == "error" and "Instagram" in x.error
    assert _publicaciones(meta) == 0


def test_dos_trabajos_de_la_misma_pieza_publican_una_vez(pieza, meta):
    from hookline.servicios import piezas
    piezas.accion({"op": "programar", "id": pieza})
    datos = {"pieza": pieza, "clave": None}
    hs = [threading.Thread(target=piezas.publicar, args=(datos,)) for _ in range(5)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert _publicaciones(meta) == 1


def test_cancelar_mientras_el_worker_la_toma_no_pisa(pieza, meta):
    from sqlalchemy.orm.exc import StaleDataError  # noqa: F401
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    piezas.accion({"op": "programar", "id": pieza})
    # el panel leyó 'programada'...
    s = sesion().__enter__()
    x = s.get(Pieza, pieza)
    # ...mientras tanto el worker la toma
    assert piezas._pasar(pieza, "programada", estado="subiendo")
    x.estado = "juzgada"                                # el panel intenta cancelar con lo que leyó
    with pytest.raises(Exception):
        s.commit()
    s.rollback()
    s.close()
    with sesion() as s2:
        assert s2.get(Pieza, pieza).estado == "subiendo"


def test_horas_con_zona_desde_pyro():
    from hookline.comun import desde_local
    assert desde_local("2026-11-20T23:00:00Z").hour == 23
    assert desde_local("2026-11-20T20:00:00-03:00").hour == 23
    assert desde_local("2026-11-20T20:00").hour == 23          # sin zona = hora local (Argentina)


def test_contenedor_ya_publicado_no_se_publica_de_nuevo(pieza, meta, monkeypatch):
    from hookline import instagram
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    piezas.accion({"op": "programar", "id": pieza})
    with sesion() as s:
        s.get(Pieza, pieza).contenedor_id = "CONT-VIEJO"
    real = instagram.pedir
    monkeypatch.setattr(instagram, "pedir", lambda m, u, p=None, d=None, **k:
                        {"status_code": "PUBLISHED"} if "CONT-VIEJO" in u else real(m, u, p, d, **k))
    piezas.revisar_cola()
    _correr_publicar()
    with sesion() as s:
        assert s.get(Pieza, pieza).estado == "publicada"
    assert _publicaciones(meta) == 0


def test_resultado_de_claude_se_aplica_una_sola_vez(cuenta_conectada):
    from hookline.ia import tareas
    from hookline.modelos import MensajeChat
    from hookline.db import sesion
    from hookline.servicios import jueces
    jueces.mensaje("hola")
    t = tareas.pendientes_claude_code()[0]
    hs = [threading.Thread(target=tareas.completar, args=(t.id, {"respuesta": "chau"})) for _ in range(6)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    with sesion() as s:
        assert s.query(MensajeChat).filter_by(de="claude").count() == 1


def test_pedido_de_claude_code_que_nadie_atiende_vence(pieza):
    from hookline import trabajos
    from hookline.db import ahora, sesion
    from hookline.ia import tareas
    from hookline.modelos import Pieza, TareaIA
    from hookline.servicios import piezas
    piezas.accion({"op": "jueces", "id": pieza})
    trabajos.correr(trabajos.tomar("w", ["preparar_jueces"]))
    with sesion() as s:
        s.query(TareaIA).one().creado = ahora() - timedelta(hours=4)    # el puente nunca corrió
    tareas.recoger_claude_code()
    with sesion() as s:
        assert s.query(TareaIA).one().estado == "error"
        assert s.get(Pieza, pieza).estado == "nueva"


def test_migrar_lite_subiendo_queda_en_error(tmp_path):
    import json
    from hookline.db import sesion
    from hookline.migrar_lite import migrar
    from hookline.modelos import Pieza
    lite = tmp_path / "lite"
    (lite / "cuentas" / "c1").mkdir(parents=True)
    (lite / "datos").mkdir()
    (lite / "cuentas" / "c1" / "cuenta.json").write_text(json.dumps({"usuario": "c1"}))
    (lite / "datos" / "piezas.json").write_text(json.dumps([{"id": "p1", "cuenta": "c1", "tipo": "reel",
                                                             "estado": "subiendo", "ahora": True}]))
    migrar(str(lite))
    with sesion() as s:
        assert s.get(Pieza, "p1").estado == "error"


def test_webhook_no_se_manda_dos_veces_con_varios_workers(monkeypatch):
    import httpx
    from hookline import config, eventos
    from hookline.comun import evento
    monkeypatch.setenv("HOOKLINE_WEBHOOK_URL", "https://pyro.test/h")
    config.recargar()
    recibidos = []
    real = httpx.Client
    monkeypatch.setattr(eventos.httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: (recibidos.append(1), httpx.Response(200))[1]), **kw))
    for i in range(10):
        evento("pieza.publicada", pieza=f"p{i}")
    hs = [threading.Thread(target=eventos.enviar_pendientes) for _ in range(5)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert len(recibidos) == 10


def test_clave_de_claude_desde_el_panel(cliente_api, monkeypatch):
    import json as _json
    from hookline.ia import motor_api, tareas
    probadas = []
    monkeypatch.setattr(motor_api, "probar_clave", lambda c: probadas.append(c))
    assert cliente_api.get("/api/ajustes").json()["tiene_clave_claude"] is False
    assert cliente_api.post("/api/ajustes", json={"anthropic_api_key": "hola"}).status_code == 400
    clave = "sk-ant-api03-" + "x" * 60 + "FINAL"
    r = cliente_api.post("/api/ajustes", json={"anthropic_api_key": "  " + clave + "\n"})
    assert r.status_code == 200 and r.json()["tiene_clave_claude"] and r.json()["clave_claude_fin"] == "INAL"
    assert probadas == [clave]
    for ruta in ("/api/ajustes", "/api/estado"):
        assert clave not in cliente_api.get(ruta).text
    from hookline.db import sesion
    from hookline.modelos import Ajuste
    with sesion() as s:
        assert clave not in _json.dumps(s.get(Ajuste, "anthropic_api_key").valor)   # cifrada en la base
    cliente_api.post("/api/ajustes", json={"motor_ia": "api"})
    comun_motor = tareas.estado_puente()
    assert comun_motor["puente"] == "escuchando"
    assert motor_api.cliente().api_key == clave
    cliente_api.post("/api/ajustes", json={"borrar_clave_claude": True})
    assert cliente_api.get("/api/ajustes").json()["tiene_clave_claude"] is False


def test_clave_rechazada_por_anthropic_no_se_guarda(cliente_api, monkeypatch):
    from hookline.ia import motor_api

    def rechazar(c):
        raise ValueError("Anthropic rechazó esa clave")
    monkeypatch.setattr(motor_api, "probar_clave", rechazar)
    r = cliente_api.post("/api/ajustes", json={"anthropic_api_key": "sk-ant-api03-" + "y" * 60})
    assert r.status_code == 400 and "rechazó" in r.json()["error"]
    assert cliente_api.get("/api/ajustes").json()["tiene_clave_claude"] is False


def test_solo_admin_carga_la_clave(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    cliente_api.post("/api/usuarios", json={"op": "crear", "email": "ed@x.com", "clave": "clave-larga-123", "rol": "editor"})
    ed = TestClient(api.app)
    ed.headers["X-CSRF-Token"] = ed.post("/api/login", json={"email": "ed@x.com", "clave": "clave-larga-123"}).json()["csrf"]
    assert ed.post("/api/ajustes", json={"anthropic_api_key": "sk-ant-api03-" + "z" * 60}).status_code == 403


def test_probar_conexion_con_claude(cliente_api, monkeypatch):
    from types import SimpleNamespace
    from hookline.ia import motor_api
    llamadas = []

    def create(**kw):
        llamadas.append(kw)
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text="listo")])
    monkeypatch.setattr(motor_api, "cliente", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    r = cliente_api.post("/api/probar_claude", json={})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["respuesta"] == "listo"
    assert llamadas[0]["model"] == "claude-haiku-5-5" and llamadas[0]["max_tokens"] <= 64


def test_probar_conexion_sin_clave_avisa(cliente_api):
    r = cliente_api.post("/api/probar_claude", json={})
    assert r.status_code == 400 and "API key" in r.json()["error"]


@pytest.mark.parametrize("email,clave", [
    ("x" * 300 + "@a.com", "clave-larga-123"),                 # más largo que la columna (en Postgres era un 500)
    ({"a": [None, False]}, "clave-larga-123"),                  # lo que encontró el fuzz en la CI
    ("sin-arroba", "clave-larga-123"),
    ("a@b", "clave-larga-123"),
    ("con espacio@x.com", "clave-larga-123"),
    ("ok@x.com", "corta"),
    ("ok@x.com", "x" * 500),
    ("ok@x.com", None),
])
def test_crear_usuario_con_datos_malos_da_400(cliente_api, email, clave):
    r = cliente_api.post("/api/usuarios", json={"op": "crear", "email": email, "clave": clave, "nombre": "n" * 500})
    assert r.status_code == 400


def test_crear_usuario_recorta_el_nombre(cliente_api):
    r = cliente_api.post("/api/usuarios", json={"op": "crear", "email": "Larga@X.com", "clave": "clave-larga-123",
                                                "nombre": "N\x07" * 300})
    assert r.status_code == 200
    u = next(u for u in cliente_api.get("/api/usuarios").json()["usuarios"] if u["email"] == "larga@x.com")
    assert len(u["nombre"]) == 120 and "\x07" not in u["nombre"]


def test_callback_de_instagram_explica_cada_caso(cliente_api):
    """Lo que vio la evaluadora: volver sin código, o con un error de Meta. Siempre un mensaje claro y escapado."""
    from fastapi.testclient import TestClient
    from hookline import api
    c = TestClient(api.app)                     # sin login: es Instagram el que vuelve
    r = c.get("/instagram/callback")
    assert r.status_code == 200 and "ID de configuración" in r.text and "redireccionamiento" in r.text
    r = c.get("/instagram/callback", params={"error": "access_denied", "error_reason": "user_denied"})
    assert "Se canceló el permiso" in r.text
    r = c.get("/instagram/callback", params={"error": "invalid_request",
                                              "error_description": "<script>alert(1)</script> Invalid platform app"})
    assert "Invalid platform app" in r.text and "<script>alert" not in r.text
    r = c.get("/instagram/callback", params={"code": "abc", "state": "inventado"})
    assert "expiró" in r.text


@pytest.mark.parametrize("minima,clave,ok", [
    (None, "corta", False), (None, "diez-letra", True),          # por defecto: 10
    ("5", "abcd", False), ("5", "abcde", True),                 # bajada a 5
    ("2", "abc", False), ("2", "abcd", True),                   # nunca menos de 4
    ("100", "x" * 63, False), ("100", "x" * 64, True),          # ni más de 64
    ("basura", "corta", False),
])
def test_largo_minimo_de_contrasena_configurable(cliente_api, monkeypatch, minima, clave, ok):
    from hookline import config
    if minima is not None:
        monkeypatch.setenv("HOOKLINE_CLAVE_MINIMA", minima)
    config.recargar()
    r = cliente_api.post("/api/usuarios", json={"op": "crear", "email": "nuevo@x.com", "clave": clave, "rol": "editor"})
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert "caracteres" in r.json()["error"]


def test_crear_usuario_por_consola_con_error_no_muestra_traceback(capsys, monkeypatch):
    from types import SimpleNamespace
    from hookline import cli
    monkeypatch.setattr(cli, "_base", lambda: None)
    with pytest.raises(SystemExit):
        cli.cmd_usuario(SimpleNamespace(email="EMAIL_DE_ZERGEN", clave="clave-larga-123", nombre="", rol="admin"))
    assert "No se creó el usuario: Escribí un email válido." in capsys.readouterr().out
