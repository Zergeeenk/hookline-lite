"""La fábrica de punta a punta con ffmpeg real: ejemplo → lectura (Claude) → render → juez → repartir."""
import json
import shutil
import subprocess

import pytest

from hookline import medios

pytestmark = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")


def _vaciar_cola(tipos=None):
    """Corre todos los trabajos listos (en este hilo) hasta que no quede ninguno."""
    from hookline import trabajos
    for _ in range(50):
        t = trabajos.tomar("test", tipos or list(trabajos.MANEJADORES))
        if not t:
            return
        trabajos.correr(t)


def _responder(tipo, resultado):
    from hookline.ia import tareas
    pend = [t for t in tareas.pendientes_claude_code() if t.tipo == tipo]
    assert pend, f"no hay pedido de {tipo}"
    (tareas.dir_tarea(pend[0].id) / "resultado.json").write_text(json.dumps(resultado))
    tareas.recoger_claude_code()
    return pend[0]


def _ejemplo(tmp_path):
    p = tmp_path / "ejemplo.mp4"
    subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30:duration=5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


@pytest.fixture
def modelo_con_multi(foto):
    from hookline import almacen
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id="modelo1", activa_desde="2026-01-01"))
        s.flush()
        s.add(Cuenta(id="multi1", main_id="modelo1", activa_desde="2026-01-01"))
    shutil.copy(foto, almacen.dir_de("material", "modelo1", "modelo") / "selfie.jpg")
    return "modelo1"


def _copia_lista(tmp_path, cuenta, lectura):
    from hookline.servicios import fabrica
    cid = fabrica.desde_archivo("ejemplo.mp4", _ejemplo(tmp_path).read_bytes(), cuenta)
    _vaciar_cola()
    _responder("lectura", lectura)
    _vaciar_cola()
    return cid


def test_fabrica_completa_y_reparto_para_aprobar(tmp_path, modelo_con_multi):
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import fabrica, repartir
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "cuando me dice que no", "texto_y": 0.2,
                                                    "texto_tam": 0.04, "modelo": 1, "notas": "ok"})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["resultado"] and c["juez"]["estado"] == "mirando"
    dur = medios.duracion(fabrica.resultado(cid))
    assert dur and 4.5 <= dur <= 5.5
    _responder("juez", {"nota_texto": 4, "nota_tecnica": 4, "motivo": "bien"})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["juez"]["estado"] == "aprobado"
    from hookline.comun import hoy_local
    from datetime import timedelta
    manana = (hoy_local() + timedelta(days=1)).isoformat()
    r = repartir.repartir(manana, 1, 0)
    assert r["reels"] == 1
    with sesion() as s:
        p = s.query(Pieza).filter_by(cuenta_id="multi1").one()
        # sin aprobación pieza por pieza NO queda programada (encargo_aprueba apagado por defecto)
        assert p.estado == "juzgada" and p.plan and p.sugerido and p.cuando is None
        assert p.archivos[0].startswith("para-subir/multi1/.reel-")


def test_menor_se_descarta(tmp_path, modelo_con_multi):
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"descartar": "menor"})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["estado"] == "error" and "menor" in c["error"] and not c.get("resultado")


def test_marca_de_agua_no_se_arma_sola(tmp_path, modelo_con_multi):
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "hola", "modelo": 1, "marca_agua": True})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c.get("marca_agua") and not c.get("resultado")
    with pytest.raises(ValueError):
        fabrica.armar(cid, {}, juzgar=True)


def test_pegar_de_la_lectura_no_se_pierde(tmp_path, modelo_con_multi):
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "hola", "modelo": 1,
                                                    "pegar": [{"x0": 0.1, "y0": 0.5, "x1": 0.9, "y1": 0.7, "cuadro": 1.0}]})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert len(c["pegar"]) == 1 and c["pegar"][0]["x1"] == 0.9


def test_marca_de_agua_bloquea_armado_manual_y_bandeja(tmp_path, modelo_con_multi):
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "hola", "modelo": 1, "marca_agua": True})
    with pytest.raises(ValueError):
        fabrica.armar(cid, {}, juzgar=False)          # el botón Armar del panel tampoco puede
    fabrica._datos(cid, resultado="inventado.mp4")
    with pytest.raises(ValueError):
        fabrica.a_bandeja(cid, "multi1")
    # con un remate propio sí
    fabrica.subir_remate(cid, "propio.mp4", _ejemplo(tmp_path).read_bytes())
    _vaciar_cola(["render"])
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["resultado"] and c["resultado"] != "inventado.mp4"


