"""API HTTP (FastAPI). Mismas rutas y formas que el panel de Hookline Lite (/api/*), más:
  - login (/api/login, /api/logout, /api/yo), CSRF y roles;
  - tokens Bearer para Pyro (/api/tokens) y usuarios (/api/usuarios), solo admin;
  - /publico/... (URLs firmadas para que Meta baje los archivos) y /api/salud.
Documentación automática en /docs (OpenAPI).
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from . import __version__, almacen, auth, comun, instagram as ig, medios
from .auth import Quien, requerir
from .config import config
from .db import sesion
from .ia import tareas
from .modelos import MensajeChat, TokenAPI, Usuario
from .servicios import comentarios as svc_com
from .servicios import cuentas as svc_cuentas
from .servicios import jueces as svc_jueces
from .servicios import noticias as svc_noticias
from .servicios import piezas as svc_piezas

log = logging.getLogger("hookline.api")
PANEL = Path(__file__).parent / "panel" / "index.html"
MAX_SUBIDA = 1024 * 1024 * 1024
# desde dónde se pueden ver archivos por /mini y /media
SECCIONES_VISIBLES = ("subir", "material", "copias", "resultados", "jueces", "candidatos", "stickers", "chat")

app = FastAPI(title="Hookline", version=__version__,
              description="Community manager de Instagram multi-cuenta. Auth: cookie de sesión (panel) o "
                          "`Authorization: Bearer hl_...` (integraciones).")
if config().origenes_permitidos:
    app.add_middleware(CORSMiddleware, allow_origins=list(config().origenes_permitidos), allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


def _opcional(nombre: str):
    """Los módulos de la fábrica se cargan si están (así la API arranca aunque falte playwright, por ejemplo)."""
    try:
        return __import__(f"hookline.servicios.{nombre}", fromlist=["x"])
    except ImportError as ex:
        log.warning("módulo %s no disponible: %s", nombre, ex)
        return None


svc_material, svc_fabrica, svc_buscador, svc_repartir = (_opcional(n) for n in ("material", "fabrica", "buscador", "repartir"))
svc_sesion_ig = _opcional("sesion_ig")


def _mod(m, nombre):
    if m is None:
        raise HTTPException(503, f"La parte '{nombre}' no está disponible en este servidor.")
    return m


def _tiene_nulo(v) -> bool:
    if isinstance(v, str):
        return "\x00" in v
    if isinstance(v, dict):
        return any(_tiene_nulo(k) or _tiene_nulo(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_tiene_nulo(x) for x in v)
    return False


@app.middleware("http")
async def _sin_nulos(req: Request, siguiente):
    """Un byte nulo en la URL no es nunca algo legítimo (y Postgres no lo acepta en textos): 400 de entrada."""
    if "%00" in req.url.query.lower() or "%00" in req.url.path.lower() or "\x00" in req.url.path:
        return JSONResponse({"error": "Pedido inválido."}, status_code=400)
    return await siguiente(req)


@app.exception_handler(ValueError)
async def _valor(_req, ex: ValueError):
    return JSONResponse({"error": str(ex)[:300]}, status_code=400)


@app.exception_handler(ig.TokenVencido)
async def _vencido(_req, _ex):
    return JSONResponse({"error": "El permiso de Instagram venció: reconectá la cuenta."}, status_code=400)


@app.exception_handler(ig.ErrorInstagram)
async def _err_ig(_req, ex):
    return JSONResponse({"error": str(ex)[:300]}, status_code=400)


@app.exception_handler(RuntimeError)
async def _runtime(_req, ex):
    return JSONResponse({"error": str(ex)[:300]}, status_code=400)


@app.exception_handler(Exception)
async def _inesperado(_req, ex: Exception):
    """Lo que no se esperaba: JSON con el tipo de error (el panel lo muestra en vez de «Algo falló») y el detalle
    completo solo en el log del servidor."""
    log.exception("error inesperado: %s", ex)
    return JSONResponse({"error": f"Algo falló en el servidor ({type(ex).__name__}). Avisale a quien lo administra: "
                                  "el detalle quedó en los logs."}, status_code=500)


@app.exception_handler(HTTPException)
async def _http(_req, ex: HTTPException):
    return JSONResponse({"error": ex.detail}, status_code=ex.status_code)


def _cuenta(cid: str) -> str:
    if not svc_cuentas.existe(cid):
        raise HTTPException(404, "Cuenta desconocida.")
    return cid


async def _cuerpo(req: Request) -> bytes:
    n = int(req.headers.get("content-length") or 0)
    if n > MAX_SUBIDA:
        raise HTTPException(400, "El archivo pesa más de 1 GB.")
    datos = await req.body()
    if len(datos) > MAX_SUBIDA:
        raise HTTPException(400, "El archivo pesa más de 1 GB.")
    return datos


async def _json(req: Request) -> dict:
    """Solo JSON de verdad (Lite aceptaba cualquier Content-Type: CSRF con text/plain)."""
    if "application/json" not in (req.headers.get("content-type") or ""):
        raise HTTPException(415, "Mandá JSON (Content-Type: application/json).")
    try:
        d = await req.json()
    except ValueError:
        raise HTTPException(400, "JSON inválido.")
    if _tiene_nulo(d):
        raise HTTPException(400, "Pedido inválido.")
    return d if isinstance(d, dict) else {}


# --- panel y sistema --------------------------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def panel():
    return HTMLResponse(PANEL.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/salud")
def salud():
    """Para el balanceador / Pyro: no pide login. No muestra datos."""
    try:
        with sesion() as s:
            s.execute(select(1))
        bd = True
    except Exception:
        bd = False
    return {"ok": bd, "version": __version__, "motor_ia": tareas.motor_actual(), "ffmpeg": bool(medios.ffmpeg())}


@app.post("/api/login")
def login(req: Request, resp: Response, d: dict = Depends(_json)):
    q = auth.login(d.get("email", ""), d.get("clave", ""), resp, ip=req.client.host if req.client else "")
    return {"usuario": q.dict(), "csrf": q.csrf}


@app.post("/api/logout")
def logout(req: Request, resp: Response):
    auth.logout(req, resp)
    return {"ok": True}


@app.get("/api/yo")
def yo(q: Quien = requerir()):
    return {"usuario": q.dict(), "csrf": q.csrf, "via": q.via}


@app.get("/api/estado")
def estado(q: Quien = requerir()):
    with sesion() as s:
        chat = [comun.mensaje_dict(m) for m in s.scalars(select(MensajeChat).order_by(MensajeChat.id.desc()).limit(80))]
    app_id, secret = comun.app_meta()
    return {"cuentas": svc_cuentas.listar(), "piezas": svc_piezas.listar(), "chat": list(reversed(chat)),
            "claude": tareas.estado_puente(),
            "app": {"configurada": bool(app_id and secret), "cloudflared": True, "ffmpeg": bool(medios.ffmpeg())},
            "ahora": comun.iso_local(comun.ahora()), "carpeta": "", "usuario": q.dict(),
            "espacios": svc_noticias.nombres_espacios()}


# --- ajustes (admin) ------------------------------------------------------------------------------------------------------------
def _ajustes() -> dict:
    cfg = config()
    app_id, secret = comun.app_meta()
    import os
    clave = comun.clave_anthropic()
    problemas = [p for p in cfg.problemas() if not ("ANTHROPIC_API_KEY" in p and clave)]
    return {"tiene_clave_claude": bool(clave), "clave_claude_fin": clave[-4:] if len(clave) > 12 else "",
            "clave_claude_por_entorno": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            "app_id": app_id, "tiene_secret": bool(secret), "app_por_entorno": bool(cfg.meta_app_id),
            "config_id": comun.config_id_meta(),
            "redirect": cfg.redirect_instagram, "redirect_defecto": cfg.redirect_instagram, "cloudflared": "",
            "ffmpeg": bool(medios.ffmpeg()), "carpeta": "", "version": cfg.ig_version, "url_publica": cfg.url_publica,
            "motor_ia": tareas.motor_actual(), "modelo": cfg.modelo, "modelo_rapido": cfg.modelo_rapido,
            "encargo_aprueba": bool(comun.ajuste("encargo_aprueba", False)), "problemas": problemas}


@app.get("/api/ajustes")
def ajustes_get(q: Quien = requerir()):
    return _ajustes()


@app.post("/api/ajustes")
def ajustes_set(d: dict = Depends(_json), q: Quien = requerir("admin")):
    if "app_id" in d:
        comun.guardar_ajuste("meta_app_id", "".join(ch for ch in str(d["app_id"] or "") if ch.isdigit())[:30])
    if "config_id" in d:
        comun.guardar_ajuste("meta_config_id", "".join(ch for ch in str(d["config_id"] or "") if ch.isdigit())[:30])
    if d.get("app_secret"):
        comun.guardar_ajuste("meta_app_secret", str(d["app_secret"]).strip(), cifrado=True)
    if "motor_ia" in d:
        if d["motor_ia"] not in tareas.MOTORES:
            raise ValueError("Motor desconocido.")
        comun.guardar_ajuste("motor_ia", d["motor_ia"])
    if "encargo_aprueba" in d:
        comun.guardar_ajuste("encargo_aprueba", bool(d["encargo_aprueba"]))
    if d.get("borrar_clave_claude") is True:
        comun.guardar_ajuste("anthropic_api_key", None)
    clave = d.get("anthropic_api_key")
    if isinstance(clave, str) and clave.strip():
        clave = "".join(clave.split())          # sin espacios ni saltos de línea pegados de más
        if not clave.startswith("sk-ant-") or not 30 <= len(clave) <= 300:
            raise ValueError("Eso no parece una API key de Claude (empieza con sk-ant-).")
        from .ia.motor_api import probar_clave
        probar_clave(clave)
        comun.guardar_ajuste("anthropic_api_key", clave, cifrado=True)
    return _ajustes()


# --- cuentas --------------------------------------------------------------------------------------------------------------------
@app.post("/api/cuenta")
def cuenta(d: dict = Depends(_json), q: Quien = requerir()):
    cid, op = str(d.get("id", "")), d.get("op")
    if op == "crear_local":
        return {"ok": True, "id": svc_cuentas.crear_local(cid, str(d.get("main") or ""),
                                                          str(d.get("espacio") or "modelos"))}
    _cuenta(cid)
    if op == "guardar":
        svc_cuentas.guardar(cid, d)
        return {"ok": True}
    if op == "sincronizar":
        return svc_cuentas.sincronizar(cid)
    if op == "desconectar":
        svc_cuentas.desconectar(cid)
        return {"ok": True}
    if op == "borrar":
        if q.rol != "admin":
            raise HTTPException(403, "Solo un admin puede borrar cuentas.")
        svc_cuentas.borrar(cid)
        return {"ok": True}
    raise HTTPException(400, "op")


@app.post("/api/conectar_url")
def conectar_url(q: Quien = requerir()):
    return {"url": svc_cuentas.url_conectar(q.id)}


@app.get("/instagram/conectar", include_in_schema=False)
def ig_conectar(q: Quien = requerir("editor")):
    return RedirectResponse(svc_cuentas.url_conectar(q.id), status_code=302)


def _pagina(titulo: str, texto: str, ok: bool = True) -> HTMLResponse:
    import html
    color = "#e8467c" if ok else "#c0392b"
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Hookline</title><body style='font-family:Inter,system-ui,sans-serif;background:#fff6f9;color:#2b1a22;"
        f"display:grid;place-items:center;min-height:90vh;margin:0 16px'><div style='max-width:480px;background:#fff;"
        f"border:1px solid #f6d3e0;border-radius:18px;padding:32px'><h2 style='margin:0 0 8px;color:{color}'>"
        f"{html.escape(titulo)}</h2><p>{html.escape(texto)}</p><a href='/#cuentas' style='color:#e8467c;font-weight:600'>"
        f"Volver al panel</a></div>")


@app.get("/instagram/callback", include_in_schema=False)
def ig_callback(code: str = "", state: str = "", error: str = "", error_reason: str = "",
                error_description: str = ""):
    """Sin login a propósito: lo protege el `state` (obligatorio, de un uso, guardado en la base)."""
    if error:
        if error_reason == "user_denied" or error == "access_denied":
            return _pagina("No se conectó", "Se canceló el permiso en Instagram.", False)
        detalle = " ".join(f"{error_description or error_reason or error}".split())[:200]
        return _pagina("No se conectó", f"Instagram respondió con un error: {detalle}", False)
    if not code:
        log.warning("instagram/callback sin code ni error (state %s)", "presente" if state else "ausente")
        return _pagina("No se pudo conectar",
                       "Meta volvió sin el código de autorización. Casi siempre es la configuración de la app: en las "
                       "apps nuevas, Ajustes necesita el Identificador de la app y su clave secreta (Configuración → "
                       "Básica) más el ID de configuración de «API setup with Unified login»; en las viejas, el "
                       "Instagram app ID y su secret. Y la dirección de vuelta de Ajustes tiene que estar registrada "
                       "en Meta como URL de redireccionamiento. Revisalo y tocá Conectar cuenta de nuevo.", False)
    try:
        usuario, _tipo = svc_cuentas.conectar_con_codigo(code, state)
        return _pagina(f"@{usuario} conectada", "Listo. Ya podés cerrar esta pestaña y volver al panel.")
    except Exception as ex:
        return _pagina("No se pudo conectar", str(ex)[:300], False)


@app.post("/api/conectar")
def conectar(d: dict = Depends(_json), q: Quien = requerir()):
    """Plan B: pegar la dirección completa a la que volvió Instagram."""
    u = urllib.parse.urlparse(str(d.get("url") or "").strip())
    qq = urllib.parse.parse_qs(u.query)
    code = (qq.get("code") or [""])[0]
    if not code:
        raise ValueError("Esa dirección no trae el código: copiala entera desde la barra de arriba.")
    usuario, tipo = svc_cuentas.conectar_con_codigo(code, (qq.get("state") or [""])[0])
    return {"ok": True, "usuario": usuario, "tipo": tipo}


@app.get("/api/stats")
def stats(cuenta: str, q: Quien = requerir()):
    return svc_cuentas.stats(_cuenta(cuenta))


@app.get("/api/calendario")
def calendario(cuenta: str = "", q: Quien = requerir()):
    return {"medios": svc_cuentas.calendario(cuenta)}


# --- bandeja --------------------------------------------------------------------------------------------------------------------
@app.post("/api/subir")
def subir(cuenta: str = "", nombre: str = "archivo", datos: bytes = Depends(_cuerpo), q: Quien = requerir()):
    return {"ok": True, "archivo": svc_piezas.guardar_subida(cuenta, nombre, datos)}


@app.post("/api/pieza")
def pieza(d: dict = Depends(_json), q: Quien = requerir()):
    return svc_piezas.accion(d, usuario=q.firma)


@app.get("/api/piezas")
def piezas(cuenta: str = "", q: Quien = requerir()):
    """Para integraciones: la bandeja (opcionalmente de una cuenta)."""
    return {"piezas": svc_piezas.listar(cuenta or None)}


# --- comentarios, tendencias, chat -----------------------------------------------------------------------------------------------
@app.get("/api/comentarios")
def comentarios(cuenta: str, q: Quien = requerir()):
    return svc_com.de_cuenta(_cuenta(cuenta))


@app.post("/api/comentario")
def comentario(d: dict = Depends(_json), q: Quien = requerir()):
    return svc_com.accion(d, usuario=q.firma)


@app.get("/api/tendencias")
def tendencias_get(cuenta: str, q: Quien = requerir()):
    return svc_jueces.tendencias_de(_cuenta(cuenta))


@app.post("/api/tendencias")
def tendencias_post(d: dict = Depends(_json), q: Quien = requerir()):
    return svc_jueces.pedir_tendencias(_cuenta(str(d.get("cuenta", ""))))


# --- noticias (espacio "Noticias"): radar, guiones con jurado y costos ---------------------------------------------------------------
@app.get("/api/noticias")
def noticias_get(cuenta: str, q: Quien = requerir()):
    return svc_noticias.listar(_cuenta(cuenta))


@app.post("/api/noticias")
def noticias_post(d: dict = Depends(_json), q: Quien = requerir()):
    op = str(d.get("op") or "")
    if op == "radar":
        return svc_noticias.pedir_radar(_cuenta(str(d.get("cuenta", ""))))
    if op in ("guion", "descartar_tema"):
        nid = svc_noticias.id_valido(d.get("noticia"), "el tema")
        if op == "guion":
            return svc_noticias.pedir_guion(nid)
        svc_noticias.descartar_noticia(nid)
        return {"ok": True}
    if op in ("editar", "aprobar", "descartar", "jurado", "reescribir"):
        return svc_noticias.accion_guion(d)
    if op == "config":
        if auth.NIVEL[q.rol] < auth.NIVEL["admin"]:
            raise HTTPException(403, "Solo un admin cambia las reglas de los guiones y el presupuesto.")
        return {"ok": True, "config": svc_noticias.guardar_conf(d.get("config") or {})}
    raise HTTPException(400, "op")


@app.post("/api/espacios")
def espacios_post(d: dict = Depends(_json), q: Quien = requerir("admin")):
    return {"ok": True, "espacios": svc_noticias.guardar_nombres(d.get("nombres") or {})}


@app.post("/api/noticias/chequear")
def noticias_chequear(d: dict = Depends(_json), q: Quien = requerir()):
    """El chequeo sin IA (para ver en vivo mientras se edita el texto)."""
    fuentes = d.get("fuentes") if isinstance(d.get("fuentes"), list) else []
    return svc_noticias.chequear(str(d.get("texto") or "")[:6000], fuentes)


@app.get("/api/costos")
def costos(dias: int = 30, q: Quien = requerir("editor")):
    return svc_noticias.costos(dias)


@app.post("/api/chat")
def chat(d: dict = Depends(_json), q: Quien = requerir()):
    return svc_jueces.mensaje(d.get("texto", ""), autor=q.firma, fotos=d.get("fotos"))


@app.post("/api/chat_foto")
def chat_foto(nombre: str = "foto.jpg", datos: bytes = Depends(_cuerpo), q: Quien = requerir()):
    """Sube una foto para mandarle a Claude por el chat (de la compu o pegada con Ctrl+V)."""
    return {"ok": True, "foto": svc_jueces.foto_chat(nombre, datos)}


@app.get("/api/actividad")
def actividad(q: Quien = requerir()):
    from .modelos import Actividad, Encargo
    with sesion() as s:
        items = [{"t": comun.local(a.t).isoformat(timespec="seconds"), "tipo": a.tipo, "texto": a.texto, "img": a.img,
                  "cuenta": a.cuenta, "copia": a.copia}
                 for a in s.scalars(select(Actividad).order_by(Actividad.id.desc()).limit(60))]
        e = s.scalar(select(Encargo).order_by(Encargo.id.desc()).limit(1))
        enc = {"dia": e.dia, "reels": e.reels, "historias": e.historias, "estado": e.estado, "pedidos": e.pedidos,
               "creado": comun.iso_local(e.creado)} if e else {}
    return {"items": items, "encargo": enc}


# --- material, fábrica, buscador, repartir ----------------------------------------------------------------------------------------
@app.get("/api/modelos")
def modelos(q: Quien = requerir()):
    return {"modelos": _mod(svc_material, "material").resumen_modelos()}


@app.get("/api/material")
def material_get(cuenta: str, q: Quien = requerir()):
    return _mod(svc_material, "material").listar(_cuenta(cuenta))


@app.post("/api/material_subir")
def material_subir(cuenta: str = "", sec: str = "", nombre: str = "archivo", datos: bytes = Depends(_cuerpo),
                   q: Quien = requerir()):
    m = _mod(svc_material, "material")
    return {"ok": True, "archivo": m.guardar(_cuenta(cuenta), sec, nombre, datos)}


@app.post("/api/material")
def material_post(d: dict = Depends(_json), q: Quien = requerir()):
    m = _mod(svc_material, "material")
    cid, op = _cuenta(str(d.get("cuenta", ""))), d.get("op")
    sec, nombre = str(d.get("sec", "")), str(d.get("nombre", ""))
    if op == "nueva_carpeta":
        return {"ok": True, "nombre": m.nueva_carpeta(cid, d.get("nombre"))}
    if op == "bajar":
        return {"ok": True, "id": m.bajar(cid, d.get("link"))}
    if op == "etiquetar":
        return {"ok": True, "etiquetas": m.etiquetar(cid, sec, nombre, d.get("etiquetas"))}
    if op == "descartar":
        m.descartar(str(d.get("id", "")))
        return {"ok": True}
    if op == "borrar":
        m.borrar(cid, sec, nombre)
        return {"ok": True}
    if op == "a_bandeja":
        m.a_bandeja(cid, sec, nombre)
        return {"ok": True}
    raise HTTPException(400, "op")


@app.get("/api/copias")
def copias(q: Quien = requerir()):
    f = _mod(svc_fabrica, "fábrica")
    return {"items": f.listar(), "opciones": f.opciones(), "links": f.links(),
            "busqueda": svc_buscador.estado() if svc_buscador else {"estado": "parado", "mensaje": "Buscador no disponible."},
            "fuentes_busqueda": svc_buscador.fuentes() if svc_buscador else [],
            "sesiones": svc_buscador.sesiones() if svc_buscador else {}, "emojis_v": f.pack_emojis()["version"],
            "sesion_ig": _sesion_ig_publico()}


def _sesion_ig_publico() -> dict:
    """Que un problema con el inicio de sesión de Instagram (por ejemplo, otra HOOKLINE_CLAVE_SECRETA) no tire toda la
    página de Memes."""
    if not svc_sesion_ig:
        return {}
    try:
        return svc_sesion_ig.publico()
    except Exception as ex:
        log.warning("estado del inicio de sesión de Instagram: %s", type(ex).__name__)
        return {}


@app.get("/api/sesion_ig/captura")
def sesion_ig_captura(q: Quien = requerir()):
    """Lo que mostró Instagram en el último inicio de sesión que no anduvo (captura de pantalla)."""
    p = _mod(svc_sesion_ig, "inicio de sesión de Instagram").captura()
    if not p:
        raise HTTPException(404, "No hay captura del último intento.")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/sesion_ig/diagnostico")
def sesion_ig_diagnostico(q: Quien = requerir()):
    """El texto de la página y lo que contestó Instagram en el último inicio de sesión que no anduvo."""
    return _mod(svc_sesion_ig, "inicio de sesión de Instagram").diagnostico()


@app.get("/api/sesion_ig")
def sesion_ig_estado(q: Quien = requerir()):
    """El inicio de sesión de Instagram con usuario y contraseña (el panel lo consulta mientras corre)."""
    _mod(svc_sesion_ig, "inicio de sesión de Instagram")
    return {"sesion_ig": _sesion_ig_publico(), "sesiones": svc_buscador.sesiones() if svc_buscador else {}}


@app.get("/api/copia_capa")
def copia_capa(id: str, que: str, k: int = 0, q: Quien = requerir()):
    """La imagen de una capa del editor (captura recortada, chat o notificación) para verla y moverla."""
    p = _mod(svc_fabrica, "fábrica").capa(id, que, k)
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/emojis")
def emojis_lista(q: Quien = requerir()):
    """El pack de emojis para el selector de stickers (nombre, grupo, palabras para buscar) y el pack propio si hay."""
    f = _mod(svc_fabrica, "fábrica")
    return JSONResponse({"emojis": f.emojis(), "pack": f.pack_emojis()}, headers={"Cache-Control": "no-cache"})


@app.post("/api/emojis_pack")
def emojis_pack(nombre: str = "", datos: bytes = Depends(_cuerpo), q: Quien = requerir("editor")):
    """Sube el pack de emojis del equipo (un ZIP con las imágenes): reemplaza a los 3D donde coincidan."""
    f = _mod(svc_fabrica, "fábrica")
    r = f.importar_pack_emojis(datos, nombre)
    comun.actividad("emojis", f"Pack de emojis propio: {r['n']} emojis ({r['nuevos']} nuevos)")
    return {"ok": True, **r, "pack": f.pack_emojis()}


@app.get("/emoji/{clave}.webp", include_in_schema=False)
def emoji_imagen(clave: str, req: Request):
    """Imagen de un emoji. Las del pack 3D (MIT) no piden login; las del pack propio que subió el equipo, sí."""
    f = _mod(svc_fabrica, "fábrica")
    p = f.ruta_emoji(clave)
    if not p or not p.is_file():
        raise HTTPException(404, "No existe ese emoji.")
    if p.suffix == ".png":
        if not auth.identificar(req):
            raise HTTPException(401, "Iniciá sesión.")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "private, max-age=604800"})
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "public, max-age=604800"})


@app.post("/api/sticker_subir")
def sticker_subir(nombre: str = "sticker.png", datos: bytes = Depends(_cuerpo), q: Quien = requerir()):
    return {"ok": True, "sticker": _mod(svc_fabrica, "fábrica").subir_sticker(nombre, datos)}


@app.post("/api/copiar_subir")
def copiar_subir(que: str = "", id: str = "", nombre: str = "archivo", cuenta: str = "",
                 datos: bytes = Depends(_cuerpo), q: Quien = requerir()):
    f = _mod(svc_fabrica, "fábrica")
    if que == "ejemplo":
        return {"ok": True, "id": f.desde_archivo(nombre, datos, cuenta)}
    if que == "audio":
        return {"ok": True, "archivo": f.subir_audio(id, nombre, datos)}
    if que == "remate":
        return {"ok": True, "archivo": f.subir_remate(id, nombre, datos)}
    return {"ok": True, "archivo": f.subir_modelo(id, nombre, datos)}


@app.post("/api/copiar")
def copiar(d: dict = Depends(_json), q: Quien = requerir()):
    f = _mod(svc_fabrica, "fábrica")
    op, cid = d.get("op"), str(d.get("id", ""))
    link = str(d.get("link") or "")
    if op == "link":
        ids = f.desde_links(d.get("link"), str(d.get("cuenta") or ""))
        return {"ok": True, "id": ids[0], "ids": ids}
    if op == "cancelar":
        return {"ok": True, "n": f.cancelar()}
    if op == "guardar_links":
        return {"ok": True, "n": f.guardar_links(d.get("link"))}
    if op == "buscar":
        _mod(svc_buscador, "buscador").buscar()
        return {"ok": True}
    if op == "sumar_fuentes":
        b = _mod(svc_buscador, "buscador")
        renglones = [r for r in str(d.get("texto") or "")[:20000].splitlines() if r.strip()]
        n = b.sumar_fuentes(renglones)
        if renglones and not n and not any(re.match(r"\s*(ig|tt)\b", r, re.I) for r in renglones):
            raise ValueError("Escribí una por renglón, así: «ig @cuenta», «tt @cuenta», «ig: frase» o «ig #hashtag».")
        return {"ok": True, "n": n, "fuentes": b.fuentes()}
    if op == "fuente_busqueda":
        b = _mod(svc_buscador, "buscador")
        b.fuente(d.get("fuente"), activa=d.get("activa"), borrar=bool(d.get("borrar")))
        return {"ok": True, "fuentes": b.fuentes()}
    if op == "usar_con_marca":
        f.usar_con_marca(cid)
        return {"ok": True}
    if op == "borrar_pack_emojis":
        f.borrar_pack_emojis()
        comun.actividad("emojis", "Se volvió al pack de emojis 3D")
        return {"ok": True, "pack": f.pack_emojis()}
    if op == "borrar_sticker":
        f.borrar_sticker(str(d.get("archivo") or ""))
        return {"ok": True}
    if op == "ig_login":                 # usuario y contraseña de la cuenta «para mirar» (o los ya guardados)
        s = _mod(svc_sesion_ig, "inicio de sesión de Instagram")
        s.preparar(d.get("usuario"), d.get("clave"))
        return {"ok": True, "sesion_ig": s.iniciar()}
    if op == "ig_codigo":
        return {"ok": True, "sesion_ig": _mod(svc_sesion_ig, "inicio de sesión de Instagram").dar_codigo(d.get("codigo"))}
    if op == "ig_borrar_sesion":          # sacar del servidor la sesión de Instagram guardada (de quien sea)
        s = _mod(svc_sesion_ig, "inicio de sesión de Instagram")
        s.borrar_sesion()
        comun.actividad("sesion", "Se borró la sesión de Instagram guardada")
        return {"ok": True, "sesion_ig": s.publico(), "sesiones": svc_buscador.sesiones() if svc_buscador else {}}
    if op == "ig_cancelar":
        return {"ok": True, "sesion_ig": _mod(svc_sesion_ig, "inicio de sesión de Instagram").cancelar()}
    if op == "ig_olvidar":
        s = _mod(svc_sesion_ig, "inicio de sesión de Instagram")
        s.olvidar()
        return {"ok": True, "sesion_ig": s.publico()}
    if op == "importar_cookies":
        b = _mod(svc_buscador, "buscador")
        n = b.importar_cookies(str(d.get("texto") or "")[:200_000])
        return {"ok": True, "n": n, "redes": [r.title() for r, ok in b.estado_sesion().items() if ok]}
    if op == "votar":
        f.votar(link, d.get("voto"), d.get("motivo"))
        return {"ok": True}
    if op == "previa":
        return {"ok": True, "url": almacen.relativa(f.previa(link))}
    if op == "borrar_link":
        f.borrar_link(link)
        return {"ok": True}
    if op == "hacer_guardados":
        ids = f.hacer_guardados(d.get("n"), str(d.get("cuenta") or ""))
        return {"ok": True, "id": ids[0], "ids": ids}
    if op == "armar":
        f.armar(cid, d)
        return {"ok": True}
    if op == "borrar":
        f.borrar(cid)
        return {"ok": True}
    if op in ("modelo_material", "audio_material"):
        m = _mod(svc_material, "material")
        cuenta = _cuenta(str(d.get("cuenta", "")))
        if op == "modelo_material":
            return {"ok": True, "archivo": f.modelo_de(cid, m.ruta(cuenta, str(d.get("sec", "")), str(d.get("nombre", ""))))}
        sec = "ejemplos" if d.get("sec") == "ejemplos" else "musica"
        return {"ok": True, "archivo": f.audio_de(cid, m.ruta(cuenta, sec, str(d.get("nombre", ""))), d.get("titulo") or "")}
    if op == "ubicar":
        return f.ubicar(cid, d)
    if op == "borrar_version":
        f.borrar_version(cid, str(d.get("archivo", "")))
        return {"ok": True}
    if op == "a_bandeja":
        f.a_bandeja(cid, _cuenta(str(d.get("cuenta", ""))), str(d.get("archivo") or ""))
        return {"ok": True}
    raise HTTPException(400, "op")


@app.post("/api/repartir")
def repartir(d: dict = Depends(_json), q: Quien = requerir()):
    r = _mod(svc_repartir, "repartir")
    return {"ok": True, **r.repartir(str(d.get("dia") or ""), usuario=q.firma)}


@app.post("/api/encargo")
def encargo(d: dict = Depends(_json), q: Quien = requerir()):
    r = _mod(svc_repartir, "repartir")
    return {"ok": True, **r.encargo(str(d.get("dia") or ""), d.get("reels", 3), d.get("historias", 2), usuario=q.firma)}


# --- archivos -----------------------------------------------------------------------------------------------------------------------
def _visible(p: str) -> Path:
    try:
        f = almacen.ruta(p)
    except ValueError:
        raise HTTPException(404, "no existe")
    raiz = almacen.raiz()
    if not f.is_file() or not any(f.is_relative_to(raiz / almacen.SECCIONES[s]) for s in SECCIONES_VISIBLES):
        raise HTTPException(404, "no existe")
    return f


@app.get("/media")
def media(p: str, descargar: int = 0, q: Quien = requerir()):
    f = _visible(p)
    return FileResponse(f, filename=f.name if descargar else None,
                        content_disposition_type="attachment" if descargar else "inline",
                        headers={"Cache-Control": "private, max-age=60"})


@app.get("/mini")
def mini(p: str, q: Quien = requerir()):
    m = medios.miniatura(_visible(p), almacen.dir_de("minis"))
    if not m:
        raise HTTPException(404, "sin miniatura")
    return FileResponse(m, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.api_route("/publico/{rel:path}", methods=["GET", "HEAD"], include_in_schema=False)
def publico(rel: str, v: str = "", f: str = ""):
    """Lo baja Meta (sin login): solo con firma válida y vigente."""
    p = almacen.verificar_publica(rel, v, f)
    if not p:
        raise HTTPException(404, "no existe")
    tipo = "video/mp4" if medios.es_video(p) else "image/jpeg"
    return FileResponse(p, media_type=tipo, headers={"Cache-Control": "no-store"})


# --- Fuentes (sección Fuentes) -------------------------------------------------------------------------------------------------
def _fuentes_estado() -> dict:
    from . import fuentes
    return {"fuentes": fuentes.lista(), "claude": fuentes.claude_elige(), "predeterminada": fuentes.predeterminada(),
            "catalogo_total": fuentes.catalogo(por_pagina=1)["total"]}


@app.get("/api/fuentes_catalogo")
def fuentes_catalogo(q: str = "", pagina: int = 0, q_: Quien = requerir()):
    from . import fuentes
    return fuentes.catalogo(q, pagina)


@app.get("/api/fuentes")
def fuentes_get(q: Quien = requerir()):
    return _fuentes_estado()


@app.get("/api/fuente")
def fuente_archivo(k: str, q: Quien = requerir()):
    """El archivo de una fuente, para que el panel la muestre tal cual queda en el video."""
    from . import fuentes
    if not fuentes.existe_alguna(k):
        raise HTTPException(404, "no existe")
    p = fuentes.ruta(k)
    if not p:
        raise HTTPException(404, "no existe")
    return FileResponse(p, media_type="font/otf" if p.suffix.lower() == ".otf" else "font/ttf",
                        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.post("/api/fuentes_subir")
def fuentes_subir(nombre: str = "fuente.ttf", datos: bytes = Depends(_cuerpo), q: Quien = requerir("editor")):
    from . import fuentes
    if len(datos) > fuentes.MAX_ZIP_TOTAL:
        raise HTTPException(400, "El archivo pesa más de 80 MB.")
    r = fuentes.subir(nombre, datos, por=q.nombre or q.email)
    if r["nuevas"]:
        comun.actividad("fuentes", f"{len(r['nuevas'])} fuentes nuevas: " + ", ".join(f["nombre"] for f in r["nuevas"][:4]))
    return {"ok": True, **r}


@app.post("/api/fuentes")
def fuentes_post(d: dict = Depends(_json), q: Quien = requerir("editor")):
    from . import fuentes
    op, k = d.get("op"), str(d.get("clave") or "")
    if op == "borrar":
        fuentes.borrar(k)
    elif op in ("mostrar", "agregar"):
        fuentes.agregar(k)
    elif op == "predeterminada":
        fuentes.elegir_predeterminada(k)
    elif op == "renombrar":
        fuentes.renombrar(k, str(d.get("nombre") or ""))
    elif op == "claude":
        fuentes.poner_claude(bool(d.get("valor")))
    else:
        raise HTTPException(400, "op")
    return _fuentes_estado()


# --- Pyro (Ajustes → Pyro) ------------------------------------------------------------------------------------------------------------
@app.get("/api/pyro")
def pyro_estado(q: Quien = requerir()):
    from .servicios import pyro as svc_pyro
    return svc_pyro.estado()


@app.post("/api/pyro")
def pyro_post(d: dict = Depends(_json), q: Quien = requerir()):
    from .servicios import pyro as svc_pyro
    from .trabajos import encolar
    op = d.get("op")
    admin = auth.NIVEL[q.rol] >= auth.NIVEL["admin"]
    if op in ("guardar", "desconectar", "material_auto") and not admin:
        raise HTTPException(403, "Solo un admin cambia la conexión con Pyro.")
    if op == "guardar":
        r = svc_pyro.guardar(str(d.get("url") or ""), str(d.get("clave") or ""))
        return {**r, **svc_pyro.estado()}
    if op == "desconectar":
        svc_pyro.desconectar()
    elif op == "material_auto":
        comun.guardar_ajuste("pyro_material_auto", bool(d.get("valor")))
    elif op == "probar":
        return svc_pyro.probar()
    elif op == "sincronizar":
        return {**svc_pyro.sincronizar(), **svc_pyro.estado()}
    elif op == "material":
        cid = svc_cuentas.modelo_de(_cuenta(str(d.get("cuenta") or "")))
        if not svc_pyro.conectado():
            raise ValueError("Pyro no está conectado.")
        if not svc_cuentas.obtener(cid).pyro_modelo_id:
            raise ValueError("Esta modelo no está atada a Pyro (sincronizá en Ajustes → Pyro).")
        import time
        encolar("pyro_material", {"cuenta": cid}, clave=f"pyro_material:{cid}:pedido:{int(time.time() // 60)}")
        return {"ok": True, "encolado": True}
    elif op == "atar":
        svc_pyro.atar(_cuenta(str(d.get("cuenta") or "")), d.get("modelo_id"))
    elif op == "crear_cuenta":
        cid = svc_pyro.crear_cuenta(str(d.get("usuario") or ""), d.get("modelo_id"))
        return {"ok": True, "id": cid, **svc_pyro.estado()}
    else:
        raise HTTPException(400, "op")
    return {"ok": True, **svc_pyro.estado()}


# --- usuarios y tokens (admin) -------------------------------------------------------------------------------------------------------
@app.post("/api/probar_claude")
def probar_claude(d: dict = Depends(_json), q: Quien = requerir("admin")):
    """Ajustes → Probar conexión: confirma que la API key funciona (un pedido mínimo)."""
    from .ia.motor_api import probar_conexion
    return probar_conexion()


@app.get("/api/usuarios")
def usuarios(q: Quien = requerir("admin")):
    with sesion() as s:
        us = [{"id": u.id, "email": u.email, "nombre": u.nombre, "rol": u.rol, "activo": u.activo}
              for u in s.scalars(select(Usuario).order_by(Usuario.id))]
        ts = [{"id": t.id, "nombre": t.nombre, "prefijo": t.prefijo, "rol": t.rol, "revocado": t.revocado,
               "ultimo_uso": comun.iso_local(t.ultimo_uso)} for t in s.scalars(select(TokenAPI).order_by(TokenAPI.id))]
    return {"usuarios": us, "tokens": ts}


def _id_entero(v) -> int:
    """Un id que viene del pedido: entero chico o 0 (una lista o un número gigante no llegan a la base)."""
    if isinstance(v, bool):
        return 0
    try:
        n = int(v) if isinstance(v, (int, str)) else 0
    except ValueError:
        return 0
    return n if 0 < n < 2**31 else 0


@app.post("/api/usuarios")
def usuarios_post(d: dict = Depends(_json), q: Quien = requerir("admin")):
    op = d.get("op")
    if op == "crear":
        return {"ok": True, "id": auth.crear_usuario(str(d.get("email", "")), str(d.get("clave", "")),
                                                     str(d.get("nombre", "")), str(d.get("rol") or "editor"))}
    if op == "desactivar":
        with sesion() as s:
            u = s.get(Usuario, _id_entero(d.get("id")))
            if not u:
                raise ValueError("Usuario desconocido.")
            if u.id == q.id:
                raise ValueError("No te podés desactivar a vos misma.")
            u.activo = False
        return {"ok": True}
    if op == "token":
        return {"ok": True, "token": auth.crear_token(str(d.get("nombre") or "integración"), str(d.get("rol") or "editor"))}
    if op == "revocar_token":
        with sesion() as s:
            t = s.get(TokenAPI, _id_entero(d.get("id")))
            if not t:
                raise ValueError("Token desconocido.")
            t.revocado = True
        return {"ok": True}
    raise HTTPException(400, "op")


@app.get("/api/tareas_ia")
def tareas_ia(q: Quien = requerir("admin")):
    """Para controlar costo y errores de Claude."""
    from .modelos import TareaIA
    with sesion() as s:
        ts = s.scalars(select(TareaIA).order_by(TareaIA.creado.desc()).limit(100)).all()
        return {"tareas": [{"id": t.id, "tipo": t.tipo, "estado": t.estado, "motor": t.motor, "referencia": t.referencia,
                            "error": t.error, "uso": t.uso, "creado": comun.iso_local(t.creado),
                            "terminado": comun.iso_local(t.terminado)} for t in ts]}
