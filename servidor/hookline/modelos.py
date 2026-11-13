"""Tablas. Reemplazan los JSON de Hookline Lite (datos/*.json y cuentas/<id>/*.json)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, FechaUTC, JSONTexto
from .db import ahora as _ahora


# --- usuarios y acceso ------------------------------------------------------------------------------
class Usuario(Base):
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(120), default="")
    clave_hash: Mapped[str] = mapped_column(String(300))
    rol: Mapped[str] = mapped_column(String(20), default="editor")      # admin | editor | lector
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)


class Sesion(Base):
    __tablename__ = "sesiones"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)        # sha256 del token de la cookie
    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    csrf: Mapped[str] = mapped_column(String(80))
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    expira: Mapped[datetime] = mapped_column(FechaUTC)


class TokenAPI(Base):
    """Para integraciones (Pyro): `Authorization: Bearer hl_...`. Solo se guarda el hash."""
    __tablename__ = "tokens_api"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    hash: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    prefijo: Mapped[str] = mapped_column(String(16))
    rol: Mapped[str] = mapped_column(String(20), default="editor")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    ultimo_uso: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    revocado: Mapped[bool] = mapped_column(Boolean, default=False)


class Ajuste(Base):
    """Clave-valor. Los valores con `cifrado=True` se guardan con Fernet (ej.: el App Secret)."""
    __tablename__ = "ajustes"
    clave: Mapped[str] = mapped_column(String(80), primary_key=True)
    valor: Mapped[object] = mapped_column(JSONTexto, nullable=True)
    cifrado: Mapped[bool] = mapped_column(Boolean, default=False)
    actualizado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, onupdate=_ahora)


class EstadoOAuth(Base):
    __tablename__ = "oauth_estados"
    state: Mapped[str] = mapped_column(String(80), primary_key=True)
    usuario_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)


# --- cuentas de Instagram ---------------------------------------------------------------------------
class Cuenta(Base):
    __tablename__ = "cuentas"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)        # @usuario de Instagram (sin @)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    nicho: Mapped[str] = mapped_column(String(300), default="")
    tono: Mapped[str] = mapped_column(String(300), default="")
    apodo: Mapped[str] = mapped_column(String(300), default="")
    main_id: Mapped[str | None] = mapped_column(ForeignKey("cuentas.id"), nullable=True, index=True)  # multicuenta de
    activa_desde: Mapped[str] = mapped_column(String(10), default="")    # AAAA-MM-DD (para el calentamiento)
    creada: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    tendencias_pedido: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    # lo que trae la sincronización
    ig_user_id: Mapped[str] = mapped_column(String(40), default="")
    usuario_ig: Mapped[str] = mapped_column(String(40), default="")
    tipo_cuenta: Mapped[str] = mapped_column(String(30), default="")
    foto: Mapped[str] = mapped_column(Text, default="")
    seguidores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    siguiendo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publicaciones: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ultima_sync: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    aviso: Mapped[str] = mapped_column(Text, default="")
    insights_28d: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)
    limite: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)
    metricas_ok: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)


class CredencialIG(Base):
    """El token de 60 días, cifrado. Nunca sale por la API."""
    __tablename__ = "credenciales_ig"
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), primary_key=True)
    token_cifrado: Mapped[str] = mapped_column(Text)
    permisos: Mapped[str] = mapped_column(Text, default="")
    obtenido: Mapped[float] = mapped_column(Float)
    refrescado: Mapped[float] = mapped_column(Float)
    vence: Mapped[float] = mapped_column(Float)
    vencido: Mapped[bool] = mapped_column(Boolean, default=False)


class MedioIG(Base):
    __tablename__ = "medios_ig"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), index=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    tipo: Mapped[str] = mapped_column(String(30), default="")
    media_type: Mapped[str] = mapped_column(String(30), default="")
    reel: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(Text, default="")
    publicado: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True, index=True)
    miniatura: Mapped[str] = mapped_column(Text, default="")
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comentarios: Mapped[int] = mapped_column(Integer, default=0)
    vistas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alcance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guardados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compartidos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interacciones: Mapped[int | None] = mapped_column(Integer, nullable=True)
    orden: Mapped[int] = mapped_column(Integer, default=0)                # 0 = el más nuevo
    actualizado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, onupdate=_ahora)


class HistorialCuenta(Base):
    __tablename__ = "historial_cuenta"
    __table_args__ = (UniqueConstraint("cuenta_id", "hora"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), index=True)
    hora: Mapped[str] = mapped_column(String(13))                         # AAAA-MM-DDTHH (UTC): una foto por hora
    fecha: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    seguidores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vistas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publicaciones: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Comentario(Base):
    __tablename__ = "comentarios"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[str] = mapped_column(String(40), default="")
    usuario: Mapped[str] = mapped_column(String(80), default="")
    texto: Mapped[str] = mapped_column(Text, default="")
    fecha: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    respuestas: Mapped[int] = mapped_column(Integer, default=0)
    respondido_por_mi: Mapped[bool] = mapped_column(Boolean, default=False)
    post: Mapped[str] = mapped_column(Text, default="")
    post_texto: Mapped[str] = mapped_column(Text, default="")
    miniatura: Mapped[str] = mapped_column(Text, default="")
    estado: Mapped[str] = mapped_column(String(20), default="nuevo", index=True)  # nuevo|respondido|oculto|saltado
    clase: Mapped[str] = mapped_column(String(20), default="")
    sugerencia: Mapped[str] = mapped_column(Text, default="")
    alerta: Mapped[str] = mapped_column(Text, default="")
    sugiriendo: Mapped[bool] = mapped_column(Boolean, default=False)
    respuesta_enviada: Mapped[str] = mapped_column(Text, default="")
    respondido: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    respondido_por: Mapped[str] = mapped_column(String(200), default="")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)


class Tendencias(Base):
    __tablename__ = "tendencias"
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), primary_key=True)
    fecha: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    nicho: Mapped[str] = mapped_column(String(300), default="")
    items: Mapped[list] = mapped_column(JSONTexto, default=list)


# --- bandeja y cola -----------------------------------------------------------------------------------
class Pieza(Base):
    """Lo que se publica. Estados: nueva → jueces → juzgada → programada → subiendo → publicada
    (+ vencida, error, planificada = cuenta sin conectar todavía)."""
    __tablename__ = "piezas"
    __table_args__ = (Index("ix_piezas_estado_cuando", "estado", "cuando"),)
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    cuenta_id: Mapped[str] = mapped_column(ForeignKey("cuentas.id", ondelete="CASCADE"), index=True)
    tipo: Mapped[str] = mapped_column(String(12))                         # historia | reel | carrusel | foto
    archivos: Mapped[list] = mapped_column(JSONTexto, default=list)       # rutas relativas a HOOKLINE_DATOS
    estado: Mapped[str] = mapped_column(String(15), default="nueva", index=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    cuando: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    ahora: Mapped[bool] = mapped_column(Boolean, default=False)
    etapa: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    jueces: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)
    sugerido: Mapped[str] = mapped_column(String(16), default="")
    pedido_jueces: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    aprobada: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    aprobada_por: Mapped[str] = mapped_column(String(200), default="")
    inicio: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    contenedor_id: Mapped[str] = mapped_column(String(40), default="")   # creation_id de Instagram (idempotencia)
    media_id: Mapped[str] = mapped_column(String(40), default="")
    permalink: Mapped[str] = mapped_column(Text, default="")
    publicado: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    subida: Mapped[str] = mapped_column(String(12), default="")
    plan: Mapped[bool] = mapped_column(Boolean, default=False)            # la armó repartir/encargo
    dia: Mapped[str] = mapped_column(String(10), default="", index=True)
    copia_id: Mapped[str] = mapped_column(String(12), default="")
    foto: Mapped[str] = mapped_column(Text, default="")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    actualizado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, onupdate=_ahora)


# --- IA y trabajos ---------------------------------------------------------------------------------------
class TareaIA(Base):
    """Un pedido a Claude (jueces, lectura, juez, filtro, respuestas, tendencias, chat).
    Lo resuelve el motor configurado: la API de Claude o Claude Code local (puente)."""
    __tablename__ = "tareas_ia"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20), index=True)
    estado: Mapped[str] = mapped_column(String(15), default="pendiente", index=True)  # pendiente|enviada|hecha|error
    motor: Mapped[str] = mapped_column(String(15), default="")
    referencia: Mapped[str] = mapped_column(String(80), default="", index=True)
    entrada: Mapped[dict] = mapped_column(JSONTexto, default=dict)
    resultado: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    uso: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)   # tokens (para controlar el costo)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    enviada: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    terminado: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)


class Trabajo(Base):
    """Cola de trabajos del worker (en la base: no hace falta Redis). `clave` evita duplicados."""
    __tablename__ = "trabajos"
    __table_args__ = (Index("ix_trabajos_listos", "estado", "ejecutar_en"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    datos: Mapped[dict] = mapped_column(JSONTexto, default=dict)
    estado: Mapped[str] = mapped_column(String(12), default="pendiente")   # pendiente|corriendo|hecho|error
    clave: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    ejecutar_en: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    max_intentos: Mapped[int] = mapped_column(Integer, default=3)
    tomado_por: Mapped[str] = mapped_column(String(80), default="")
    tomado_hasta: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    terminado: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True)


# --- chat, actividad, encargos, eventos --------------------------------------------------------------------
class MensajeChat(Base):
    __tablename__ = "chat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    de: Mapped[str] = mapped_column(String(12))                          # vos | claude | panel
    texto: Mapped[str] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSONTexto, nullable=True)
    autor: Mapped[str] = mapped_column(String(200), default="")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, index=True)


class Actividad(Base):
    __tablename__ = "actividad"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    t: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, index=True)
    tipo: Mapped[str] = mapped_column(String(30))
    texto: Mapped[str] = mapped_column(Text)
    img: Mapped[str] = mapped_column(Text, default="")
    cuenta: Mapped[str] = mapped_column(String(40), default="")
    copia: Mapped[str] = mapped_column(String(12), default="")


class Encargo(Base):
    __tablename__ = "encargos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dia: Mapped[str] = mapped_column(String(10), index=True)
    reels: Mapped[int] = mapped_column(Integer, default=3)
    historias: Mapped[int] = mapped_column(Integer, default=2)
    estado: Mapped[str] = mapped_column(String(15), default="trabajando")  # trabajando | listo | incompleto
    pedidos: Mapped[dict] = mapped_column(JSONTexto, default=dict)
    aprobado_por: Mapped[str] = mapped_column(String(200), default="")
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)


class Evento(Base):
    """Outbox: lo que se le avisa a Pyro por webhook (pieza.publicada, cuenta.token_vencido, ...)."""
    __tablename__ = "eventos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo: Mapped[str] = mapped_column(String(40), index=True)
    datos: Mapped[dict] = mapped_column(JSONTexto, default=dict)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    enviado: Mapped[datetime | None] = mapped_column(FechaUTC, nullable=True, index=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    proximo: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    error: Mapped[str] = mapped_column(Text, default="")


# --- material, fábrica y buscador --------------------------------------------------------------------------
class MaterialMeta(Base):
    """Etiquetas y datos de cada archivo del material (los archivos viven en HOOKLINE_DATOS/material/...)."""
    __tablename__ = "material_meta"
    __table_args__ = (UniqueConstraint("modelo", "seccion", "nombre"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    modelo: Mapped[str] = mapped_column(String(40), index=True)          # id de la cuenta main o "_general"
    seccion: Mapped[str] = mapped_column(String(60))
    nombre: Mapped[str] = mapped_column(String(200))
    etiquetas: Mapped[list] = mapped_column(JSONTexto, default=list)
    meta: Mapped[dict] = mapped_column(JSONTexto, default=dict)          # link, autor, titulo, tema, musica, bajado, de


class Copia(Base):
    """'Copiar video'. `datos` guarda todos los campos de la copia (texto, corte, chat, pegar, juez, versiones...)."""
    __tablename__ = "copias"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    cuenta_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    estado: Mapped[str] = mapped_column(String(15), default="bajando", index=True)
    link: Mapped[str] = mapped_column(Text, default="")
    datos: Mapped[dict] = mapped_column(JSONTexto, default=dict)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    actualizado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora, onupdate=_ahora)


class LinkBanco(Base):
    """Banco de memes de ejemplo (lo que pegó ella o encontró el buscador)."""
    __tablename__ = "banco_links"
    link: Mapped[str] = mapped_column(String(400), primary_key=True)
    agregado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
    copia_id: Mapped[str] = mapped_column(String(12), default="", index=True)
    titulo: Mapped[str] = mapped_column(Text, default="")
    cat: Mapped[str] = mapped_column(String(30), default="")
    voto: Mapped[str] = mapped_column(String(10), default="")            # oro | bueno | malo
    motivo: Mapped[str] = mapped_column(Text, default="")
    origen: Mapped[str] = mapped_column(String(20), default="")
    autor: Mapped[str] = mapped_column(String(80), default="")
    datos: Mapped[dict] = mapped_column(JSONTexto, default=dict)         # ficha: dur, titulo_orig, mini, error


class FuenteBusqueda(Base):
    __tablename__ = "fuentes_busqueda"
    __table_args__ = (UniqueConstraint("red", "consulta"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    red: Mapped[str] = mapped_column(String(4))                          # ig | tt
    consulta: Mapped[str] = mapped_column(String(200))                   # "@cuenta", "#hashtag" o frase
    vistos: Mapped[int] = mapped_column(Integer, default=0)
    sirven: Mapped[int] = mapped_column(Integer, default=0)
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    creado: Mapped[datetime] = mapped_column(FechaUTC, default=_ahora)
