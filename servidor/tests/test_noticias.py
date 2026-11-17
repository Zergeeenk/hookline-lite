"""Espacio Noticias: radar → guion → jurado, chequeo sin IA, costos, espacios y la API. Se testea a romper."""
import math
import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

CID = "loqueborraron"
FUENTE = "https://www.infobae.com/politica/2026/11/17/nota/"
USO_OPUS = {"modelo": "claude-opus-5-5", "entrada": 10_000, "salida": 2_000, "cache": 5_000, "cache_escrita": 1_000,
            "busquedas": 6}


def guion_ok(palabras: int = 170, cta_en: int = 90, final: str = "Cerró en 1.250 pesos.") -> str:
    """Un guion que cumple todo: gancho de 7 palabras, el seguime en la palabra cta_en, cierre con dato."""
    gancho = "El dólar subió 30 por ciento hoy."
    cta = "Seguime y dale like."
    n_gancho, n_cta, n_final = 7, 4, len(final.split())
    i = 0

    def frase(n):
        nonlocal i
        ws = []
        for _ in range(n):
            ws.append(f"w{i}")
            i += 1
        return ws
    antes = frase(cta_en - n_gancho)
    despues = frase(palabras - cta_en - n_cta - n_final)
    trozos = [gancho]
    for k in range(0, len(antes), 9):
        trozos.append(" ".join(antes[k:k + 9]) + ".")
    trozos.append(cta)
    for k in range(0, len(despues), 9):
        trozos.append(" ".join(despues[k:k + 9]) + ".")
    trozos.append(final)
    return " ".join(trozos)


@pytest.fixture
def cuenta_noticias():
    from hookline.db import sesion
    from hookline.modelos import Cuenta
    with sesion() as s:
        s.add(Cuenta(id=CID, usuario_ig=CID, espacio="noticias"))
        s.add(Cuenta(id="modelo1", usuario_ig="modelo1"))
    return CID


def _tarea(tipo, ref=None):
    from sqlalchemy import select
    from hookline.db import sesion
    from hookline.modelos import TareaIA
    with sesion() as s:
        q = select(TareaIA).where(TareaIA.tipo == tipo, TareaIA.estado.in_(("pendiente", "enviada")))
        if ref is not None:
            q = q.where(TareaIA.referencia == str(ref))
        ts = s.scalars(q.order_by(TareaIA.creado)).all()
    return ts


def _item(titulo="Diputados aprobó el presupuesto 2027", area="Casa Rosada y Congreso", **kw):
    return {"area": area, "titulo": titulo, "resumen": "Se votó en la madrugada.", "por_que": "Define el gasto.",
            "datos": ["129 votos a favor (Infobae)"], "fuentes": [FUENTE], "urgencia": 9, "potencial": 8, **kw}


def _radar(items, uso=USO_OPUS, auto=False):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    noticias.pedir_radar(CID, auto=auto)
    t = _tarea("radar", CID)[-1]
    tareas.completar(t.id, {"items": items}, uso)
    return t.id


def _guion_y_jurado(nid, texto=None, notas=8, mejorado="", uso=USO_OPUS):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    g = noticias.pedir_guion(nid)
    tg = _tarea("guion", g["id"])[-1]
    tareas.completar(tg.id, {"titulo": "Presupuesto 2027", "texto": texto or guion_ok(), "caption": "Lo que se votó.",
                             "hashtags": ["presupuesto", "#Congreso", "presupuesto", "con espacio"],
                             "fuentes": [FUENTE, "javascript:alert(1)"], "datos": ["129 votos"]}, uso)
    tj = _tarea("jurado", g["id"])[-1]
    from hookline.ia.prompts import JURADO_NOTICIAS
    notas = notas if isinstance(notas, list) else [notas] * 10
    tareas.completar(tj.id, {"personas": [{"nombre": p["nombre"], "nota": n, "se_iria": None if n >= 7 else 20,
                                           "comentario": "ok"} for p, n in zip(JURADO_NOTICIAS, notas)],
                             "mejoras": ["a", "b", "c", "d"], "texto_mejorado": mejorado, "resumen": "bien"},
                     {"modelo": "claude-opus-5-5", "entrada": 3000, "salida": 1500})
    return noticias.obtener(g["id"])


# --- chequeo sin IA ---------------------------------------------------------------------------------------------------------
def test_un_guion_que_cumple_todo_pasa():
    from hookline.servicios.noticias import chequear
    ch = chequear(guion_ok(), [FUENTE])
    assert ch["ok"], ch["problemas"]
    assert ch["palabras"] == 170 and ch["segundos"] == 68.0 and ch["cta_segundo"] == 36.0 and ch["gancho_palabras"] == 7


