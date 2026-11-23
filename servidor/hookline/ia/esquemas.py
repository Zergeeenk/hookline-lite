"""Lo que tiene que devolver Claude en cada tipo de pedido. Valen para los dos motores:
  - motor API: se mandan como structured outputs (el JSON viene garantizado con esta forma),
  - motor Claude Code: se le pasa el JSON Schema en el pedido y se valida lo que escribe.
Los nombres de campos son los mismos de Hookline Lite (veredicto.json, lectura.json, juicio.json, filtro-k.json).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- jueces (jueces-cm) -------------------------------------------------------------------------------------------------
class Juez(_Base):
    nombre: str
    nota: int = Field(description="1 a 10")
    comentario: str
    riesgo: Literal["bajo", "medio", "alto"] | None = None


class Horario(_Base):
    cuando: str = Field(description="AAAA-MM-DDTHH:MM, hora local")
    motivo: str


class Veredicto(_Base):
    jueces: list[Juez] = Field(description="Exactamente 3: Público, Gancho y tendencia, Seguridad")
    veredicto: Literal["aprobar", "cambiar"]
    resumen: str
    sugerencias: list[str]
    caption: str
    hashtags: list[str]
    horario: Horario


# --- respuestas a comentarios ---------------------------------------------------------------------------------------------
class Sugerencia(_Base):
    id: str
    clase: Literal["positivo", "neutro", "pregunta", "acoso", "insulto", "spam"]
    respuesta: str


class Respuestas(_Base):
    items: list[Sugerencia]


# --- tendencias (tendencias-cm) ---------------------------------------------------------------------------------------------
class Tendencia(_Base):
    idea: str
    formato: str = Field(description="Reel | Carrusel | Historia | Foto")
    audio: str
    porque: str
    como: str
    link: str


class TendenciasRes(_Base):
    nicho: str
    items: list[Tendencia] = Field(description="Entre 5 y 8, de mayor a menor potencial")


# --- copiar video: lectura del ejemplo ---------------------------------------------------------------------------------------
class MensajeChat(_Base):
    de: Literal["yo", "otro", "aviso"]
    texto: str
    hora: str = ""


class ChatDibujado(_Base):
    mensajes: list[MensajeChat]
    x0: float
    x1: float
    y0: float


class Recuadro(_Base):
    x0: float
    y0: float
    x1: float
    y1: float


class Pegar(Recuadro):
    cuadro: float = Field(description="segundo del ejemplo donde la captura se ve completa")
    a_y0: float | None = Field(default=None, description="a qué altura pegarla en la foto nueva (0-1), opcional")


class Noti(_Base):
    texto: str
    titulo: str = "Instagram"
    x0: float | None = None
    x1: float | None = None
    y0: float | None = None


class TextoSuelto(_Base):
    texto: str
    x: float
    y: float
    tam: float | None = None


class Lectura(_Base):
    descartar: Literal["menor"] | None = Field(default=None, description='"menor" si la persona del ejemplo puede ser menor de edad')
    corte: float | None = Field(default=None, description="segundo donde termina la parte de la persona y empieza el remate")
    texto: str = ""
    texto_y: float | None = Field(default=None, description="0-1, dónde empieza el texto (arriba = 0)")
    texto_tam: float | None = Field(default=None, description="alto de letra / alto del video, ej. 0.042")
    modelo: int | None = Field(default=None, description="número de la foto de la modelo que mejor encaja")
    notas: str = ""
    marca_agua: bool = Field(default=False, description="true si el remate tiene marca de agua (TikTok, CapCut, @usuario)")
    chat: ChatDibujado | None = None
    pegar: list[Pegar] = Field(default_factory=list)
    noti: Noti | None = None
    textos: list[TextoSuelto] = Field(default_factory=list)
    recorte_remate: Recuadro | None = None
    fuente: str | None = Field(default=None, description="clave de la fuente del muestrario más parecida a la del ejemplo")


# --- juez del reel armado ----------------------------------------------------------------------------------------------------
class Juicio(_Base):
    nota_texto: int = Field(description="1-5")
    nota_tecnica: int = Field(description="1-5")
    motivo: str
    arreglo: Literal["otra_foto", "texto", "musica"] | None = None
    foto: int | None = None
    texto_y: float | None = None


# --- filtro del buscador -----------------------------------------------------------------------------------------------------
class Elegido(_Base):
    n: int
    cat: str
    titulo: str


class Filtro(_Base):
    sirven: list[Elegido]
    fuentes_nuevas: list[str] = Field(default_factory=list, description='0-5, formato "ig @cuenta", "ig: frase", "tt @cuenta" o "pin: frase"')


# --- chat ---------------------------------------------------------------------------------------------------------------------
class RespuestaChat(_Base):
    respuesta: str


# --- noticias: radar, guion y jurado ---------------------------------------------------------------------------------------
class TemaRadar(_Base):
    area: str = Field(description="una de las áreas pedidas, tal cual")
    titulo: str = Field(description="el hecho en una línea, sin opinión")
    resumen: str = Field(description="2-4 frases: qué pasó, quién, cuándo")
    por_que: str = Field(description="por qué le importa a la gente hoy (1 frase)")
    datos: list[str] = Field(description="1-5 cifras o hechos verificables, cada uno con su fuente entre paréntesis")
    fuentes: list[str] = Field(description="1-4 URLs de medios u organismos donde se publicó")
    urgencia: int = Field(description="1-10: qué tan de hoy es")
    potencial: int = Field(description="1-10: qué tanto se va a ver y compartir")


class RadarRes(_Base):
    items: list[TemaRadar] = Field(description="Entre 3 y 9, de mayor a menor puntaje")


class GuionRes(_Base):
    titulo: str = Field(description="título corto para la tapa del reel")
    texto: str = Field(description="la locución completa, de corrido, lista para leer")
    caption: str = Field(description="texto del posteo: 1-3 líneas, sin hashtags")
    hashtags: list[str] = Field(description="3 a 6")
    fuentes: list[str] = Field(description="las URLs que respaldan cada dato")
    datos: list[str] = Field(description="los datos (cifras/hechos) que usa el guion")


class Persona(_Base):
    nombre: str = Field(description="el nombre del perfil, tal cual se pidió")
    nota: int = Field(description="1-10")
    se_iria: int | None = Field(default=None, description="segundo en que dejaría de mirar, o null si lo mira entero")
    comentario: str = Field(description="1-2 frases, en primera persona, como hablaría ese perfil")


class JuradoRes(_Base):
    personas: list[Persona] = Field(description="Exactamente los 10 perfiles pedidos")
    mejoras: list[str] = Field(description="Las 3 mejoras más importantes, concretas")
    texto_mejorado: str = Field(description="el guion con las 3 mejoras aplicadas, sin agregar datos nuevos")
    resumen: str = Field(description="una línea: qué funciona y qué no")


# --- versiones "para la API" ---------------------------------------------------------------------------------------------
# Structured outputs compila el esquema a una gramática y rechaza los muy complejos ("Schema is too complex for
# compilation", 400): pesan sobre todo los campos opcionales y los que aceptan dos tipos (x | None). La lectura del
# ejemplo tiene muchos, así que a la API se le pide una versión con TODO obligatorio y valores "vacíos" (-1, 0, "", [])
# en vez de null; `desde_api` la pasa a la forma de siempre (Lectura) antes de validar y guardar.
class _MensajeChatAPI(_Base):
    de: Literal["yo", "otro", "aviso"]
    texto: str
    hora: str = Field(description='"" si no se ve')


class _ChatAPI(_Base):
    hay: bool = Field(description="true SOLO si el ejemplo muestra una captura de chat de WhatsApp para redibujar")
    mensajes: list[_MensajeChatAPI] = Field(description="[] si no hay captura de chat para dibujar")
    x0: float
    x1: float
    y0: float


class _RecuadroAPI(_Base):
    x0: float
    y0: float
    x1: float
    y1: float


class _PegarAPI(_Base):
    x0: float
    y0: float
    x1: float
    y1: float
    cuadro: float = Field(description="segundo del ejemplo donde la captura se ve completa")
    a_y0: float = Field(description="a qué altura pegarla en la foto nueva (0-1); -1 = la misma")


class _NotiAPI(_Base):
    hay: bool = Field(description="true SOLO si el ejemplo muestra una notificación de Instagram real")
    texto: str = Field(description='el texto de esa notificación, tal cual; "" si no hay. Nunca tus notas.')
    titulo: str
    x0: float = Field(description="-1 si no se sabe")
    x1: float = Field(description="-1 si no se sabe")
    y0: float = Field(description="-1 si no se sabe")


class _TextoAPI(_Base):
    texto: str
    x: float
    y: float
    tam: float = Field(description="0 = tamaño normal")


class LecturaAPI(_Base):
    descartar: Literal["", "menor"] = Field(description='"menor" si la persona del ejemplo puede ser menor de edad; si no, ""')
    corte: float = Field(description="segundo donde termina la parte de la persona y empieza el remate")
    texto: str
    texto_y: float = Field(description="0-1, dónde empieza el texto (arriba = 0); -1 si no hay texto")
    texto_tam: float = Field(description="alto de letra / alto del video, ej. 0.042; 0 si no hay texto")
    modelo: int = Field(description="número de la foto de la modelo que mejor encaja; 0 si no hay hoja de fotos")
    notas: str
    marca_agua: bool
    chat: _ChatAPI
    pegar: list[_PegarAPI] = Field(description="[] si no hay capturas reales para pegar")
    noti: _NotiAPI
    textos: list[_TextoAPI] = Field(description="[] si no hay textos sueltos")
    recorte_remate: _RecuadroAPI = Field(description="todo en 0 si no hay que recortar")
    fuente: str = Field(description='clave de la fuente del muestrario más parecida; "" si no estás segura')


def _o_nada(v, minimo=None, maximo=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or (minimo is not None and v < minimo) or (maximo is not None and v > maximo):
        return None
    return v


def _parece(a: str, b: str) -> bool:
    """¿La "notificación" es en realidad la nota de Claude? (pasó en producción: la nota salía dibujada en el video)."""
    norm = lambda t: " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in str(t or "")).split())
    x, y = norm(a), norm(b)
    return bool(x and y) and (x == y or (len(x) > 25 and (x in y or y in x)))


def _lectura_desde_api(d: dict) -> dict:
    if d.get("descartar") == "menor":
        return {"descartar": "menor"}
    out = {"texto": d.get("texto") or "", "notas": d.get("notas") or "", "marca_agua": bool(d.get("marca_agua"))}
    out["corte"] = _o_nada(d.get("corte"), 0)
    out["texto_y"] = _o_nada(d.get("texto_y"), 0, 1)
    out["texto_tam"] = _o_nada(d.get("texto_tam"), 1e-9)
    m = d.get("modelo")
    out["modelo"] = m if isinstance(m, int) and not isinstance(m, bool) and m > 0 else None
    ch = d.get("chat") or {}
    if ch.get("hay") and ch.get("mensajes"):
        out["chat"] = {"mensajes": [{"de": x["de"], "texto": x["texto"], "hora": x.get("hora") or ""}
                                    for x in ch["mensajes"]], "x0": ch["x0"], "x1": ch["x1"], "y0": ch["y0"]}
    out["pegar"] = [{**{k: b[k] for k in ("x0", "y0", "x1", "y1", "cuadro")}, "a_y0": _o_nada(b.get("a_y0"), 0, 1)}
                    for b in d.get("pegar") or []]
    nt = d.get("noti") or {}
    if nt.get("hay") and str(nt.get("texto") or "").strip() and not _parece(nt["texto"], out["notas"]):
        out["noti"] = {"texto": nt["texto"], "titulo": nt.get("titulo") or "Instagram",
                       **{k: _o_nada(nt.get(k), 0) for k in ("x0", "x1", "y0")}}
    out["textos"] = [{"texto": t["texto"], "x": t["x"], "y": t["y"], "tam": _o_nada(t.get("tam"), 1e-9)}
                     for t in d.get("textos") or []]
    rr = d.get("recorte_remate") or {}
    if all(isinstance(rr.get(k), (int, float)) for k in ("x0", "y0", "x1", "y1")) and rr["x1"] > rr["x0"] \
            and rr["y1"] > rr["y0"]:
        out["recorte_remate"] = {k: rr[k] for k in ("x0", "y0", "x1", "y1")}
    out["fuente"] = str(d.get("fuente") or "").strip() or None
    return out


# tipo → (lo que se le pide a la API, cómo se pasa a la forma de ESQUEMAS)
PARA_API: dict[str, tuple[type[_Base], object]] = {"lectura": (LecturaAPI, _lectura_desde_api)}


def para_api(tipo: str):
    return PARA_API.get(tipo, (ESQUEMAS[tipo], None))


ESQUEMAS: dict[str, type[_Base]] = {
    "jueces": Veredicto, "respuestas": Respuestas, "tendencias": TendenciasRes, "lectura": Lectura,
    "juez": Juicio, "filtrar": Filtro, "chat": RespuestaChat,
    "radar": RadarRes, "guion": GuionRes, "jurado": JuradoRes,
}
