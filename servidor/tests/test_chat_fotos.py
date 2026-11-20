"""Pedido 2026-11-20: mandarle fotos a Claude por el chat (de la compu o pegadas con Ctrl+V)."""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

PANEL = Path(__file__).resolve().parents[1] / "hookline" / "panel" / "index.html"


def _img(fmt="PNG", tam=(400, 300), modo="RGB", color=(200, 30, 90)):
    b = io.BytesIO()
    Image.new(modo, tam, color if modo == "RGB" else color + (128,)).save(b, fmt)
    return b.getvalue()


def test_la_foto_se_guarda_liviana_y_sin_transparencia():
    from hookline import almacen
    from hookline.servicios import jueces
    rel = jueces.foto_chat("captura.png", _img(tam=(4000, 3000), modo="RGBA"))
    assert rel.startswith("chat/") and rel.endswith(".jpg")
    im = Image.open(almacen.ruta(rel))
    assert im.mode == "RGB" and max(im.size) == 1568
    for malo in (b"", b"no soy una foto", b"\x89PNG\r\n\x1a\nroto"):
        with pytest.raises(ValueError):
            jueces.foto_chat("x.png", malo)
    with pytest.raises(ValueError, match="15 MB"):
        jueces.foto_chat("x.png", b"0" * (16 * 1024 * 1024))


def test_mensaje_con_fotos_le_llega_a_claude():
    from hookline.ia import tareas
    from hookline.servicios import jueces
    f1, f2 = jueces.foto_chat("a.png", _img()), jueces.foto_chat("b.jpg", _img("JPEG"))
    m = jueces.mensaje("¿por qué sale mal este chat?", fotos=[f1, f2])
    assert m["fotos"] == [f1, f2] and m["texto"] == "¿por qué sale mal este chat?"
    t = tareas.pendientes_claude_code()[-1]
    assert t.tipo == "chat" and t.entrada["imagenes"] == [f1, f2]
    assert "te mandó 2 fotos" in t.entrada["instrucciones"]
    solo = jueces.mensaje("", fotos=[f1])                       # solo la foto, sin texto
    assert solo["texto"] == "¿Qué ves acá?"
    assert "te mandó 1 foto" in tareas.pendientes_claude_code()[-1].entrada["instrucciones"]


@pytest.mark.parametrize("fotos,motivo", [
    (["material/modelo1/modelo/selfie.jpg"], "no es del chat"),
    (["../../../etc/passwd"], "no es del chat"),
    (["chat/2026-11/no-existe.jpg"], "ya no está"),
    ([5], "inválida"),
    ("chat/x.jpg", "lista"),
])
def test_fotos_raras_se_rechazan(fotos, motivo):
    from hookline.servicios import jueces
    with pytest.raises(ValueError, match=motivo):
        jueces.mensaje("hola", fotos=fotos)


def test_hasta_cuatro_fotos_y_sin_nada_no_manda():
    from hookline.servicios import jueces
    fs = [jueces.foto_chat(f"{i}.png", _img()) for i in range(5)]
    with pytest.raises(ValueError, match="Hasta 4"):
        jueces.mensaje("hola", fotos=fs)
    with pytest.raises(ValueError, match="Escribí algo"):
        jueces.mensaje("   ", fotos=[])


def test_api_subir_y_mandar(cliente_api):
    r = cliente_api.post("/api/chat_foto?nombre=captura.png", content=_img(),
                         headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    foto = r.json()["foto"]
    assert cliente_api.get("/mini?p=" + foto).status_code == 200
    m = cliente_api.post("/api/chat", json={"texto": "mirá esto", "fotos": [foto]}).json()
    assert m["fotos"] == [foto]
    assert cliente_api.get("/api/estado").json()["chat"][-1]["fotos"] == [foto]
    assert cliente_api.post("/api/chat_foto", content=b"hola",
                            headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert cliente_api.post("/api/chat", json={"texto": "x", "fotos": ["material/a.jpg"]}).status_code == 400
    assert cliente_api.post("/api/chat", json={"texto": "solo texto"}).status_code == 200    # como antes


def test_el_motor_api_le_manda_la_imagen(monkeypatch):
    from hookline import comun, trabajos
    from hookline.ia import motor_api
    from hookline.servicios import jueces
    comun.guardar_ajuste("motor_ia", "api")
    llamadas = []

    def parse(**kw):
        llamadas.append(kw)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, model=kw["model"],
                               parsed_output=kw["output_format"].model_validate({"respuesta": "Veo un chat."}),
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))
    falso = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(parse=parse)),
                            messages=SimpleNamespace(parse=parse))
    monkeypatch.setattr(motor_api, "cliente", lambda: falso)
    jueces.mensaje("¿qué ves?", fotos=[jueces.foto_chat("a.png", _img())])
    trabajos.correr(trabajos.tomar("w", ["ia"]))
    contenido = llamadas[0]["messages"][0]["content"]
    assert contenido[0]["type"] == "image" and contenido[0]["source"]["media_type"] == "image/jpeg"
    assert contenido[-1]["type"] == "text"


def test_el_panel_deja_adjuntar_y_pegar():
    html = PANEL.read_text(encoding="utf-8")
    assert 'id="chatAdj"' in html and 'id="chatArchivo"' in html and 'addEventListener("paste"' in html
    assert "/api/chat_foto" in html and "{ texto: t, fotos }" in html and 'addEventListener("drop"' in html