@pytest.mark.parametrize("texto,falla", [
    (guion_ok(140, 85), "le faltan"),
    (guion_ok(200, 95), "le sobran"),
    (guion_ok(170, 60), "segundo ~24"),
    (guion_ok(170, 115), "segundo ~46"),
    (guion_ok(final="Y esto recién empieza."), "cierre no tiene un dato"),
    ("Hola a todos, hoy les cuento. " + guion_ok(160), "saludando"),
    (guion_ok().replace("Seguime y dale like.", "Mirá esto ahora ya."), "Falta"),
    (guion_ok().replace("El dólar subió 30 por ciento hoy.", "El dólar subió treinta por ciento en un solo día hoy."),
     "gancho"),
])
def test_cada_regla_del_chequeo_salta_sola(texto, falla):
    from hookline.servicios.noticias import chequear
    ch = chequear(texto, [FUENTE])
    assert not ch["ok"]
    assert any(falla in p for p in ch["problemas"]), ch["problemas"]


def test_seguime_dos_veces_frases_repetidas_prohibidas_y_acotaciones():
    from hookline.servicios.noticias import chequear
    t = guion_ok().replace("w112 w113", "seguime y dale like")
    assert any("2 veces" in p for p in chequear(t, [FUENTE])["problemas"])
    t = guion_ok().replace("w10 w11 w12 w13", "la inflación de octubre").replace("w120 w121 w122 w123",
                                                                              "la inflación de octubre")
    assert any("Frases repetidas" in p and "inflacion de octubre" in p for p in chequear(t, [FUENTE])["problemas"])
    t = guion_ok().replace("w20", "Corrupto")
    assert any("corrupto" in p for p in chequear(t, [FUENTE])["problemas"])
    assert chequear(guion_ok().replace("w20", "incorruptible"), [FUENTE])["ok"]     # palabra entera, no un pedazo
    t = guion_ok().replace("w30", "[pausa]")
    assert any("acotaciones" in p for p in chequear(t, [FUENTE])["problemas"])
    assert any("fuentes" in p for p in chequear(guion_ok(), [])["problemas"])
    assert any("fuentes" in p for p in chequear(guion_ok(), ["javascript:alert(1)"])["problemas"])


def test_avisa_datos_ya_usados_y_emojis_sin_frenar():
    from hookline.servicios.noticias import chequear
    ch = chequear(guion_ok().replace("w40", "🔥"), [FUENTE], ["Cerró en 1250 pesos (BCRA)", "3 votos"])
    assert ch["ok"]
    assert any("1250" in a for a in ch["avisos"]) and any("emojis" in a for a in ch["avisos"])
    assert not any("3" == x for a in ch["avisos"] for x in a.split())       # un dígito suelto no es "dato repetido"


def test_cifras_y_cierre_con_dato():
    from hookline.servicios.noticias import _cifras, _tiene_dato
    assert _cifras("Subió 2.500 pesos, un 3,5 % y 12000 más") == {"2500", "3.5", "12000"}
    assert _cifras("El G20 y el COVID19 en w155") == set()
    assert _tiene_dato("Son 3 de cada 10.") and _tiene_dato("Quedó en la mitad.") and _tiene_dato("Un millón de pesos.")
    assert not _tiene_dato("Lo dijo el G20.") and not _tiene_dato("Veremos qué pasa.")


def test_vacio_y_basura():
    from hookline.servicios.noticias import chequear
    for t in ("", "   ", "\n\n", "...!!!", None):
        ch = chequear(t, [FUENTE])
        assert not ch["ok"] and ch["palabras"] == 0


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
          deadline=None)
@given(st.text(max_size=3000), st.lists(st.text(max_size=80), max_size=5), st.lists(st.text(max_size=80), max_size=5))
def test_fuzz_el_chequeo_nunca_explota(texto, fuentes, usados):
    from hookline.servicios.noticias import DEFECTO, chequear
    ch = chequear(texto, fuentes, usados, dict(DEFECTO))
    assert ch["ok"] == (not ch["problemas"])
    assert ch["palabras"] >= 0 and math.isfinite(ch["segundos"])


