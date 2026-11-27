"""Mayor de edad verificada por la agencia (26/11). En Pyro, un admin marca a cada modelo «Mayor de edad verificada por
la agencia (documento revisado)» y la API v1 lo da en ageVerified = {"verified": bool, "at": ...}. Hookline se lo dice
a Claude SOLO para las fotos de esa modelo (lectura del ejemplo y juez), nunca para otras personas, y solo con
verified === true. Ante cualquier duda (sin Pyro, sin modelo atada, error, forma rara), no se dice nada."""
import json
import shutil
import subprocess

import httpx
import pytest

from hookline import medios

CLAVE = "pyro_" + "a1B2c3D4" * 5
URL = "https://pyro.test"
FRASE = "MAYOR DE EDAD"


class PyroFalso:
    def __init__(self):
        self.pedidos = []
        self.fichas = {1: {"id": 1, "name": "Pamela", "ageVerified": {"verified": True, "at": "2026-11-26T22:40:00Z"}},
                       2: {"id": 2, "name": "Valentina", "ageVerified": {"verified": False, "at": None}}}
        self.caido = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req.url.path)
        if self.caido:
            return httpx.Response(500, json={"error": "boom"})
        if req.headers.get("authorization") != f"Bearer {CLAVE}":
            return httpx.Response(401, json={"error": "invalid api key"})
        mid = int(req.url.path.rsplit("/", 1)[1])
        if mid not in self.fichas:
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(200, json=self.fichas[mid])


@pytest.fixture
def pyro(monkeypatch):
    from hookline import pyro as cli
    from hookline.servicios import pyro as svc
    f = PyroFalso()
    monkeypatch.setattr(cli, "transporte", httpx.MockTransport(f))
    monkeypatch.setenv("HOOKLINE_PYRO_URL", URL)
    monkeypatch.setenv("HOOKLINE_PYRO_CLAVE", CLAVE)
    svc._edad.clear()
    yield f
    svc._edad.clear()


def _cuenta(cid, **kw):
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id=cid, **kw))
        s.flush()


# --- la pregunta a Pyro -------------------------------------------------------------------------------------------------
def test_verificada_solo_con_verified_true(pyro):
    from hookline.servicios import pyro as svc
    _cuenta("pame", pyro_modelo_id=1)
    _cuenta("valen", pyro_modelo_id=2)
    _cuenta("nadie", pyro_modelo_id=99)
    _cuenta("suelta")
    assert svc.edad_verificada("pame") is True
    assert svc.edad_verificada("valen") is False
    assert svc.edad_verificada("nadie") is False             # Pyro dice 404
    assert svc.edad_verificada("suelta") is False and svc.edad_verificada("") is False
    assert svc.edad_verificada("no-existe") is False
    assert "/api/v1/models/99" in pyro.pedidos and len(pyro.pedidos) == 3   # sin modelo atada no se pregunta


@pytest.mark.parametrize("av", [None, {}, {"verified": "true"}, {"verified": 1}, {"verified": None}, "verified", [True],
                                {"verified": True, "at": None, "x": 1} and {"verified": False}])
def test_formas_raras_no_cuentan_como_verificada(pyro, av):
    from hookline.servicios import pyro as svc
    pyro.fichas[1] = {"id": 1, "name": "Pamela", **({"ageVerified": av} if av is not None else {})}
    _cuenta("pame", pyro_modelo_id=1)
    assert svc.edad_verificada("pame") is False


def test_la_ficha_de_otra_modelo_no_cuenta(pyro):
    from hookline.servicios import pyro as svc
    pyro.fichas[1] = {"id": 2, "name": "Otra", "ageVerified": {"verified": True, "at": "2026-11-26T22:40:00Z"}}
    _cuenta("pame", pyro_modelo_id=1)
    assert svc.edad_verificada("pame") is False


def test_multicuenta_usa_la_modelo_de_su_main(pyro):
    from hookline.servicios import pyro as svc
    _cuenta("pame", pyro_modelo_id=1)
    _cuenta("pame.multi", main_id="pame")
    assert svc.edad_verificada("pame.multi") is True


def test_sin_pyro_conectado_no_se_pregunta(pyro, monkeypatch):
    from hookline.servicios import pyro as svc
    monkeypatch.delenv("HOOKLINE_PYRO_URL")
    monkeypatch.delenv("HOOKLINE_PYRO_CLAVE")
    _cuenta("pame", pyro_modelo_id=1)
    assert svc.edad_verificada("pame") is False and pyro.pedidos == []


def test_pyro_caido_da_no_verificada_y_reintenta_al_minuto(pyro, monkeypatch):
    from hookline.servicios import pyro as svc
    _cuenta("pame", pyro_modelo_id=1)
    pyro.caido = True
    assert svc.edad_verificada("pame") is False
    assert svc.edad_verificada("pame") is False and len(pyro.pedidos) == 1     # dentro del minuto, no vuelve a pedir
    pyro.caido = False
    _avanzar(monkeypatch, svc, 61)
    assert svc.edad_verificada("pame") is True and len(pyro.pedidos) == 2


