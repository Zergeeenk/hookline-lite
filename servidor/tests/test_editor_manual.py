"""Feedback de Rubén (2026-11-19), probado con videos reales (ffmpeg):
- el texto puesto a mano salía en otro lugar (la vista previa dibujaba la letra 20 % más chica que el video);
- la nota de Claude salía dibujada en el video como una "notificación";
- borde, sombra, caja y color no se podían elegir (se forzaba letra blanca sin nada);
- mover y achicar capturas, chat, notificación y stickers (emojis de iPhone en PNG);
- el buscador decía «agregá fuentes en Memes → Fuentes» y esa pantalla no existía;
- el chat con Claude en el menú, con un aviso por cada video listo."""
import io
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from hookline import medios

PANEL = Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html"
con_ffmpeg = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")


# --- helpers ------------------------------------------------------------------------------------------------------------
def _vaciar():
    from hookline import trabajos
    for _ in range(60):
        t = trabajos.tomar("test", list(trabajos.MANEJADORES))
        if not t:
            return
        trabajos.correr(t)


def _png(color=(255, 0, 255, 255), lado=200) -> bytes:
    im = Image.new("RGBA", (lado + 40, lado + 40), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((20, 20, lado + 20, lado + 20), fill=color)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def copia(tmp_path):
    """Una copia lista: modelo oscura y lisa, ejemplo con un recuadro blanco (la "captura") en el medio."""
    from hookline import almacen
    from hookline.db import sesion
    from hookline.ia import tareas
    from hookline.modelos import Cuenta
    from hookline.servicios import fabrica
    with sesion() as s:
        s.add(Cuenta(id="modelo1", activa_desde="2026-01-01"))
    Image.new("RGB", (720, 1280), (30, 30, 40)).save(almacen.dir_de("material", "modelo1", "modelo") / "selfie.jpg")
    ej = tmp_path / "ejemplo.mp4"
    subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=0x223344:size=720x1280:rate=30:duration=4", "-f", "lavfi", "-i", "sine=duration=4",
                    "-vf", "drawbox=x=100:y=500:w=520:h=200:color=white:t=fill", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(ej)], check=True)
    cid = fabrica.desde_archivo("ejemplo.mp4", ej.read_bytes(), "modelo1")
    _vaciar()
    t = next(t for t in tareas.pendientes_claude_code() if t.tipo == "lectura")
    (tareas.dir_tarea(t.id) / "resultado.json").write_text(json.dumps({
        "corte": 2.0, "texto": "Cuando me dice que ya llega", "texto_y": 0.3, "texto_tam": 0.042, "modelo": 1,
        "notas": "ok", "pegar": [{"x0": 0.139, "y0": 0.39, "x1": 0.861, "y1": 0.547, "cuadro": 1.0}]}))
    tareas.recoger_claude_code()
    _vaciar()
    return cid


def _cuadro(cid, t=1.0) -> np.ndarray:
    from hookline.servicios import fabrica
    out = Path(fabrica.resultado(cid)).with_suffix(".png")
    subprocess.run([medios.ffmpeg(), "-y", "-v", "error", "-ss", str(t), "-i", str(fabrica.resultado(cid)),
                    "-frames:v", "1", str(out)], check=True)
    return np.asarray(Image.open(out).convert("RGB")).astype(int)


def _caja(m: np.ndarray):
    ys, xs = np.where(m)
    if not len(xs):
        return None
    H, W = m.shape
    return xs.min() / W, ys.min() / H, (xs.max() + 1) / W, (ys.max() + 1) / H


def _armar(cid, **campos):
    from hookline.servicios import fabrica
    fabrica.armar(cid, campos)
    _vaciar()
    c = next(x for x in fabrica.listar() if x["id"] == cid)
    assert c["estado"] == "lista" and not c.get("error"), c.get("error")
    return c


# --- 1. la nota de Claude no sale dibujada como notificación --------------------------------------------------------------
NOTA = "Sin riesgo para Instagram. La foto del ejemplo tiene un logo chiquito en el centro, pero no va en la versión nueva."


def _lectura(**kw):
    base = {"descartar": "", "corte": 2.0, "texto": "hola", "texto_y": 0.2, "texto_tam": 0.04, "modelo": 1,
            "notas": NOTA, "marca_agua": False, "chat": {"hay": False, "mensajes": [], "x0": 0, "x1": 0, "y0": 0},
            "pegar": [], "noti": {"hay": False, "texto": "", "titulo": "", "x0": -1, "x1": -1, "y0": -1},
            "textos": [], "recorte_remate": {"x0": 0, "y0": 0, "x1": 0, "y1": 0}, "fuente": ""}
    base.update(kw)
    return base


