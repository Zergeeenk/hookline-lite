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