def test_desmarcarla_en_pyro_llega_cuando_vence_la_memoria(pyro, monkeypatch):
    from hookline.servicios import pyro as svc
    _cuenta("pame", pyro_modelo_id=1)
    assert svc.edad_verificada("pame") is True
    pyro.fichas[1]["ageVerified"] = {"verified": False, "at": None}
    _avanzar(monkeypatch, svc, 290)
    assert svc.edad_verificada("pame") is True and len(pyro.pedidos) == 1     # 5 min de memoria
    _avanzar(monkeypatch, svc, 301)
    assert svc.edad_verificada("pame") is False and len(pyro.pedidos) == 2


def _avanzar(monkeypatch, svc, segundos):
    """El reloj del módulo de Pyro adelanta `segundos` desde ahora (solo ese módulo: lo demás sigue con el real)."""
    import time
    from types import SimpleNamespace
    ya = time.monotonic()
    monkeypatch.setattr(svc, "time", SimpleNamespace(monotonic=lambda: ya + segundos, sleep=time.sleep))


# --- lo que lee Claude --------------------------------------------------------------------------------------------------
def test_el_texto_va_solo_con_fotos_y_modelo_verificada():
    from hookline.ia import prompts
    base = {"duracion": 5, "cuenta": "pame", "usadas": ""}
    con = prompts.lectura({**base, "hay_fotos": True, "modelo_verificada": True})
    assert FRASE in con and "Vale SOLO para ella" in con and "si puede ser menor, se descarta" in con
    assert "segunda imagen" in con
    assert 'devolvé SOLO {"descartar": "menor"}' in con                   # la regla del ejemplo sigue intacta
    for p in ({**base, "hay_fotos": True, "modelo_verificada": False}, {**base, "hay_fotos": False, "modelo_verificada": True},
              {**base, "hay_fotos": True, "modelo_verificada": "true"}, {**base, "hay_fotos": True}):
        assert FRASE not in prompts.lectura(p)
    j = prompts.juez({"hay_fotos": True, "modelo_verificada": True})
    assert FRASE in j and "foto fija" in j
    assert FRASE not in prompts.juez({"hay_fotos": True}) and FRASE not in prompts.juez({"hay_fotos": False,
                                                                                         "modelo_verificada": True})


def test_si_la_pregunta_falla_la_fabrica_sigue_sin_el_dato(monkeypatch):
    from hookline.servicios import fabrica, pyro as svc
    monkeypatch.setattr(svc, "edad_verificada", lambda c: (_ for _ in ()).throw(RuntimeError("x")))
    assert fabrica._modelo_verificada("pame") is False


# --- de punta a punta: el pedido que le llega a Claude ------------------------------------------------------------------
def _vaciar_cola():
    from hookline import trabajos
    for _ in range(50):
        t = trabajos.tomar("test", list(trabajos.MANEJADORES))
        if not t:
            return
        trabajos.correr(t)


def _pedido(tipo):
    from hookline.ia import tareas
    pend = [t for t in tareas.pendientes_claude_code() if t.tipo == tipo]
    assert pend, f"no hay pedido de {tipo}"
    return pend[0], json.loads((tareas.dir_tarea(pend[0].id) / "pedido.json").read_text())


def _responder(tipo, resultado):
    from hookline.ia import tareas
    t, _ = _pedido(tipo)
    (tareas.dir_tarea(t.id) / "resultado.json").write_text(json.dumps(resultado))
    tareas.recoger_claude_code()


def _ejemplo(tmp_path):
    p = tmp_path / "ejemplo.mp4"
    subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


@pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")
@pytest.mark.parametrize("modelo,verificada", [(1, True), (2, False)])
def test_copiar_video_le_pasa_el_dato_a_la_lectura_y_al_juez(tmp_path, pyro, foto, modelo, verificada):
    from hookline import almacen
    from hookline.servicios import fabrica
    _cuenta("modelo1", pyro_modelo_id=modelo, activa_desde="2026-01-01")
    shutil.copy(foto, almacen.dir_de("material", "modelo1", "modelo") / "selfie.jpg")
    fabrica.desde_archivo("ejemplo.mp4", _ejemplo(tmp_path).read_bytes(), "modelo1")
    _vaciar_cola()
    t, pedido = _pedido("lectura")
    assert t.entrada["datos"]["modelo_verificada"] is verificada
    assert (FRASE in pedido["instrucciones"]) is verificada
    _responder("lectura", {"corte": 2.0, "texto": "cuando me dice que no", "texto_y": 0.2, "texto_tam": 0.04,
                           "modelo": 1, "notas": "ok"})
    _vaciar_cola()
    t, pedido = _pedido("juez")
    assert t.entrada["datos"]["modelo_verificada"] is verificada
    assert (FRASE in pedido["instrucciones"]) is verificada


@pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")
def test_sin_fotos_de_la_modelo_no_se_pregunta_ni_se_dice(tmp_path, pyro):
    from hookline.servicios import fabrica
    _cuenta("modelo1", pyro_modelo_id=1, activa_desde="2026-01-01")
    fabrica.desde_archivo("ejemplo.mp4", _ejemplo(tmp_path).read_bytes(), "modelo1")
    _vaciar_cola()
    t, pedido = _pedido("lectura")
    assert t.entrada["datos"]["modelo_verificada"] is False and FRASE not in pedido["instrucciones"]
    assert pyro.pedidos == []
