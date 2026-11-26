"""Rechazos de Claude por edad (26/11). Con reels de chicas, cuando Claude no puede asegurar que alguien es mayor de
edad se niega. Eso está bien y NO se saltea; lo que estaba mal era lo que se perdía alrededor:

- Buscador: Claude mira las miniaturas en hojas de 40. Si rechazaba la hoja por una sola dudosa, se perdían las 40.
  Ahora la hoja rechazada se parte en mitades hasta aislar lo dudoso (grupos de MIN_PARTE): lo que Claude sigue
  rechazando se descarta y lo bueno se guarda. Lo rechazado nunca entra al banco.
- Copiar video: un rechazo al leer el ejemplo queda como descartado con su motivo (como «descartar: menor»), no como
  un error raro."""
import json
from types import SimpleNamespace

from sqlalchemy import select

RECHAZO = "Claude no quiso hacer este pedido (motivo: seguridad). Revisalo a mano."


def _lote(tandas: int = 1, objetivo: int = 1):
    from PIL import Image
    from hookline import almacen
    from hookline.ia import tareas
    from hookline.servicios import buscador
    lote = "1126-1700-abcd"
    d = almacen.dir_de("candidatos", lote)
    total = buscador.POR_TANDA * tandas
    cands = []
    for n in range(1, total + 1):
        Image.new("RGB", (30, 50), (n * 5 % 255, 80, 120)).save(d / f"{n}.jpg")
        cands.append({"n": n, "link": f"https://www.instagram.com/reel/R{n:03d}xyz/", "alt": f"meme {n}",
                      "fuente": "ig pov soltera"})
    (d / "candidatos.json").write_text(json.dumps(cands), encoding="utf-8")
    buscador.estado(estado="filtrando", lote=lote, hojas=tandas, hechas=0, nuevos=0, acumulado=0,
                    vuelta=buscador.MAX_VUELTAS, objetivo=objetivo, partes={}, descartadas_edad=0)
    ids = []
    for k, (hoja, rango, parte) in enumerate(buscador._hojas(d, cands), 1):
        ids.append(tareas.crear("filtrar", {"desde": rango[0], "hasta": rango[1], "textos": {}},
                                referencia=f"{lote}:{k}", imagenes=[almacen.relativa(hoja)]))
    return lote, ids


def _abiertas() -> dict:
    """Las tareas de filtro pendientes: referencia → id."""
    from hookline.db import sesion
    from hookline.modelos import TareaIA
    with sesion() as s:
        return {t.referencia: t.id for t in s.scalars(select(TareaIA).where(TareaIA.tipo == "filtrar",
                                                                            TareaIA.estado == "pendiente"))}


def _banco() -> set:
    from hookline.db import sesion
    from hookline.modelos import LinkBanco
    with sesion() as s:
        return {int(x.link.split("/reel/R")[1][:3]) for x in s.scalars(select(LinkBanco))}


def _sirven(*ns):
    return {"sirven": [{"n": n, "cat": "pov", "titulo": f"t{n}"} for n in ns]}


def test_hoja_rechazada_se_parte_hasta_aislar_lo_dudoso_y_lo_bueno_se_guarda():
    from hookline import almacen
    from hookline.ia import tareas
    from hookline.servicios import buscador
    lote, (t1,) = _lote()
    tareas.fallar(t1, RECHAZO)
    ab = _abiertas()
    assert set(ab) == {f"{lote}:1:1-20", f"{lote}:1:21-40"}
    assert buscador.estado()["partes"] == {"1": 2} and buscador.estado()["estado"] == "filtrando"
    d = almacen.dir_de("candidatos", lote)
    assert (d / "hoja-1-1-20.jpg").is_file() and (d / "hoja-1-21-40.jpg").is_file()

    tareas.completar(ab[f"{lote}:1:1-20"], _sirven(3, 25))       # el 25 no estaba en esa hoja: no entra
    assert _banco() == {3} and buscador.estado()["hechas"] == 0

    tareas.fallar(ab[f"{lote}:1:21-40"], RECHAZO)
    ab = _abiertas()
    assert set(ab) == {f"{lote}:1:21-30", f"{lote}:1:31-40"}
    tareas.fallar(ab[f"{lote}:1:21-30"], RECHAZO)
    ab = _abiertas()
    assert set(ab) == {f"{lote}:1:21-25", f"{lote}:1:26-30", f"{lote}:1:31-40"}
    tareas.fallar(ab[f"{lote}:1:21-25"], RECHAZO)               # 5: ya no se parte, se descarta entera
    assert set(_abiertas()) == {f"{lote}:1:26-30", f"{lote}:1:31-40"}
    assert buscador.estado()["descartadas_edad"] == 5 and buscador.estado()["estado"] == "filtrando"

    tareas.completar(ab[f"{lote}:1:26-30"], _sirven(27))
    assert buscador.estado()["estado"] == "filtrando"            # falta la última parte
    tareas.completar(ab[f"{lote}:1:31-40"], _sirven(35, 22))     # el 22 era de la parte rechazada: nunca entra
    e = buscador.estado()
    assert e["estado"] == "listo" and e["acumulado"] == 3, e
    assert "3 memes nuevos" in e["mensaje"] and "5 se descartaron" in e["mensaje"] and "mayores de edad" in e["mensaje"]
    assert _banco() == {3, 27, 35}
    assert not (almacen.raiz() / almacen.SECCIONES["candidatos"] / lote).exists()


