"""Pedidos del 2026-11-20: un pack de emojis listo para usar (sin buscarlos uno por uno) y poder usar igual un meme
cuyo remate tiene marca de agua de otra cuenta (es decisión de la dueña)."""
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hookline import medios
from test_editor_manual import _armar, _cuadro, _vaciar, copia  # noqa: F401  (fixture)

con_ffmpeg = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")


def test_el_pack_esta_completo_y_con_su_licencia():
    from hookline.servicios import fabrica
    es = fabrica.emojis()
    assert len(es) >= 1500
    assert {"caras", "gente", "animales", "comida", "simbolos", "banderas"} <= {e["g"] for e in es}
    assert all(re.fullmatch(r"[0-9a-z-]+", e["k"]) for e in es)
    faltan = [e["k"] for e in es if not fabrica.ruta_emoji(e["k"]).is_file()]
    assert not faltan, faltan[:5]
    corazon = next(e for e in es if e["e"] == "😍")
    im = Image.open(fabrica.ruta_emoji(corazon["k"]))
    assert im.mode == "RGBA" and max(im.size) == 192 and im.getpixel((0, 0))[3] == 0     # fondo transparente
    lic = (fabrica.EMOJIS_DIR / "LICENCIA-fluentui-emoji.txt").read_text()
    assert "MIT License" in lic and "Microsoft" in lic
    assert fabrica.ruta_emoji("../../config") is None and fabrica.ruta_emoji("") is None


def test_api_del_pack(cliente_api):
    from fastapi.testclient import TestClient
    from hookline import api
    es = cliente_api.get("/api/emojis").json()["emojis"]
    assert len(es) >= 1500
    r = TestClient(api.app).get(f"/emoji/{es[0]['k']}.webp")             # las imágenes no piden login
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp" and "immutable" in r.headers["cache-control"]
    for mal in ("..%2F..%2Fapi", "no-existe", "1f60d.png"):
        assert TestClient(api.app).get(f"/emoji/{mal}.webp").status_code == 404, mal
    assert TestClient(api.app).get("/api/emojis").status_code == 401


@con_ffmpeg
def test_un_emoji_del_pack_sale_en_el_video_donde_lo_ponen(copia):  # noqa: F811
    from hookline.servicios import fabrica
    k = next(e["k"] for e in fabrica.emojis() if e["e"] == "😍")
    c = _armar(copia, texto="", ubicacion_auto=False, pegar=[],
               stickers=[{"archivo": f"emoji:{k}", "x0": 0.55, "y0": 0.2, "ancho": 0.3},
                         {"archivo": "emoji:no-existe"}, {"archivo": "emoji:../../x"}])
    assert [s["archivo"] for s in c["stickers"]] == [f"emoji:{k}"]
    a = _cuadro(copia)
    amarillo = (a[..., 0] > 200) & (a[..., 1] > 140) & (a[..., 2] < 90)            # la cara amarilla del emoji
    ys, xs = np.where(amarillo)
    H, W = amarillo.shape
    assert len(xs) > 500
    assert 0.54 <= xs.min() / W <= 0.6 and 0.8 <= xs.max() / W <= 0.86 and 0.19 <= ys.min() / H <= 0.24


def test_el_selector_de_emojis_esta_en_el_editor():
    html = (Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html").read_text(encoding="utf-8")
    assert 'id="cpEmojis"' in html and "async function elegirEmoji()" in html and "/api/emojis" in html
    assert 'String(st.archivo).startsWith("emoji:")' in html


# --- marca de agua: decisión de la dueña -----------------------------------------------------------------------------------
@con_ffmpeg
def test_usarlo_igual_con_marca_de_agua(copia, cliente_api):  # noqa: F811
    from hookline.servicios import fabrica
    fabrica._datos(copia, marca_agua=True, resultado="", versiones=[])
    with pytest.raises(ValueError, match="marca de agua"):
        fabrica.armar(copia, {})
    assert fabrica._bloqueada_por_marca(fabrica._datos(copia))
    r = cliente_api.post("/api/copiar", json={"op": "usar_con_marca", "id": copia})
    assert r.status_code == 200
    _vaciar()
    c = next(x for x in fabrica.listar() if x["id"] == copia)
    assert c["marca_ok"] and c["resultado"] and not c.get("error")
    assert not fabrica._bloqueada_por_marca(fabrica._datos(copia))
    fabrica.a_bandeja(copia, "modelo1")                                    # ya se puede mandar a la bandeja


def test_usarlo_igual_solo_si_tiene_marca(copia, cliente_api):  # noqa: F811
    r = cliente_api.post("/api/copiar", json={"op": "usar_con_marca", "id": copia})
    assert r.status_code == 400 and "no tiene marca" in r.json()["error"]
    assert cliente_api.post("/api/copiar", json={"op": "usar_con_marca", "id": "nada"}).status_code == 400


def test_el_reparto_respeta_la_decision():
    src = (Path(__file__).resolve().parents[1] / "hookline" / "servicios" / "repartir.py").read_text(encoding="utf-8")
    assert 'not (x.get("marca_agua") and not x.get("remate") and not x.get("marca_ok"))' in src
    html = (Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html").read_text(encoding="utf-8")
    assert "data-cpmarcaok" in html and "Usarlo igual" in html