def test_configuracion_cambia_las_reglas_y_rechaza_lo_absurdo():
    from hookline.servicios import noticias as n
    c = n.guardar_conf({"min_s": 30, "max_s": 45, "cta_desde": 10, "cta_hasta": 20, "cta": "  Seguinos   ya  "})
    assert c["cta"] == "Seguinos ya" and c["min_s"] == 30
    assert n.chequear(guion_ok(100, 40).replace("Seguime y dale like.", "Seguinos ya w9999 w9998."), [FUENTE])["ok"]
    for malo in ({"min_s": 80, "max_s": 70}, {"pps": "nan"}, {"pps": float("inf")}, {"umbral": 11}, {"hora_radar": 24},
                 {"cta_desde": 50, "cta_hasta": 40}, {"cta_hasta": 999}, {"cta": "x"}, {"areas": []},
                 {"areas": "   "}, {"precios": {"../x": {}}}, {"precios": []}, {"presupuesto_usd": -1},
                 {"auto_guiones": "muchos"}, {"umbral": None}, {"areas": 5}):
        with pytest.raises(ValueError):
            n.guardar_conf(malo)
    assert n.conf()["min_s"] == 30                         # lo malo no pisa lo guardado
    n.guardar_conf({"areas": "Uno\nUno\n dos \n", "prohibidas": ["X", "x", ""]})
    assert n.conf()["areas"] == ["Uno", "dos"] and n.conf()["prohibidas"] == ["X"]


def test_ajuste_corrupto_no_rompe():
    from hookline import comun
    from hookline.servicios import noticias as n
    comun.guardar_ajuste("noticias_config", ["no", "es", "un", "dict"])
    assert n.conf()["min_s"] == 60
    comun.guardar_ajuste("noticias_config", {"min_s": 50, "inventado": 1})
    assert n.conf()["min_s"] == 50 and "inventado" not in n.conf()
    comun.guardar_ajuste("espacios_nombres", "basura")
    assert n.nombres_espacios() == {"modelos": "Modelos", "noticias": "Noticias"}


# --- radar ------------------------------------------------------------------------------------------------------------------
def test_radar_solo_para_cuentas_de_noticias(cuenta_noticias):
    from hookline.servicios import noticias
    with pytest.raises(ValueError, match="espacio Noticias"):
        noticias.pedir_radar("modelo1")
    with pytest.raises(ValueError, match="desconocida"):
        noticias.pedir_radar("nadie")


def test_radar_guarda_temas_sin_repetir_y_limpia_lo_que_viene(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item(), _item("Diputados aprobó el Presupuesto 2027 en general"),            # el mismo tema
            _item("Kicillof anunció un bono para estatales", area="provincias y GOBERNADORES", urgencia=99,
                  potencial=0, fuentes=["ftp://x", "https://www.lanacion.com.ar/a", "<script>"]),
            _item("   "), _item("Subió la nafta 3 %", area="Otra cosa", datos=["x" * 900] * 9)])
    d = noticias.listar(CID)
    ts = {n["titulo"]: n for n in d["noticias"]}
    assert set(ts) == {"Diputados aprobó el presupuesto 2027", "Kicillof anunció un bono para estatales",
                       "Subió la nafta 3 %"}
    k = ts["Kicillof anunció un bono para estatales"]
    assert k["area"] == "Provincias y gobernadores" and k["urgencia"] == 10 and k["potencial"] == 1
    assert k["fuentes"] == ["https://www.lanacion.com.ar/a"]
    assert len(ts["Subió la nafta 3 %"]["datos"]) == 5 and len(ts["Subió la nafta 3 %"]["datos"][0]) == 300
    assert d["noticias"][0]["puntaje"] >= d["noticias"][-1]["puntaje"]
    # el segundo radar no repite lo que ya está y recibe la lista de lo cubierto
    noticias.pedir_radar(CID)
    t = _tarea("radar", CID)[-1]
    assert "Diputados aprobó el presupuesto 2027" in t.entrada["datos"]["ya_cubiertos"]
    from hookline.ia import tareas
    tareas.completar(t.id, {"items": [_item("Diputados aprobó el presupuesto 2027 tras 20 horas")]})
    assert len(noticias.listar(CID)["noticias"]) == 3


