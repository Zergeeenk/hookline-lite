"""(Portado de Hookline Lite: chat.py, con fuentes que existen en Linux.)

Dibuja una captura de chat de WhatsApp (modo oscuro) en alta calidad, para pegarla en los reels en vez de recortarla
del video de ejemplo (que se ve borrosa y se le cuela el fondo). Es solo texto y globitos, como cualquier captura."""
from PIL import Image, ImageDraw, ImageFont

from . import fuentes
FONDO = (11, 20, 26)          # fondo del chat
MIO = (0, 92, 75)             # globo verde (lo que mando yo)
OTRO = (32, 44, 51)           # globo gris (lo que manda el otro)
TEXTO = (233, 237, 239)
HORA = (134, 150, 160)
TILDE = (83, 189, 235)        # doble tilde azul (leído)
AVISO = (17, 27, 33)          # cartelito "3 mensajes no leídos"


def _fuente(tam, negrita=False):
    p = fuentes.ruta("texto_negrita" if negrita else "texto")
    return ImageFont.truetype(str(p), tam) if p else ImageFont.load_default(size=tam)


def _tilde(dr, x, y, s, color):
    """Doble tilde de leído, dibujada (las fuentes no siempre la tienen)."""
    for dx in (0, int(s * 0.42)):
        dr.line([(x + dx, y + s * 0.55), (x + dx + s * 0.28, y + s * 0.85), (x + dx + s * 0.85, y + s * 0.15)],
                fill=color, width=max(2, int(s * 0.14)), joint="curve")


