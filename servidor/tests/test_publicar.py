"""Bandeja y cola: aprobación, publicación idempotente, vencidas."""
import shutil
from datetime import timedelta

import pytest


def _pieza_con_foto(cuenta, foto):
    from hookline import almacen
    from hookline.servicios import piezas
    shutil.copy(foto, almacen.dir_de("subir", cuenta) / "post.jpg")
    assert piezas.escanear() == 1
    return piezas.listar(cuenta)[0]


def test_escanear_y_validar(cuenta_conectada, foto):
    from hookline.servicios import piezas
    p = _pieza_con_foto(cuenta_conectada, foto)
    assert p["tipo"] == "foto" and p["estado"] == "nueva" and p["archivos"][0].startswith("para-subir/modelo1/")
    assert piezas.escanear() == 0
    assert piezas.validar_tipo("reel", p["archivos"]) == "Un Reel es un solo video."
    assert piezas.validar_tipo("carrusel", p["archivos"])


def test_programar_pide_cuenta_conectada(foto):
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    from hookline.servicios import piezas
    with sesion() as s:
        s.add(Cuenta(id="sinconectar"))
    p = _pieza_con_foto("sinconectar", foto)
    with pytest.raises(ValueError, match="no está conectada"):
        piezas.accion({"op": "programar", "id": p["id"], "cuando": "2099-01-01T20:00"})


def test_publicar_una_sola_vez(cuenta_conectada, foto, meta):
    from hookline import trabajos
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza, Trabajo
    from hookline.servicios import piezas
    p = _pieza_con_foto(cuenta_conectada, foto)
    piezas.accion({"op": "programar", "id": p["id"], "caption": "hola #pov"}, usuario="duena")   # publicar ahora
    piezas.revisar_cola()
    piezas.revisar_cola()                                   # dos vueltas: un solo trabajo
    with sesion() as s:
        ts = s.query(Trabajo).filter_by(tipo="publicar").all()
    assert len(ts) == 1
    t = trabajos.tomar("w", ["publicar"])
    trabajos.correr(t)
    with sesion() as s:
        x = s.get(Pieza, p["id"])
        assert x.estado == "publicada", x.error
        assert x.media_id == "MEDIA-1" and x.aprobada_por == "duena"
        assert x.archivos[0].startswith("para-subir/modelo1/_publicados/")
    # la foto se ofreció a Meta por URL firmada de este servicio (sin túnel)
    creados = [d for m, u, _, d in meta.llamadas if m == "POST" and u.endswith("/media")]
    assert creados and creados[0]["image_url"].startswith("https://hookline.test/publico/")
    assert all("TOKEN-SECRETO" not in str(d.get("image_url")) for d in creados)
    # si el trabajo se corriera otra vez (reinicio), no vuelve a publicar
    n = sum(1 for _, u, _, _ in meta.llamadas if u.endswith("/media_publish"))
    piezas.publicar({"pieza": p["id"]})
    assert sum(1 for _, u, _, _ in meta.llamadas if u.endswith("/media_publish")) == n


def test_vencida_si_el_worker_no_llego(cuenta_conectada, foto, meta):
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza
    from hookline.servicios import piezas
    p = _pieza_con_foto(cuenta_conectada, foto)
    piezas.accion({"op": "programar", "id": p["id"], "cuando": "2099-01-01T20:00"})
    with sesion() as s:
        s.get(Pieza, p["id"]).cuando = ahora() - timedelta(minutes=40)   # la hora pasó con el sistema caído
    piezas.revisar_cola()
    with sesion() as s:
        assert s.get(Pieza, p["id"]).estado == "vencida"


def test_anticipo_8_minutos(cuenta_conectada, foto, meta):
    from hookline.db import ahora, sesion
    from hookline.modelos import Pieza, Trabajo
    from hookline.servicios import piezas
    p = _pieza_con_foto(cuenta_conectada, foto)
    piezas.accion({"op": "programar", "id": p["id"], "cuando": "2099-01-01T20:00"})
    with sesion() as s:
        s.get(Pieza, p["id"]).cuando = ahora() + timedelta(minutes=20)
    piezas.revisar_cola()
    with sesion() as s:
        assert s.query(Trabajo).filter_by(tipo="publicar").count() == 0
        s.get(Pieza, p["id"]).cuando = ahora() + timedelta(minutes=5)
    piezas.revisar_cola()
    with sesion() as s:
        assert s.query(Trabajo).filter_by(tipo="publicar").count() == 1


def test_planificadas_pasan_a_aprobar_al_conectar(foto):
    from hookline.db import ahora, sesion
    from hookline.modelos import Cuenta, Pieza
    from hookline.servicios import piezas
    with sesion() as s:
        s.add(Cuenta(id="multi1"))
        s.add(Pieza(id="p1", cuenta_id="multi1", tipo="reel", archivos=[], estado="planificada",
                    cuando=ahora() + timedelta(hours=5), plan=True))
    assert piezas.planificadas_a_revisar("multi1") == 1
    with sesion() as s:
        x = s.get(Pieza, "p1")
        assert x.estado == "juzgada" and x.cuando is None and x.sugerido