def test_no_se_lanzan_dos_radares_a_la_vez(cuenta_noticias):
    from hookline.servicios import noticias
    errores, ok = [], []

    def uno():
        try:
            ok.append(noticias.pedir_radar(CID))
        except ValueError as ex:
            errores.append(str(ex))
    hs = [threading.Thread(target=uno) for _ in range(8)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert len(ok) == 1 and len(errores) == 7 and len(_tarea("radar", CID)) == 1


def test_radar_usa_las_reglas_de_noticias_y_busca_en_la_web(cuenta_noticias):
    import json
    from hookline.ia import prompts, tareas
    from hookline.servicios import noticias
    noticias.pedir_radar(CID)
    t = _tarea("radar", CID)[-1]
    assert t.entrada["web"] and not t.entrada["rapido"]
    pedido = json.loads((tareas.dir_tarea(t.id) / "pedido.json").read_text())
    assert pedido["sistema"] == prompts.SISTEMA_NOTICIAS and pedido["buscar_en_internet"]
    assert "Presunción de inocencia" in pedido["sistema"] and "modelos / creadoras" not in pedido["sistema"]
    assert "Bolsillo y economía" in pedido["instrucciones"]


def test_si_el_radar_falla_avisa(cuenta_noticias):
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.servicios import noticias
    noticias.pedir_radar(CID)
    t = _tarea("radar", CID)[-1]
    tareas.fallar(t.id, "sin saldo")
    assert not _tarea("radar", CID)
    noticias.pedir_radar(CID)                                 # se puede volver a pedir
    with sesion() as s:
        from sqlalchemy import select
        from hookline.modelos import MensajeChat
        txt = " ".join(m.texto for m in s.scalars(select(MensajeChat)))
    assert "No pude correr el radar de @loqueborraron: sin saldo" in txt


# --- guion y jurado -----------------------------------------------------------------------------------------------------------
def _nid():
    from hookline.servicios import noticias
    return noticias.listar(CID)["noticias"][0]["id"]


def test_flujo_completo_radar_guion_jurado_listo(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item()])
    nid = _nid()
    g = _guion_y_jurado(nid)
    assert g["estado"] == "listo" and g["nota"] == 8.0 and g["jurado"]["retencion"] == 100
    assert g["hashtags"] == ["#presupuesto", "#Congreso", "#conespacio"] and g["fuentes"] == [FUENTE]
    assert g["chequeo"]["ok"] and len(g["jurado"]["personas"]) == 10 and len(g["jurado"]["mejoras"]) == 3
    assert noticias.listar(CID)["noticias"][0]["estado"] == "elegida"
    assert g["costo"]["guion"] > 0 and g["costo"]["jurado"] > 0 and g["costo"]["radar"] > 0
    assert g["costo"]["total"] == pytest.approx(g["costo"]["radar"] + g["costo"]["guion"] + g["costo"]["jurado"], abs=1e-3)
    # el guion recibe las reglas exactas
    t = noticias.listar(CID)
    assert t["guiones"][0]["id"] == g["id"]
    g2 = noticias.accion_guion({"op": "aprobar", "id": g["id"]})
    assert g2["estado"] == "aprobado"
    g3 = noticias.accion_guion({"op": "editar", "id": g["id"], "texto": guion_ok(165)})
    assert g3["estado"] == "listo"                               # cambió el texto: hay que volver a aprobar


def test_el_pedido_del_guion_trae_las_reglas_en_palabras(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item()])
    g = noticias.pedir_guion(_nid())
    t = _tarea("guion", g["id"])[-1]
    ins = t.entrada["instrucciones"]
    assert "150 a 187 palabras" in ins and "entre la palabra 83 y la 107" in ins and "Seguime y dale like" in ins
    with pytest.raises(ValueError, match="Ya se está escribiendo"):
        noticias.pedir_guion(_nid())


def test_jurado_bajo_descarta(cuenta_noticias):
    _radar([_item()])
    g = _guion_y_jurado(_nid(), notas=[5, 5, 6, 5, 4, 6, 5, 5, 6, 5])
    assert g["estado"] == "descartado" and g["nota"] == 5.2 and "menos de 5,5" in g["error"]
    assert g["jurado"]["retencion"] < 100


def test_las_mejoras_se_aplican_solo_si_no_empeoran_ni_inventan(cuenta_noticias):
    _radar([_item(), _item("Gobernadores piden fondos", area="Provincias y gobernadores"),
            _item("Récord de la soja 2026", area="Bolsillo y economía")])
    from hookline.servicios import noticias
    ids = [n["id"] for n in noticias.listar(CID)["noticias"]]
    mejor = guion_ok(172)
    g = _guion_y_jurado(ids[0], mejorado=mejor)
    assert g["jurado"]["aplicado"] and g["texto"] == mejor and g["original"]
    peor = guion_ok(120)                                          # demasiado corto: no se aplica
    g = _guion_y_jurado(ids[1], mejorado=peor)
    assert not g["jurado"]["aplicado"] and g["texto"] != peor
    inventa = guion_ok(172).replace("w50", "9.999.999")          # un número que no estaba: no se aplica
    g = _guion_y_jurado(ids[2], mejorado=inventa)
    assert not g["jurado"]["aplicado"]