@pytest.mark.parametrize("noti,sale", [
    ({"hay": False, "texto": NOTA, "titulo": "", "x0": -1, "x1": -1, "y0": -1}, False),      # lo que pasó en producción
    ({"hay": True, "texto": NOTA, "titulo": "", "x0": -1, "x1": -1, "y0": -1}, False),       # "hay" pero es la nota
    ({"hay": True, "texto": NOTA[:60], "titulo": "", "x0": -1, "x1": -1, "y0": -1}, False),  # un pedazo de la nota
    ({"hay": False, "texto": "sofi te empezó a seguir", "titulo": "", "x0": -1, "x1": -1, "y0": -1}, False),
    ({"hay": True, "texto": "sofi te empezó a seguir", "titulo": "", "x0": -1, "x1": -1, "y0": -1}, True),
])
def test_la_notificacion_sale_solo_si_hay_una_de_verdad(noti, sale):
    from hookline.ia.esquemas import LecturaAPI, para_api
    r = para_api("lectura")[1](LecturaAPI.model_validate(_lectura(noti=noti)).model_dump())
    assert ("noti" in r) is sale


def test_el_chat_sale_solo_si_hay():
    from hookline.ia.esquemas import LecturaAPI, para_api
    ms = [{"de": "otro", "texto": "hola", "hora": ""}]
    conv = para_api("lectura")[1]
    assert "chat" not in conv(LecturaAPI.model_validate(_lectura(chat={"hay": False, "mensajes": ms, "x0": 0.1,
                                                                        "x1": 0.9, "y0": 0.3})).model_dump())
    assert conv(LecturaAPI.model_validate(_lectura(chat={"hay": True, "mensajes": ms, "x0": 0.1, "x1": 0.9,
                                                         "y0": 0.3})).model_dump())["chat"]["mensajes"] == ms


def test_el_esquema_pide_hay_y_avisa_que_no_van_las_notas():
    from anthropic.lib._parse._transform import transform_schema
    from hookline.ia.esquemas import LecturaAPI
    sch = transform_schema(LecturaAPI)
    for d in ("_ChatAPI", "_NotiAPI"):
        assert "hay" in sch["$defs"][d]["properties"] and "hay" in sch["$defs"][d]["required"], d
    assert "Nunca tus notas" in json.dumps(sch, ensure_ascii=False)


# --- 2. vista previa = video ----------------------------------------------------------------------------------------------
def test_la_vista_previa_usa_la_escala_del_video():
    from hookline.servicios import fabrica
    assert fabrica.opciones()["escala_texto"] == fabrica.ESCALA_TEXTO == 1.2
    html = PANEL.read_text(encoding="utf-8")
    assert "dr.texto_tam * H * (o.escala_texto || 1)" in html
    # tocar el tamaño (esquina, botones A−/A+, barra) deja el texto donde está: no lo vuelve a ubicar solo
    assert "if (!cpArr.esquina) cpManual();" not in html
    assert re.search(r"d\.cptam\) \{ const c = cpCopia\(\); if \(c\) \{ cpManual\(\);", html)
    assert 'if (e.target.id === "cpTam") { cpManual();' in html
    campos = re.search(r"const CP_CAMPOS = (\[.*?\]);", html).group(1)
    for k in ("pegar", "chat", "noti", "stickers", "estilo", "color"):
        assert f'"{k}"' in campos, k


@con_ffmpeg
def test_texto_a_mano_queda_donde_lo_ponen(copia):
    """Con ubicación a mano el armado no lo mueve ni lo achica; el texto amarillo aparece a esa altura."""
    c = _armar(copia, texto="Hola", texto_y=0.62, texto_x=0.5, texto_tam=0.05, ubicacion_auto=False,
               estilo="contorno", color="amarillo")
    assert c["texto_y"] == 0.62 and c["texto_tam"] == 0.05
    a = _cuadro(copia)
    caja = _caja((a[..., 0] > 200) & (a[..., 1] > 170) & (a[..., 2] < 140))
    assert caja and 0.6 <= caja[1] <= 0.66 and abs((caja[0] + caja[2]) / 2 - 0.5) < 0.03, caja


