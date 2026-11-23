"""Pedidos del 23/11: (1) poder usar el pack de emojis del equipo (ej. los de iPhone) en vez de los 3D, también adentro
del texto; (2) que la sesión de Instagram/TikTok del buscador se vea siempre (aunque recargues con F5) y que avise
cuando Instagram la corta, en vez de «no busca» sin explicación."""
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hookline import medios
from test_editor_manual import _armar, _cuadro, copia  # noqa: F401  (fixture)

con_ffmpeg = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")
ROJO, AZUL = (230, 20, 20, 255), (20, 40, 230, 255)


def _png(color=ROJO, lado=64) -> bytes:
    b = io.BytesIO()
    Image.new("RGBA", (lado, lado), color).save(b, "PNG")
    return b.getvalue()


def _zip(archivos: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in archivos.items():
            z.writestr(n, d)
    return b.getvalue()


PACK = {
    "img-apple-64/1f600.png": _png(AZUL, 64),           # el mismo emoji en dos tamaños: se queda el más grande
    "img-apple-160/1f600.png": _png(ROJO, 160),
    "emoji_u1f60d.png": _png(ROJO),                      # nombre de Noto
    "2764.png": _png(ROJO),                              # sin FE0F: es el ❤️ del pack (2764-fe0f)
    "1f1e6_1f1f7.png": _png(ROJO),                       # 🇦🇷: no está en el pack 3D → nuevo
    "1fae9.png": _png(ROJO),                             # un emoji más nuevo que el pack 3D → nuevo
    "🔥.png": _png(ROJO),                                # el nombre es el emoji
    "grinning squinting face.png": _png(ROJO),           # el nombre en inglés
    "1f44d-1f3fd.png": _png(ROJO),                       # tono de piel: se ignora (el texto usa el amarillo)
    "__MACOSX/._1f600.png": b"basura",
    "LEEME.txt": b"hola",
    "1f923.png": b"no soy una imagen",                   # roto: se saltea sin romper el resto
    "a.png": _png(),                                     # no es un emoji
}


def _k(e):
    from hookline.servicios import fabrica
    return next(x["k"] for x in fabrica.emojis() if x["e"].replace("️", "") == e.replace("️", ""))


def test_importar_pack_reemplaza_suma_nuevos_y_saltea_basura():
    from hookline.servicios import fabrica
    antes = len(fabrica.emojis())
    k_sonrisa, k_corazon = _k("😀"), _k("❤️")
    assert fabrica.ruta_emoji(k_sonrisa).suffix == ".webp"
    r = fabrica.importar_pack_emojis(_zip(PACK), "Apple 2027")
    assert r == {"n": 7, "reemplazados": 5, "nuevos": 2}
    for e in ("😀", "😍", "❤️", "🔥", "😆"):
        p = fabrica.ruta_emoji(_k(e))
        assert p.suffix == ".png" and p.parent.name == "emojis-propios", e
    im = Image.open(fabrica.ruta_emoji(k_sonrisa))
    assert im.size == (160, 160) and im.getpixel((5, 5))[:3] == ROJO[:3]          # el de 160, no el de 64 (azul)
    assert fabrica.ruta_emoji(k_corazon).suffix == ".png"
    es = fabrica.emojis()
    assert len(es) == antes + 2
    nuevos = {e["e"]: e for e in es if e["g"] == "nuevos"}
    assert set(nuevos) == {"🇦🇷", "\U0001FAE9"}
    assert fabrica.ruta_emoji(nuevos["🇦🇷"]["k"]).is_file()
    assert fabrica.ruta_emoji(_k("😂")).suffix == ".webp"                      # el que el pack no trae sigue en 3D
    assert fabrica.pack_emojis() == {"propio": 7, "nuevos": 2, "version": fabrica._version_pack(), "nombre": "Apple 2027"}
    t, mapa = fabrica.emojis_del_texto("vamos 🇦🇷 \U0001FAE9")                  # adentro del texto también
    assert sorted(mapa.values()) == sorted(e["k"] for e in nuevos.values())


def test_otro_pack_reemplaza_al_anterior_y_volver_a_los_3d():
    from hookline.servicios import fabrica
    fabrica.importar_pack_emojis(_zip(PACK))
    v1 = fabrica._version_pack()
    fabrica.importar_pack_emojis(_zip({"1f525.png": _png()}))
    assert fabrica.pack_emojis()["propio"] == 1 and fabrica._version_pack() != v1
    assert fabrica.ruta_emoji(_k("😀")).suffix == ".webp"                      # lo del pack anterior ya no está
    assert not [e for e in fabrica.emojis() if e["g"] == "nuevos"]
    fabrica.borrar_pack_emojis()
    assert fabrica.pack_emojis()["propio"] == 0 and fabrica.ruta_emoji(_k("🔥")).suffix == ".webp"


def test_el_otro_proceso_ve_el_pack_nuevo_sin_reiniciar():
    """API y worker son procesos distintos: el índice se relee por la versión del pack.json, no queda en caché."""
    from hookline.servicios import fabrica
    fabrica.emojis_del_texto("🔥")                                             # calienta las cachés
    d = fabrica._dir_pack()
    Image.new("RGBA", (8, 8), ROJO).save(d / "1fae9.png")
    (d / "pack.json").write_text(json.dumps({"version": "otro-proceso", "n": 1, "extra": [
        {"k": "1fae9", "n": "nuevo", "g": "nuevos", "e": "\U0001FAE9", "p": []}]}), encoding="utf-8")
    assert fabrica.ruta_emoji("1fae9") == d / "1fae9.png"
    assert list(fabrica.emojis_del_texto("a \U0001FAE9")[1].values()) == ["1fae9"]


@pytest.mark.parametrize("datos,error", [
    (b"", "ZIP"), (b"no es un zip", "no es un ZIP"), (_zip({"LEEME.txt": b"x", "a.png": _png()}), "No encontré emojis"),
    (_zip({"1f600.png": b"roto"}), "Ninguna imagen"),
])
def test_zips_malos_dan_error_claro_y_no_tocan_el_pack(datos, error):
    from hookline.servicios import fabrica
    fabrica.importar_pack_emojis(_zip({"1f525.png": _png()}))
    with pytest.raises(ValueError, match=error):
        fabrica.importar_pack_emojis(datos)
    assert fabrica.pack_emojis()["propio"] == 1 and fabrica.ruta_emoji(_k("🔥")).suffix == ".png"
    assert not list(fabrica._dir_pack().parent.glob("*.nuevo"))


def test_zip_con_rutas_peligrosas_no_escribe_fuera():
    from hookline.servicios import fabrica
    fabrica.importar_pack_emojis(_zip({"../../../1f525.png": _png(), "/etc/1f600.png": _png()}))
    assert {p.name for p in fabrica._dir_pack().iterdir()} == {"1f525.png", "1f600.png", "pack.json"}


def test_api_del_pack_propio(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    r = cliente_api.post("/api/emojis_pack?nombre=Apple", content=_zip(PACK))
    assert r.status_code == 200 and r.json()["n"] == 7 and r.json()["pack"]["nombre"] == "Apple"
    d = cliente_api.get("/api/emojis").json()
    assert d["pack"]["propio"] == 7 and any(e["g"] == "nuevos" for e in d["emojis"])
    assert cliente_api.get("/api/copias").json()["emojis_v"] == d["pack"]["version"]
    k = next(e["k"] for e in d["emojis"] if e["e"] == "😀")
    r = cliente_api.get(f"/emoji/{k}.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert TestClient(api.app).get(f"/emoji/{k}.webp").status_code == 401             # las del equipo piden login
    k3d = next(e["k"] for e in d["emojis"] if e["e"] == "😂")
    assert TestClient(api.app).get(f"/emoji/{k3d}.webp").status_code == 200            # las 3D (MIT) no
    assert cliente_api.post("/api/emojis_pack", content=b"basura").status_code == 400
    assert TestClient(api.app).post("/api/emojis_pack", content=_zip(PACK)).status_code == 401
    r = cliente_api.post("/api/copiar", json={"op": "borrar_pack_emojis"})
    assert r.status_code == 200 and r.json()["pack"]["propio"] == 0
    assert cliente_api.get(f"/emoji/{k}.webp").headers["content-type"] == "image/webp"


@con_ffmpeg
def test_el_emoji_del_pack_propio_sale_en_el_video_como_sticker_y_en_el_texto(copia):  # noqa: F811
    from hookline.servicios import fabrica
    fabrica.importar_pack_emojis(_zip({"1f1e6_1f1f7.png": _png(ROJO, 160), "1f60d.png": _png(ROJO, 160)}))
    k_ar = next(e["k"] for e in fabrica.emojis() if e["e"] == "🇦🇷")
    c = _armar(copia, texto="", ubicacion_auto=False, pegar=[],
               stickers=[{"archivo": f"emoji:{k_ar}", "x0": 0.55, "y0": 0.2, "ancho": 0.3}])
    assert [s["archivo"] for s in c["stickers"]] == [f"emoji:{k_ar}"]          # un emoji nuevo vale como sticker
    a = _cuadro(copia)
    rojo = (a[..., 0] > 190) & (a[..., 1] < 70) & (a[..., 2] < 70)
    ys, xs = np.where(rojo)
    H, W = rojo.shape
    assert len(xs) > 500 and 0.54 <= xs.min() / W <= 0.58 and 0.82 <= xs.max() / W <= 0.87, (xs.min() / W, xs.max() / W)
    c = _armar(copia, texto="hola 😍", ubicacion_auto=False, texto_y=0.7, texto_x=0.5, texto_tam=0.06, stickers=[])
    assert c["faltan"] == ""
    a = _cuadro(copia)
    rojo = (a[..., 0] > 190) & (a[..., 1] < 70) & (a[..., 2] < 70)
    ys, _ = np.where(rojo)
    assert len(ys) > 100 and 0.6 <= np.median(ys) / H <= 0.8                    # el cuadrado rojo, en el renglón


# --- sesiones ----------------------------------------------------------------------------------------------------------------
HEADER = "sessionid=111%3Aabc%3A22; csrftoken=tok; ds_user_id=111"


def test_la_sesion_se_ve_siempre_y_avisa_cuando_vence(cliente_api, monkeypatch):
    from hookline.servicios import buscador
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    se = cliente_api.get("/api/copias").json()["sesiones"]
    assert se["instagram"]["cargada"] is False and se["instagram"]["vencida"] is False
    assert cliente_api.post("/api/copiar", json={"op": "importar_cookies", "texto": HEADER}).status_code == 200
    se = cliente_api.get("/api/copias").json()["sesiones"]                     # como recargar con F5: sigue ahí
    assert se["instagram"]["cargada"] and not se["instagram"]["vencida"] and se["instagram"]["cargada_el"]
    assert not se["tiktok"]["cargada"]
    buscador.marcar_sesion("instagram", True, "Instagram cerró la sesión al buscar")
    se = cliente_api.get("/api/copias").json()["sesiones"]
    assert se["instagram"]["vencida"] and "cerró" in se["instagram"]["motivo"]
    cliente_api.post("/api/copiar", json={"op": "importar_cookies", "texto": HEADER})
    assert not cliente_api.get("/api/copias").json()["sesiones"]["instagram"]["vencida"]   # cargar nuevas lo limpia
    buscador.marcar_sesion("pinterest", True)                                  # una red que no existe: nada


def test_una_descarga_con_la_sesion_vencida_la_marca(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from hookline.servicios import buscador, material
    from test_sesion_ytdlp import _YDLQueBorra
    monkeypatch.setattr(buscador, "_abierto", lambda: False)
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_YDLQueBorra))
    buscador.importar_cookies(HEADER)
    with pytest.raises(ValueError, match="venció"):
        material.bajar_video("https://www.instagram.com/p/X/", tmp_path)
    s = buscador.sesiones()["instagram"]
    assert s["cargada"] and s["vencida"] and "bajar" in s["motivo"]


def test_el_buscador_detecta_que_instagram_borro_la_sesion():
    from hookline.servicios import buscador

    class Ctx:
        def __init__(self, cs):
            self.cs = cs

        def cookies(self, url):
            return self.cs
    assert buscador._sesion_viva(Ctx([{"name": "sessionid", "value": "x"}]), "instagram")
    assert not buscador._sesion_viva(Ctx([{"name": "sessionid", "value": ""}, {"name": "csrftoken", "value": "t"}]),
                                     "instagram")
    assert "HeadlessChrome" not in buscador.AGENTE and "Chrome/" in buscador.AGENTE


def test_el_panel_muestra_las_sesiones_y_el_pack():
    html = (Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html").read_text(encoding="utf-8")
    assert "function sesionesHtml(" in html and html.count("sesionesHtml(CP.sesiones)") == 1
    assert 'id="emoPackArchivo"' in html and "/api/emojis_pack" in html and "borrar_pack_emojis" in html
    assert "dejá abierta la ventana de Chrome" not in html
    assert html.count("/emoji/${") == 1                                       # todas las imágenes pasan por emoUrl()


def test_nombres_de_archivo_de_todos_los_packs():
    """Cada emoji del pack 3D se reconoce con los nombres que usan los packs que andan por ahí (iamcal/emoji-data,
    Noto, con o sin FE0F, con ceros a la izquierda); lo que no es emoji no entra como «nuevo»."""
    from hookline.servicios import fabrica
    base = fabrica._indice_base()
    pc = {fabrica._sin_fe0f(k): k for k in base}
    pn = {e["n"].lower(): k for k, e in base.items()}
    for k in base:
        sin = k.replace("-fe0f", "")
        for n in (f"{k}.png", f"{sin}.png", f"emoji_u{sin.replace('-', '_')}.png", f"{sin.upper()}.PNG"):
            assert fabrica._clave_de_archivo(n, pc, pn)[0] == k, (n, k)
    assert fabrica._clave_de_archivo("23-20e3.png", pc, pn)[0] == "0023-fe0f-20e3"          # #️⃣
    assert fabrica._clave_de_archivo("a9.png", pc, pn)[0] == "00a9-fe0f"                     # ©
    for n in ("cafe.png", "logo.png", "a.png", "e000.png", "ffff.png", "1f3fb.png", "200d.png", "x" * 40 + ".png"):
        assert fabrica._clave_de_archivo(n, pc, pn) == ("", ""), n