def test_un_resultado_por_copia_en_resultados(tmp_path, modelo_con_multi):
    from hookline import almacen
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "uno (con paréntesis)", "modelo": 1})
    fabrica.armar(cid, {"texto": "dos"})
    _vaciar_cola(["render"])
    assert len(list(almacen.dir_de("resultados").glob("*.mp4"))) == 1


def test_render_viejo_no_pisa_al_nuevo(tmp_path, modelo_con_multi):
    from hookline import trabajos
    from hookline.servicios import fabrica
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "uno", "modelo": 1})
    fabrica.armar(cid, {"texto": "dos"})
    fabrica.armar(cid, {"texto": "tres"})            # doble clic: el primero queda viejo
    _vaciar_cola(["render"])
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["estado"] == "lista" and c["versiones"][-1]["texto"] == "tres"
    assert not any(v["texto"] == "dos" for v in c["versiones"])


def test_encargo_respeta_la_aprobacion_del_momento(tmp_path, modelo_con_multi, monkeypatch):
    from datetime import timedelta
    from hookline import comun
    from hookline.comun import hoy_local
    from hookline.db import sesion
    from hookline.modelos import Pieza
    from hookline.servicios import cuentas, repartir
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "hola", "modelo": 1})
    _responder("juez", {"nota_texto": 4, "nota_tecnica": 4, "motivo": "ok"})
    monkeypatch.setattr(cuentas, "estado_token", lambda c: {"conectada": True, "vencida": False, "dias_token": 50,
                                                            "puede_publicar": True, "puede_comentar": True,
                                                            "puede_insights": True})
    manana = (hoy_local() + timedelta(days=1)).isoformat()
    repartir.encargo(manana, 0, 0)                   # encargo creado SIN aprobación previa
    comun.guardar_ajuste("encargo_aprueba", True)    # después lo activan
    from hookline.modelos import Encargo
    with sesion() as s:
        e = s.query(Encargo).one()
        e.reels = 1
    repartir.seguir_encargo()
    with sesion() as s:
        p = s.query(Pieza).filter_by(cuenta_id="multi1").one()
        assert p.estado == "juzgada" and not p.aprobada_por   # sigue pidiendo aprobación


def test_repartir_en_paralelo_no_repite(tmp_path, modelo_con_multi):
    import threading
    from datetime import timedelta
    from hookline.comun import hoy_local
    from hookline.db import sesion
    from hookline.modelos import Cuenta, Pieza
    from hookline.servicios import repartir
    with sesion() as s:
        s.add(Cuenta(id="multi2", main_id="modelo1", activa_desde="2026-01-01"))
    _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "hola", "modelo": 1})
    _responder("juez", {"nota_texto": 4, "nota_tecnica": 4, "motivo": "ok"})
    manana = (hoy_local() + timedelta(days=1)).isoformat()
    hs = [threading.Thread(target=repartir.repartir, args=(manana, 1, 0)) for _ in range(4)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    with sesion() as s:
        ps = s.query(Pieza).filter(Pieza.copia_id != "").all()
    assert len(ps) == 1    # una sola copia aprobada → un solo reel, en una sola cuenta


def test_reel_armado_con_una_fuente_subida(tmp_path, modelo_con_multi):
    """Una fuente subida en la sección Fuentes llega al video (ffmpeg drawtext) y el reel sale."""
    from pathlib import Path
    from hookline import fuentes
    from hookline.servicios import fabrica
    ttf = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
    if not ttf.is_file():
        pytest.skip("falta DejaVuSerif-Bold")
    k = fuentes.subir("Mi Letra (1).ttf", ttf.read_bytes(), "test")["nuevas"][0]["clave"]
    fuentes.elegir_predeterminada(k)
    cid = _copia_lista(tmp_path, modelo_con_multi, {"corte": 2.0, "texto": "con letra nueva: ñ á", "modelo": 1})
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["fuente"] == k and c["estado"] == "lista" and c["versiones"]
    fabrica.armar(cid, {"texto": "otra", "fuente": "maquina"})
    _vaciar_cola(["render"])
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["fuente"] == "maquina" and c["versiones"][-1]["texto"] == "otra"