def test_jurado_con_respuesta_rara(cuenta_noticias):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    _radar([_item()])
    g = noticias.pedir_guion(_nid())
    tareas.completar(_tarea("guion", g["id"])[-1].id, {"titulo": "t", "texto": guion_ok(), "caption": "",
                                                       "hashtags": [], "fuentes": [], "datos": []})
    assert noticias.obtener(g["id"])["fuentes"] == [FUENTE]          # sin fuentes: hereda las del tema
    tj = _tarea("jurado", g["id"])[-1]
    tareas.completar(tj.id, {"personas": [{"nombre": "Sofi", "nota": 99, "se_iria": None, "comentario": "x"},
                                          {"nombre": "Sofi", "nota": 1, "se_iria": -5, "comentario": "dup"},
                                          {"nombre": "", "nota": 5, "comentario": ""},
                                          {"nombre": "Intruso", "nota": "7", "se_iria": 1e9, "comentario": "y"}],
                             "mejoras": [], "texto_mejorado": "", "resumen": ""})
    j = noticias.obtener(g["id"])["jurado"]
    assert [p["nombre"] for p in j["personas"]] == ["Sofi", "Intruso"]
    assert j["personas"][0]["nota"] == 10 and j["personas"][0]["se_iria"] is None
    assert j["personas"][1]["se_iria"] == 68.0 and not j["personas"][1]["del_jurado"]
    tj2 = _tarea("jurado", g["id"])
    assert not tj2


def test_respuesta_que_no_cumple_el_esquema_no_rompe_nada(cuenta_noticias):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    noticias.pedir_radar(CID)
    t = _tarea("radar", CID)[-1]
    tareas.completar(t.id, {"items": [_item(potencial="alto")]})
    assert not noticias.listar(CID)["noticias"] and not _tarea("radar", CID)        # quedó en error, sin basura
    _radar([_item()])
    g = noticias.pedir_guion(_nid())
    tareas.completar(_tarea("guion", g["id"])[-1].id, {"texto": 5})
    assert noticias.obtener(g["id"])["estado"] == "error"


def test_jurado_vacio_queda_en_error_y_se_puede_reescribir(cuenta_noticias):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    _radar([_item()])
    g = noticias.pedir_guion(_nid())
    tareas.completar(_tarea("guion", g["id"])[-1].id, {"titulo": "t", "texto": guion_ok(), "caption": "",
                                                       "hashtags": [], "fuentes": [FUENTE], "datos": []})
    tareas.completar(_tarea("jurado", g["id"])[-1].id, {"personas": [], "mejoras": [], "texto_mejorado": "",
                                                        "resumen": ""})
    assert noticias.obtener(g["id"])["estado"] == "error"
    g = noticias.reescribir(g["id"])
    assert g["estado"] == "escribiendo" and g["version"] == 2
    t = _tarea("guion", g["id"])[-1]
    assert "VERSIÓN ANTERIOR" in t.entrada["instrucciones"]
    with pytest.raises(ValueError, match="trabajando"):
        noticias.reescribir(g["id"])
    with pytest.raises(ValueError, match="trabajando"):
        noticias.accion_guion({"op": "editar", "id": g["id"], "texto": "x"})


def test_fallo_de_claude_deja_el_guion_en_error(cuenta_noticias):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    _radar([_item()])
    g = noticias.pedir_guion(_nid())
    tareas.fallar(_tarea("guion", g["id"])[-1].id, "Claude no quiso")
    g = noticias.obtener(g["id"])
    assert g["estado"] == "error" and "no quiso" in g["error"]


def test_acciones_validan_todo(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item()])
    g = _guion_y_jurado(_nid(), texto=guion_ok(120))                  # el chequeo falla
    with pytest.raises(ValueError, match="chequeo"):
        noticias.accion_guion({"op": "aprobar", "id": g["id"]})
    for malo in ({"op": "aprobar"}, {"op": "aprobar", "id": "abc"}, {"op": "aprobar", "id": True},
                 {"op": "aprobar", "id": 2 ** 40}, {"op": "aprobar", "id": -1}, {"op": "aprobar", "id": 1.5},
                 {"op": "aprobar", "id": 99999}, {"op": "volar", "id": g["id"]},
                 {"op": "editar", "id": g["id"], "texto": "   "}):
        with pytest.raises(ValueError):
            noticias.accion_guion(malo)
    g = noticias.accion_guion({"op": "editar", "id": g["id"], "texto": guion_ok(), "fuentes": [FUENTE, 3, None]})
    assert g["chequeo"]["ok"] and g["fuentes"] == [FUENTE]
    noticias.accion_guion({"op": "aprobar", "id": g["id"]})
    noticias.accion_guion({"op": "descartar", "id": g["id"]})
    assert noticias.obtener(g["id"])["estado"] == "descartado"
    noticias.accion_guion({"op": "jurado", "id": g["id"]})
    assert noticias.obtener(g["id"])["estado"] == "jurado" and len(_tarea("jurado", g["id"])) == 1
    with pytest.raises(ValueError):
        noticias.accion_guion({"op": "jurado", "id": g["id"]})          # ya está con el jurado