def dibujar(mensajes, ancho_px, destino, separador="", fondo=True):
    """mensajes: [{"de": "yo"|"otro", "texto": "...", "hora": "01:34"}]. ancho_px: ancho final en el video.
    Se dibuja al triple y se achica: queda nítido. Devuelve (ancho, alto) finales."""
    k = 3
    W = ancho_px * k
    pad, gap, r = int(W * 0.035), int(W * 0.012), int(W * 0.028)
    f_txt, f_hora, f_aviso = _fuente(int(W * 0.052)), _fuente(int(W * 0.034)), _fuente(int(W * 0.036), True)
    max_globo = int(W * 0.72)
    medir = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def partir(texto):
        renglones = []
        for parrafo in str(texto).split("\n"):
            actual = ""
            for palabra in parrafo.split(" "):
                prueba = (actual + " " + palabra).strip()
                if medir.textlength(prueba, font=f_txt) <= max_globo - pad * 2.6 or not actual:
                    actual = prueba
                else:
                    renglones.append(actual)
                    actual = palabra
            renglones.append(actual)
        return renglones

    # 1) medir todo
    alto_linea = int(f_txt.size * 1.28)
    bloques, y = [], pad
    for m in mensajes:
        if m.get("de") == "aviso" or m.get("aviso"):
            bloques.append(("aviso", m.get("texto") or m.get("aviso"), y))
            y += int(f_aviso.size * 2.2) + gap
            continue
        lineas = partir(m.get("texto", ""))
        hora = str(m.get("hora") or "")
        w_hora = medir.textlength(hora, font=f_hora) + (f_hora.size * 1.3 if m.get("de") == "yo" else 0)
        w_txt = max(medir.textlength(l, font=f_txt) for l in lineas)
        # la hora va a la derecha del último renglón si entra; si no, abajo
        ultimo = medir.textlength(lineas[-1], font=f_txt)
        al_lado = ultimo + w_hora + pad * 0.8 <= max_globo - pad * 2
        gw = int(min(max_globo, max(w_txt, ultimo + (w_hora + pad * 0.8 if al_lado else 0)) + pad * 1.6))
        gw = max(gw, int(w_hora + pad * 1.6))
        gh = int(alto_linea * len(lineas) + pad * 0.9 + (0 if al_lado else f_hora.size * 1.2))
        bloques.append(("globo", (m, lineas, hora, gw, gh), y))
        y += gh + gap
    H = y + pad - gap

    # 2) dibujar
    img = Image.new("RGBA", (W, H), FONDO + (255,) if fondo else (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    for tipo, dato, y in bloques:
        if tipo == "aviso":
            tw = medir.textlength(dato, font=f_aviso)
            bw, bh = int(tw + pad * 1.6), int(f_aviso.size * 1.8)
            x = (W - bw) // 2
            dr.rounded_rectangle((x, y, x + bw, y + bh), r, fill=AVISO)
            dr.text((W / 2, y + bh / 2), dato, font=f_aviso, fill=TEXTO, anchor="mm")
            continue
        m, lineas, hora, gw, gh = dato
        mio = m.get("de") == "yo"
        x = W - pad - gw if mio else pad
        dr.rounded_rectangle((x, y, x + gw, y + gh), r, fill=MIO if mio else OTRO)
        ty = y + pad * 0.45
        for l in lineas:
            dr.text((x + pad * 0.8, ty), l, font=f_txt, fill=TEXTO)
            ty += alto_linea
        hx = x + gw - pad * 0.7 - (f_hora.size * 1.3 if mio else 0)
        hy = y + gh - pad * 0.35
        dr.text((hx, hy), hora, font=f_hora, fill=HORA if not mio else (180, 205, 198), anchor="rd")
        if mio:
            _tilde(dr, hx + f_hora.size * 0.2, hy - f_hora.size * 0.95, f_hora.size * 0.95, TILDE)
    final = img.resize((ancho_px, max(1, H // k)), Image.LANCZOS)
    final.save(destino)
    return final.size


def notificacion(texto, ancho_px, destino, titulo="Instagram", hora="Ahora"):
    """Cartel de notificación de Instagram (modo oscuro), nítido. Devuelve (ancho, alto)."""
    k = 3
    W = ancho_px * k
    pad = int(W * 0.035)
    f_tit, f_txt = _fuente(int(W * 0.04), True), _fuente(int(W * 0.042))
    icono = int(W * 0.085)
    medir = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_txt = W - pad * 3 - icono
    lineas, actual = [], ""
    for palabra in str(texto).split():
        prueba = (actual + " " + palabra).strip()
        if medir.textlength(prueba, font=f_txt) <= max_txt or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = palabra
    lineas.append(actual)
    alto_l = int(f_txt.size * 1.3)
    H = pad * 2 + int(f_tit.size * 1.4) + alto_l * len(lineas)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((0, 0, W - 1, H - 1), int(W * 0.045), fill=(38, 38, 40, 238))
    # ícono de Instagram (degradé simple + cámara)
    ix, iy = pad, (H - icono) // 2
    ic = Image.new("RGBA", (icono, icono))
    for y in range(icono):
        for x in range(icono):
            t = (x + (icono - y)) / (2 * icono)
            ic.putpixel((x, y), (int(254 - 60 * t), int(200 * (1 - t) + 30), int(80 + 150 * t), 255))
    m = Image.new("L", (icono, icono), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, icono - 1, icono - 1), int(icono * 0.28), fill=255)
    img.paste(ic, (ix, iy), m)
    s = icono
    dr.rounded_rectangle((ix + s * 0.22, iy + s * 0.22, ix + s * 0.78, iy + s * 0.78), int(s * 0.16), outline="white", width=max(2, s // 14))
    dr.ellipse((ix + s * 0.37, iy + s * 0.37, ix + s * 0.63, iy + s * 0.63), outline="white", width=max(2, s // 14))
    tx = ix + icono + pad
    dr.text((tx, pad), titulo, font=f_tit, fill=(235, 235, 235))
    dr.text((W - pad, pad), hora, font=_fuente(int(W * 0.034)), fill=(150, 150, 155), anchor="ra")
    y = pad + int(f_tit.size * 1.4)
    for l in lineas:
        dr.text((tx, y), l, font=f_txt, fill=(245, 245, 245))
        y += alto_l
    final = img.resize((ancho_px, max(1, H // k)), Image.LANCZOS)
    final.save(destino)
    return final.size