# --- 3. estilo y color ------------------------------------------------------------------------------------------------------
@con_ffmpeg
def test_estilo_y_color_se_guardan_y_salen_en_el_video(copia):
    from hookline.servicios import fabrica
    c = _armar(copia, estilo="caja", color="amarillo", ubicacion_auto=False, texto_y=0.5)
    assert (c["estilo"], c["color"]) == ("caja", "amarillo")
    assert fabrica._datos(copia)["estilo"] == "caja"                     # antes se pisaba con "nada"/"blanco"
    a = _cuadro(copia)
    negro = _caja((a[..., 0] < 15) & (a[..., 1] < 15) & (a[..., 2] < 15))   # la caja negra detrás de la letra
    assert negro and 0.47 <= negro[1] <= 0.53, negro
    for malo in ({"estilo": "neon"}, {"color": "fucsia-chillon"}, {"estilo": []}, {"color": {"a": 1}}):
        fabrica.armar(copia, malo)
        _vaciar()
        assert fabrica._datos(copia)["estilo"] == "caja" and fabrica._datos(copia)["color"] == "amarillo"


# --- 4. capturas, stickers: lugar y tamaño ------------------------------------------------------------------------------------
@con_ffmpeg
def test_la_captura_se_pega_donde_y_del_tamano_que_la_dejan(copia):
    from hookline.servicios import fabrica
    p = dict(fabrica._datos(copia)["pegar"][0])
    _armar(copia, pegar=[{**p, "a_x0": 0.2, "a_y0": 0.7, "a_ancho": 0.4}], texto="", ubicacion_auto=False)
    a = _cuadro(copia)
    caja = _caja((a[..., 0] > 235) & (a[..., 1] > 235) & (a[..., 2] > 235))
    assert caja and abs(caja[0] - 0.2) < 0.012 and abs(caja[1] - 0.7) < 0.012 and abs(caja[2] - 0.6) < 0.015, caja


@con_ffmpeg
def test_stickers_se_suben_se_pegan_y_se_validan(copia):
    from hookline.servicios import fabrica
    st = fabrica.subir_sticker("corazón 😍 de iPhone!!.png", _png())
    assert re.fullmatch(r"[0-9A-Za-z_-]+\.png", st["archivo"]) and st["archivo"].startswith("coraz")
    im = Image.open(fabrica._dir_stickers() / st["archivo"])
    assert im.mode == "RGBA" and im.size == (201, 201)                 # recortado: sin el borde transparente
    otro = fabrica.subir_sticker("corazón 😍 de iPhone!!.png", _png())
    assert otro["archivo"] != st["archivo"]                            # no pisa al anterior
    for malo in (b"", b"no soy una imagen", b"\x89PNG\r\n\x1a\nroto"):
        with pytest.raises(ValueError):
            fabrica.subir_sticker("x.png", malo)
    with pytest.raises(ValueError):
        fabrica.subir_sticker("x.png", b"0" * (9 * 1024 * 1024))
    c = _armar(copia, texto="", ubicacion_auto=False, pegar=[],
               stickers=[{"archivo": st["archivo"], "x0": 0.1, "y0": 0.15, "ancho": 0.3},
                         {"archivo": "../../etc/passwd", "x0": 0, "y0": 0, "ancho": 1},     # no está en la lista
                         {"archivo": "inventado.png"}])
    assert [s["archivo"] for s in c["stickers"]] == [st["archivo"]]
    a = _cuadro(copia)
    caja = _caja((a[..., 0] > 200) & (a[..., 1] < 90) & (a[..., 2] > 200))
    assert caja and abs(caja[0] - 0.1) < 0.01 and abs(caja[1] - 0.15) < 0.01 and abs(caja[2] - 0.4) < 0.012, caja
    assert len(fabrica.stickers()) == 2
    fabrica.borrar_sticker(otro["archivo"])
    fabrica.borrar_sticker("../../../datos.db")                         # no sale de la carpeta
    assert [x["archivo"] for x in fabrica.stickers()] == [st["archivo"]]