def test_dos_clics_en_escribir_guion_hacen_uno_solo(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item()])
    nid = _nid()
    ok, mal = [], []

    def uno():
        try:
            ok.append(noticias.pedir_guion(nid))
        except ValueError:
            mal.append(1)
    hs = [threading.Thread(target=uno) for _ in range(6)]
    [h.start() for h in hs]
    [h.join() for h in hs]
    assert len(ok) == 1 and len(mal) == 5


def test_datos_ya_usados_viajan_al_siguiente_guion(cuenta_noticias):
    from hookline.servicios import noticias
    _radar([_item(), _item("Paro de colectivos en el AMBA", area="Bolsillo y economía")])
    ids = [n["id"] for n in noticias.listar(CID)["noticias"]]
    _guion_y_jurado(ids[0])
    g = noticias.pedir_guion(ids[1])
    t = _tarea("guion", g["id"])[-1]
    assert "129 votos" in t.entrada["datos"]["datos_usados"]


# --- costos -------------------------------------------------------------------------------------------------------------------
def test_costo_de_un_pedido():
    from hookline.servicios.noticias import costo
    # 10k entrada × 4 + 1k escrita × 4 × 1,25 + 5k cache × 0,2 + 2k salida × 20, por millón, + 6 búsquedas × 0,01
    assert costo(USO_OPUS) == pytest.approx((40000 + 5000 + 1000 + 40000) / 1e6 + 0.06)
    assert costo({"modelo": "claude-haiku-5-5", "entrada": 1_000_000, "salida": 1_000_000}) == pytest.approx(0.6)
    assert costo({"modelo": "claude-opus-5-5-20990101", "entrada": 1_000_000}) == pytest.approx(4.0)
    assert costo(None) == 0 and costo({}) == 0
    assert costo({"modelo": "raro", "entrada": "x", "salida": -5, "busquedas": None}) == 0


def test_precios_propios_pisan_los_de_fabrica():
    from hookline.servicios import noticias
    noticias.guardar_conf({"precios": {"claude-opus-5-5": {"entrada": 1, "salida": 1, "cache": 0}},
                           "precio_busqueda": 0})
    assert noticias.costo({"modelo": "claude-opus-5-5", "entrada": 1_000_000, "busquedas": 9}) == pytest.approx(1.0)


def test_pagina_de_costos(cuenta_noticias):
    from hookline.db import sesion
    from hookline.modelos import TareaIA
    from hookline.servicios import noticias
    _radar([_item(), _item("Gobernadores piden fondos", area="Provincias y gobernadores")])
    g = _guion_y_jurado(_nid())
    with sesion() as s:                          # un pedido hecho con Claude Code: sin uso
        s.add(TareaIA(id="chat-x", tipo="chat", estado="hecha", motor="claude_code"))
    c = noticias.costos(30)
    etapas = {e["etapa"]: e for e in c["etapas"]}
    assert set(etapas) >= {"radar", "guion", "jurado", "chat"} and etapas["radar"]["busquedas"] == 6
    assert c["sin_medir"] == 1 and "Claude Code" in c["nota"]
    assert c["total_usd"] == pytest.approx(sum(e["usd"] for e in c["etapas"]), abs=1e-3)
    r = next(x for x in c["reels"] if x["id"] == g["id"])
    assert r["radar"] == pytest.approx(noticias.costo(USO_OPUS) / 2, abs=1e-4)      # el radar se reparte en 2 temas
    assert c["promedio_reel_usd"] == pytest.approx(r["total"])
    assert c["mes_usd"] > 0 and not c["alerta"]
    noticias.guardar_conf({"presupuesto_usd": 0.1})
    assert "%" in noticias.costos()["alerta"] or "Se pasó" in noticias.costos()["alerta"]
    noticias.guardar_conf({"presupuesto_usd": 0.01})
    assert noticias.presupuesto_agotado() and "frenado" in noticias.costos()["alerta"]
    assert noticias.costos(0)["dias"] == 1 and noticias.costos(10 ** 9)["dias"] == 366


