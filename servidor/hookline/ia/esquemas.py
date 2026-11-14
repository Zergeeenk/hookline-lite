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
    fuentes_nuevas: list[str] = Field(default_factory=list, description='0-5, formato "ig @cuenta" o "ig: frase"')


# --- chat ---------------------------------------------------------------------------------------------------------------------
class RespuestaChat(_Base):
    respuesta: str


ESQUEMAS: dict[str, type[_Base]] = {
    "jueces": Veredicto, "respuestas": Respuestas, "tendencias": TendenciasRes, "lectura": Lectura,
    "juez": Juicio, "filtrar": Filtro, "chat": RespuestaChat,
}