@con_ffmpeg
def test_capas_del_editor(copia, cliente_api):
    from hookline.servicios import fabrica
    p = fabrica.capa(copia, "pegar", 0)
    assert Image.open(p).size == (520, 201) or abs(Image.open(p).size[0] - 520) <= 2
    assert np.asarray(Image.open(p).convert("L")).mean() > 230         # es el recuadro blanco del ejemplo
    fabrica._datos(copia, chat={"mensajes": [{"de": "otro", "texto": "¿ya llegás?", "hora": "21:04"}],
                                "x0": 0.2, "x1": 0.8, "y0": 0.5},
                   noti={"texto": "sofi te empezó a seguir", "titulo": "Instagram", "x0": 0.1, "x1": 0.9, "y0": 0.2})
    for que in ("chat", "noti"):
        r = cliente_api.get(f"/api/copia_capa?id={copia}&que={que}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    for mal in (f"id={copia}&que=pegar&k=5", f"id={copia}&que=otra", "id=nada&que=chat", f"id={copia}&que=pegar&k=x"):
        assert cliente_api.get(f"/api/copia_capa?{mal}").status_code in (400, 422), mal
    _armar(copia, texto="", ubicacion_auto=False)                       # el chat y la noti se arman sin error


def test_api_stickers(cliente_api):
    r = cliente_api.post("/api/sticker_subir?nombre=risa.png", content=_png((255, 200, 0, 255)),
                         headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    a = r.json()["sticker"]["archivo"]
    op = cliente_api.get("/api/copias").json()["opciones"]["stickers"]
    assert [x["archivo"] for x in op] == [a]
    assert cliente_api.get("/media?p=" + op[0]["url"]).status_code == 200
    assert cliente_api.post("/api/sticker_subir?nombre=x.png", content=b"hola",
                            headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert cliente_api.post("/api/copiar", json={"op": "borrar_sticker", "archivo": a}).json()["ok"]
    assert cliente_api.get("/api/copias").json()["opciones"]["stickers"] == []


@con_ffmpeg
def test_cada_video_listo_avisa_en_el_chat(copia):
    from sqlalchemy import select
    from hookline.db import sesion
    from hookline.modelos import MensajeChat
    _armar(copia, texto="Cuando me dice que ya llega")
    with sesion() as s:
        ts = [m.texto for m in s.scalars(select(MensajeChat))]
    assert any("Video listo" in t and "Cuando me dice que ya llega" in t and "@modelo1" in t for t in ts), ts


def test_el_chat_esta_en_el_menu():
    html = PANEL.read_text(encoding="utf-8")
    assert "data-abrirchat" in html and "Chat con Claude" in html and "d.abrirchat !== undefined" in html


# --- 5. buscador: dónde busca -----------------------------------------------------------------------------------------------
def test_buscador_sin_fuentes_explica_y_se_siembra_del_banco():
    from hookline.servicios import buscador, fabrica
    with pytest.raises(ValueError, match="Dónde busca"):
        buscador.buscar()
    fabrica.guardar_links("https://www.tiktok.com/@memes.ar/video/123\nhttps://www.instagram.com/reel/ABC/")
    from hookline.db import sesion
    from hookline.modelos import LinkBanco
    with sesion() as s:
        s.get(LinkBanco, "https://www.instagram.com/reel/ABC/").datos = {"ficha": "ok", "usuario": "cuenta.memes"}
    buscador.buscar()
    assert {f["texto"] for f in buscador.fuentes()} == {"tt @memes.ar", "ig @cuenta.memes"}


def test_api_fuentes_de_busqueda(cliente_api):
    r = cliente_api.post("/api/copiar", json={"op": "sumar_fuentes",
                                              "texto": "ig @memes_ar\ntt @otra\nig: pov soltera\nig @memes_ar\n\n"})
    assert r.json()["n"] == 3 and [f["texto"] for f in r.json()["fuentes"]] == ["ig @memes_ar", "tt @otra",
                                                                               "ig: pov soltera"]
    assert cliente_api.post("/api/copiar", json={"op": "sumar_fuentes", "texto": "hola esto no es nada"}).status_code == 400
    f = r.json()["fuentes"][0]["id"]
    r = cliente_api.post("/api/copiar", json={"op": "fuente_busqueda", "fuente": f, "activa": False})
    assert r.json()["fuentes"][0]["activa"] is False
    r = cliente_api.post("/api/copiar", json={"op": "fuente_busqueda", "fuente": f, "borrar": True})
    assert len(r.json()["fuentes"]) == 2
    for mal in ({"fuente": 999}, {"fuente": "x"}, {"fuente": None}, {}):
        assert cliente_api.post("/api/copiar", json={"op": "fuente_busqueda", **mal}).status_code == 400
    assert "fuentes_busqueda" in cliente_api.get("/api/copias").json()


def test_fichas_guardan_la_cuenta_y_nunca_none(monkeypatch):
    import sys
    from types import SimpleNamespace
    from hookline.servicios import fabrica

    class Y:
        def __init__(self, o):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def extract_info(self, link, download=False):
            if "SIN" in link:
                return {"id": "1", "channel": None, "uploader": None}
            return {"id": "2", "channel": "cuenta.memes", "uploader": "Cuenta Memes"}
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Y))
    fabrica.guardar_links("https://www.instagram.com/reel/SIN/\nhttps://www.instagram.com/reel/CON/")
    fabrica._fichas({})
    fs = {x["link"]: x for x in fabrica.links()}
    assert fs["https://www.instagram.com/reel/CON/"]["usuario"] == "cuenta.memes"
    assert "usuario" not in fs["https://www.instagram.com/reel/SIN/"]