def test_el_motor_api_suma_todas_las_vueltas_de_la_busqueda():
    from types import SimpleNamespace as N
    from hookline.ia import motor_api

    def resp(stop, e, s, b):
        return N(stop_reason=stop, model="claude-opus-5-5", content=[],
                 usage=N(input_tokens=e, output_tokens=s, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                         server_tool_use=N(web_search_requests=b)))
    rs = iter([resp("pause_turn", 100, 10, 3), resp("pause_turn", 200, 20, 2), resp("end_turn", 300, 30, 1)])
    r, u = motor_api._bucle_pausa(lambda m: next(rs), [{"role": "user", "content": "x"}])
    assert r.stop_reason == "end_turn"
    assert u == {"modelo": "claude-opus-5-5", "entrada": 600, "salida": 60, "cache": 0, "cache_escrita": 0,
                 "busquedas": 6}


# --- automático ----------------------------------------------------------------------------------------------------------------
def test_radar_diario_una_vez_por_dia_y_despues_de_la_hora(cuenta_noticias, monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from hookline.db import sesion
    from hookline.modelos import Trabajo
    from hookline.servicios import noticias
    noticias.guardar_conf({"hora_radar": 8})
    monkeypatch.setattr(noticias, "ahora", lambda: datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc))   # 7 h local
    noticias.periodicas()
    with sesion() as s:
        assert not s.scalars(select(Trabajo).where(Trabajo.tipo == "radar_diario")).all()
    monkeypatch.setattr(noticias, "ahora", lambda: datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc))   # 9 h local
    for _ in range(3):
        noticias.periodicas()
    with sesion() as s:
        ts = s.scalars(select(Trabajo).where(Trabajo.tipo == "radar_diario")).all()
        assert [t.datos["cuenta"] for t in ts] == [CID]


def test_radar_diario_no_corre_sin_claude_ni_sin_presupuesto_y_escribe_el_mejor(cuenta_noticias, monkeypatch):
    from hookline.ia import tareas
    from hookline.servicios import noticias
    monkeypatch.setattr(tareas, "estado_puente", lambda: {"puente": "apagado"})
    noticias._radar_diario({"cuenta": CID})
    assert not _tarea("radar", CID)
    monkeypatch.setattr(tareas, "estado_puente", lambda: {"puente": "escuchando"})
    noticias._radar_diario({"cuenta": CID})
    t = _tarea("radar", CID)[-1]
    assert t.entrada["datos"]["auto"]
    tareas.completar(t.id, {"items": [_item(urgencia=3, potencial=3),
                                      _item("Gobernadores piden fondos", area="Provincias y gobernadores")]}, USO_OPUS)
    gs = _tarea("guion")
    assert len(gs) == 1
    from hookline.servicios.noticias import listar
    mejor = max(listar(CID)["noticias"], key=lambda n: n["puntaje"])
    assert mejor["estado"] == "elegida"
    noticias.guardar_conf({"presupuesto_usd": 0.0001})
    noticias._radar_diario({"cuenta": CID})
    assert not _tarea("radar", CID)


# --- espacios, cuentas y API ------------------------------------------------------------------------------------------------------
def test_espacios_y_cuentas(cuenta_noticias):
    from hookline.servicios import cuentas, noticias
    assert cuentas.crear_local("lqb.dos", espacio="noticias") == "lqb.dos"
    assert cuentas.crear_local("lqb.multi", main="lqb.dos") == "lqb.multi"
    lista = {c["id"]: c["espacio"] for c in cuentas.listar()}
    assert lista["lqb.multi"] == "noticias" and lista["modelo1"] == "modelos"
    with pytest.raises(ValueError):
        cuentas.crear_local("x1", espacio="otro")
    with pytest.raises(ValueError):
        cuentas.guardar("modelo1", {"espacio": "<script>"})
    cuentas.guardar("modelo1", {"espacio": "noticias"})
    assert set(noticias.cuentas_noticias()) == {CID, "lqb.dos", "lqb.multi", "modelo1"}
    assert noticias.guardar_nombres({"modelos": "  Creadoras  ", "noticias": ""}) == {"modelos": "Creadoras",
                                                                                       "noticias": "Noticias"}
    with pytest.raises(ValueError):
        noticias.guardar_nombres({"modelos": "noticias"})
    with pytest.raises(ValueError):
        noticias.guardar_nombres("x")