def test_una_falla_que_no_es_rechazo_no_parte_la_hoja():
    from hookline.ia import tareas
    from hookline.servicios import buscador
    lote, (t1,) = _lote()
    tareas.fallar(t1, "La API de Claude no respondió después de 3 intentos: timeout")
    assert _abiertas() == {}
    e = buscador.estado()
    assert e["estado"] == "listo" and e["acumulado"] == 0 and "descartaron" not in e["mensaje"]


def test_varias_hojas_la_busqueda_termina_cuando_terminan_todas_sus_partes():
    from hookline.ia import tareas
    from hookline.servicios import buscador
    lote, (t1, t2) = _lote(tandas=2)
    tareas.fallar(t2, RECHAZO)                                    # la hoja 2 se parte
    ab = _abiertas()
    assert set(ab) == {f"{lote}:1", f"{lote}:2:41-60", f"{lote}:2:61-80"}
    tareas.completar(ab[f"{lote}:1"], _sirven(5))                 # la hoja 1, entera, termina
    e = buscador.estado()
    assert e["estado"] == "filtrando" and e["hechas"] == 1
    tareas.completar(ab[f"{lote}:2:41-60"], _sirven(50))
    assert buscador.estado()["estado"] == "filtrando" and buscador.estado()["hechas"] == 1
    tareas.completar(ab[f"{lote}:2:61-80"], _sirven())
    e = buscador.estado()
    assert e["estado"] == "listo" and e["hechas"] == 2 and _banco() == {5, 50}


def test_rechazo_de_otro_lote_no_toca_la_busqueda_actual():
    from hookline.ia import tareas
    from hookline.servicios import buscador
    lote, (t1,) = _lote()
    buscador.estado(lote="otro-lote")
    tareas.fallar(t1, RECHAZO)
    assert _abiertas() == {} and buscador.estado()["lote"] == "otro-lote"


def test_es_rechazo_reconoce_el_mensaje_del_motor():
    import pytest
    from hookline.ia import motor_api
    with pytest.raises(motor_api.ErrorIA) as ex:
        motor_api._revisar_rechazo(SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="x")))
    assert motor_api.es_rechazo(str(ex.value))
    assert not motor_api.es_rechazo("La API de Claude rechazó la clave: inválida. Revisala en Ajustes.")
    assert not motor_api.es_rechazo(None) and not motor_api.es_rechazo("")


def test_copiar_video_rechazo_al_leer_queda_descartado_con_su_motivo():
    from hookline.ia import tareas
    from hookline.servicios import fabrica
    cid = fabrica._nueva(link="https://www.instagram.com/reel/AbCdE12345/", estado="leyendo")
    t = tareas.crear("lectura", {"duracion": 4}, referencia=cid)
    tareas.fallar(t, RECHAZO)
    c = fabrica._datos(cid)
    assert c["estado"] == "error" and c["leido"] is True
    assert c["error"].startswith("Descartado: Claude no aceptó este ejemplo") and "mayor de edad" in c["error"]


def test_copiar_video_rechazo_hace_lo_mismo_que_descartar_menor():
    """Igual que «descartar: menor»: link votado malo, la cuenta deja de ser fuente y, en «Hacer N», va otro link."""
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.modelos import FuenteBusqueda, LinkBanco
    from hookline.servicios import buscador, fabrica
    usado, libre = "https://www.instagram.com/reel/RECH01/", "https://www.instagram.com/reel/LIBRE2/"
    cid = fabrica._nueva(link=usado, estado="leyendo", cuenta="", reponer=fabrica.REPONER)
    fabrica.guardar_links(usado, {usado: cid})
    fabrica.guardar_links(libre)
    with sesion() as s:
        s.get(LinkBanco, usado).datos = {"ficha": "ok", "usuario": "cuenta.rara"}
        s.get(LinkBanco, libre).datos = {"ficha": "ok"}
    buscador.sumar_fuentes(["ig @cuenta.rara"])
    t = tareas.crear("lectura", {"duracion": 4}, referencia=cid)
    tareas.fallar(t, RECHAZO)
    c = fabrica._datos(cid)
    assert c["error"].startswith("Descartado: Claude no aceptó este ejemplo")
    assert "@cuenta.rara" in c["error"] and "otro link" in c["error"]
    with sesion() as s:
        assert (s.get(LinkBanco, usado).voto, s.get(LinkBanco, usado).motivo) == ("malo", "Parece menor")
        assert not any(f.activa for f in s.scalars(select(FuenteBusqueda)))
        assert s.get(LinkBanco, libre).copia_id not in ("", "reponiendo")


def test_copiar_video_otra_falla_sigue_como_antes():
    from hookline.ia import tareas
    from hookline.servicios import fabrica
    cid = fabrica._nueva(link="https://www.instagram.com/reel/AbCdE12346/", estado="leyendo")
    t = tareas.crear("lectura", {"duracion": 4}, referencia=cid)
    tareas.fallar(t, "La API de Claude no respondió después de 3 intentos: timeout")
    c = fabrica._datos(cid)
    assert c["estado"] == "error" and c["error"].startswith("Claude no pudo leer el ejemplo")