def test_api_noticias(cliente_api, cuenta_noticias):
    c = cliente_api
    assert c.get("/api/estado").json()["espacios"] == {"modelos": "Modelos", "noticias": "Noticias"}
    assert c.post("/api/espacios", json={"nombres": {"noticias": "Lo Que Borraron"}}).json()["espacios"]["noticias"] == \
        "Lo Que Borraron"
    assert c.get("/api/noticias?cuenta=nadie").status_code == 404
    assert c.get("/api/noticias?cuenta=modelo1").status_code == 400
    r = c.post("/api/noticias", json={"op": "radar", "cuenta": CID})
    assert r.status_code == 200 and r.json()["tarea"].startswith("radar-")
    assert c.post("/api/noticias", json={"op": "radar", "cuenta": CID}).status_code == 400
    from hookline.ia import tareas
    tareas.completar(_tarea("radar", CID)[-1].id, {"items": [_item()]}, USO_OPUS)
    d = c.get(f"/api/noticias?cuenta={CID}").json()
    nid = d["noticias"][0]["id"]
    for malo in ({"op": "guion"}, {"op": "guion", "noticia": "x"}, {"op": "guion", "noticia": 10 ** 20},
                 {"op": "guion", "noticia": [1]}, {"op": "volar"}, {"op": "editar", "id": None}):
        assert c.post("/api/noticias", json=malo).status_code == 400, malo
    g = c.post("/api/noticias", json={"op": "guion", "noticia": nid}).json()
    assert g["estado"] == "escribiendo"
    r = c.post("/api/noticias/chequear", json={"texto": guion_ok(), "fuentes": [FUENTE]})
    assert r.json()["ok"]
    assert c.post("/api/noticias/chequear", json={"texto": {"a": 1}, "fuentes": "x"}).status_code == 200
    assert c.post("/api/noticias", json={"op": "config", "config": {"umbral": 6}}).json()["config"]["umbral"] == 6
    assert c.post("/api/noticias", json={"op": "config", "config": {"umbral": "mucho"}}).status_code == 400
    co = c.get("/api/costos?dias=7").json()
    assert co["dias"] == 7 and co["config"]["umbral"] == 6
    assert c.get("/api/costos?dias=abc").status_code == 422
    assert c.post("/api/noticias", json={"op": "descartar_tema", "noticia": nid}).json()["ok"]
    assert c.post("/api/cuenta", json={"op": "crear_local", "id": "nueva.nt", "espacio": "noticias"}).status_code == 200


def test_api_permisos(cliente_api, cuenta_noticias):
    from fastapi.testclient import TestClient
    from hookline import api
    from hookline.auth import crear_usuario
    for rol in ("editor", "lector"):
        crear_usuario(f"{rol}@hookline.test", "una-clave-larga", rol, rol)
    clientes = {}
    for rol in ("editor", "lector"):
        x = TestClient(api.app)
        r = x.post("/api/login", json={"email": f"{rol}@hookline.test", "clave": "una-clave-larga"})
        x.headers["X-CSRF-Token"] = r.json()["csrf"]
        clientes[rol] = x
    ed, le = clientes["editor"], clientes["lector"]
    assert le.get(f"/api/noticias?cuenta={CID}").status_code == 200
    assert le.post("/api/noticias", json={"op": "radar", "cuenta": CID}).status_code == 403
    assert le.get("/api/costos").status_code == 403
    assert ed.get("/api/costos").status_code == 200
    assert ed.post("/api/noticias", json={"op": "config", "config": {"umbral": 2}}).status_code == 403
    assert ed.post("/api/espacios", json={"nombres": {"modelos": "X"}}).status_code == 403
    assert ed.post("/api/noticias", json={"op": "radar", "cuenta": CID}).status_code == 200
    sin = TestClient(api.app)
    assert sin.get(f"/api/noticias?cuenta={CID}").status_code == 401
    assert sin.get("/api/costos").status_code == 401


def test_migracion_0004_sobre_una_base_vieja(tmp_path, monkeypatch):
    """Las cuentas que ya existían quedan en 'modelos'."""
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    bd = tmp_path / "vieja.db"
    url = f"sqlite:///{bd}"
    monkeypatch.setenv("HOOKLINE_BASE_DATOS", url)
    from hookline import config, db
    config.recargar()
    db.reiniciar_motor()
    ini = Path(__file__).resolve().parents[1] / "hookline" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migraciones"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0003")
    con = sqlite3.connect(bd)
    con.execute("insert into cuentas (id, nombre, nicho, tono, apodo, activa_desde, creada, ig_user_id, usuario_ig, "
                "tipo_cuenta, foto, error, aviso, pyro_modelo) values ('vieja','','','','','','2026-01-01','','','','',"
                "'','','')")
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(bd)
    assert con.execute("select espacio from cuentas where id='vieja'").fetchone() == ("modelos",)
    assert {r[0] for r in con.execute("select name from sqlite_master where type='table'")} >= {"noticias", "guiones"}
    con.close()
    command.downgrade(cfg, "0003")
    command.upgrade(cfg, "head")
