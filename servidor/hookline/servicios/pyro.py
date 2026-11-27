"""Conexión con Pyro (Model Pro): ata cada cuenta de Hookline a su modelo de Pyro y trae su material.

- Se conecta con la dirección de Pyro y una clave de su API de solo lectura (`pyro_...`, Pyro → Accesos → API de Pyro).
  Las dos se cargan en Ajustes (la clave queda cifrada y nunca vuelve) o por variables HOOKLINE_PYRO_URL y
  HOOKLINE_PYRO_CLAVE (mandan sobre Ajustes).
- La clave solo va a donde se pegó: si cambia la dirección, hay que volver a pegar la clave.
- Sincronizar (cada 6 horas, o con el botón): lee las modelos de Pyro y sus cuentas de Instagram y ata cada cuenta de
  Hookline (por su usuario) a su modelo (`pyro_modelo_id`). Las multicuentas sin usuario en Pyro heredan la modelo de su
  main. Lo que Pyro no tiene queda como está (una atadura a mano se respeta mientras la modelo exista en Pyro).
- Traer material (una vez por día, o con el botón): baja la biblioteca de fotos y videos de la modelo a su Material,
  respetando las carpetas de Pyro. No baja dos veces el mismo archivo (se anota su id de Pyro).
- Nunca escribe en Pyro: la API de Pyro es de solo lectura. Pyro se entera de lo que pasa en Hookline por los
  webhooks (eventos.py), que llevan `pyro_modelo_id`.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from sqlalchemy import select

from .. import almacen, medios
from .. import pyro as cli
from ..comun import actividad, ajuste, guardar_ajuste, iso_local
from ..db import ahora, sesion
from ..modelos import Cuenta, MaterialMeta
from ..trabajos import encolar, manejador
from . import cuentas as svc_cuentas

log = logging.getLogger("hookline.pyro")
MAX_POR_VUELTA = 300           # archivos que se bajan por vuelta (el resto, en la siguiente)
PAUSA = 0.5                    # entre pedidos (Pyro deja 120 por minuto por clave)


# --- conexión ----------------------------------------------------------------------------------------------------
def conexion() -> tuple[str, str]:
    """(url, clave). Las variables de entorno mandan sobre Ajustes."""
    url = os.environ.get("HOOKLINE_PYRO_URL", "").strip() or str(ajuste("pyro_url", "") or "")
    clave = os.environ.get("HOOKLINE_PYRO_CLAVE", "").strip() or str(ajuste("pyro_api_key", "") or "")
    return (cli.normalizar_url(url) if url else ""), clave


def conectado() -> bool:
    try:
        url, clave = conexion()
        return bool(url and clave)
    except ValueError:
        return False


# --- mayor de edad verificada por la agencia -----------------------------------------------------------------------
EDAD_CACHE = 300               # segundos que vale una respuesta de Pyro (así una desmarca llega rápido)
EDAD_CACHE_ERROR = 60          # si Pyro no contestó: se vuelve a probar al minuto (mientras tanto, «no verificada»)
_edad: dict[int, tuple[float, bool, float]] = {}
_edad_lock = threading.Lock()


def edad_verificada(cuenta: str) -> bool:
    """¿Un admin de la agencia marcó en Pyro que la modelo de esta cuenta es mayor de edad, con su documento revisado
    (ageVerified.verified = true en /api/v1/models/{id})? Se pregunta a Pyro en vivo (con 5 min de memoria). Las
    multicuentas usan la modelo de su main. Ante cualquier duda (sin Pyro, sin modelo atada, error, forma rara): False.
    Solo sirve para las fotos de ESA modelo; nunca para otras personas."""
    if not cuenta:
        return False
    try:
        with sesion() as s:
            c = s.get(Cuenta, cuenta)
            mid = c.pyro_modelo_id if c else None
            if not mid and c and c.main_id:
                m = s.get(Cuenta, c.main_id)
                mid = m.pyro_modelo_id if m else None
        url, clave = conexion()
    except Exception:
        return False
    if not mid or not url or not clave:
        return False
    ya = time.monotonic()
    with _edad_lock:
        hit = _edad.get(mid)
        if hit and ya - hit[0] < hit[2]:
            return hit[1]
    try:
        ficha = cli.modelo(url, clave, mid)
        av = ficha.get("ageVerified")
        ok = isinstance(av, dict) and av.get("verified") is True and ficha.get("id") == mid
        dura = EDAD_CACHE
    except Exception as ex:
        log.info("pyro: no pude preguntar si la modelo %s está verificada: %s", mid, ex)
        ok, dura = False, EDAD_CACHE_ERROR
    with _edad_lock:
        _edad[mid] = (time.monotonic(), ok, dura)
    return ok


def estado() -> dict:
    try:
        url, clave = conexion()
        err_conf = ""
    except ValueError as ex:
        url, clave, err_conf = "", "", str(ex)
    e = ajuste("pyro_estado", {}) or {}
    with sesion() as s:
        atadas = len(s.scalars(select(Cuenta.id).where(Cuenta.pyro_modelo_id.is_not(None))).all())
        existentes = {cli.limpiar_handle(x) for fila in s.execute(select(Cuenta.id, Cuenta.usuario_ig)) for x in fila if x}
    # lo que Pyro tiene y acá no: se filtra con las cuentas de AHORA (si la crearon recién, ya no aparece)
    sin_cuenta = [x for x in e.get("sin_cuenta", []) or []
                  if isinstance(x, dict) and cli.limpiar_handle(x.get("usuario")) not in existentes]
    return {"conectado": bool(url and clave), "url": url, "clave_fin": clave[-4:] if clave else "",
            "por_entorno": bool(os.environ.get("HOOKLINE_PYRO_URL") or os.environ.get("HOOKLINE_PYRO_CLAVE")),
            "material_auto": bool(ajuste("pyro_material_auto", True)),
            "ultima": e.get("ultima", ""), "error": err_conf or e.get("error", ""), "modelos": e.get("modelos", []),
            "sin_cuenta": sin_cuenta, "atadas": atadas,
            "material": ajuste("pyro_material_estado", {}) or {}}


def guardar(url: str, clave: str = "") -> dict:
    """Guarda la conexión (y la prueba). Sin clave nueva, se mantiene la guardada solo si la dirección no cambió."""
    url = cli.normalizar_url(url)
    actual_url = str(ajuste("pyro_url", "") or "")
    actual_clave = str(ajuste("pyro_api_key", "") or "")
    if clave:
        clave = cli.validar_clave(clave)
    elif actual_clave and actual_url == url:
        clave = actual_clave
    else:
        raise cli.ErrorPyro("Pegá la clave de la API de Pyro (si cambia la dirección, la clave se vuelve a pegar).")
    ms = cli.modelos(url, clave)                                  # la prueba: si no anda, no se guarda nada
    guardar_ajuste("pyro_url", url)
    guardar_ajuste("pyro_api_key", clave, cifrado=True)
    encolar("pyro_sincronizar", {}, clave=f"pyro_sincronizar:conectar:{int(time.time())}", max_intentos=1)
    return {"ok": True, "n_modelos": len(ms)}


def probar() -> dict:
    url, clave = conexion()
    if not (url and clave):
        raise cli.ErrorPyro("Primero cargá la dirección y la clave de Pyro.")
    t = time.perf_counter()
    ms = cli.modelos(url, clave)
    return {"ok": True, "modelos": len(ms), "ms": round((time.perf_counter() - t) * 1000)}


def desconectar() -> None:
    """Borra la clave. Las cuentas siguen atadas a su modelo (el puente de Pyro lo necesita para filtrar)."""
    guardar_ajuste("pyro_api_key", None)
    guardar_ajuste("pyro_estado", {})


# --- sincronizar modelos ---------------------------------------------------------------------------------------
def _usuario(c: Cuenta) -> str:
    return cli.limpiar_handle(c.usuario_ig or c.id)


def sincronizar() -> dict:
    url, clave = conexion()
    if not (url and clave):
        raise cli.ErrorPyro("Pyro no está conectado.")
    try:
        ms = cli.modelos(url, clave)
        fichas = {}
        for m in ms:
            fichas[m["id"]] = cli.modelo(url, clave, m["id"])
            time.sleep(PAUSA)
    except cli.ErrorPyro as ex:
        e = ajuste("pyro_estado", {}) or {}
        guardar_ajuste("pyro_estado", {**e, "error": str(ex), "ultima": iso_local(ahora())})
        raise
    nombres = {m["id"]: str(m.get("name") or m.get("alias") or f"Modelo {m['id']}")[:200] for m in ms}
    por_handle: dict[str, int] = {}
    for mid, f in fichas.items():
        for h in cli.handles_instagram(f):
            por_handle.setdefault(h, mid)
    cambios, atadas = [], 0
    with sesion() as s:
        cs = s.scalars(select(Cuenta).order_by(Cuenta.id)).all()
        usados = set()
        for c in cs:                                            # 1) por el usuario de Instagram
            mid = por_handle.get(_usuario(c))
            if mid:
                usados.add(_usuario(c))
            elif c.pyro_modelo_id in nombres:                   # una atadura a mano, si la modelo sigue existiendo
                mid = c.pyro_modelo_id
            if mid != c.pyro_modelo_id:
                cambios.append((c.id, c.pyro_modelo_id, mid))
            c.pyro_modelo_id, c.pyro_modelo = mid, nombres.get(mid, "") if mid else ""
            if mid and not c.nicho and fichas[mid].get("niche"):
                c.nicho = str(fichas[mid]["niche"])[:300]
        por_id = {c.id: c for c in cs}
        for c in cs:                                            # 2) las multicuentas heredan la modelo de su main
            m = por_id.get(c.main_id or "")
            if not c.pyro_modelo_id and m and m.pyro_modelo_id:
                cambios.append((c.id, None, m.pyro_modelo_id))
                c.pyro_modelo_id, c.pyro_modelo = m.pyro_modelo_id, m.pyro_modelo
        atadas = sum(1 for c in cs if c.pyro_modelo_id)
    sin_cuenta = [{"modelo_id": mid, "modelo": nombres.get(mid, ""), "usuario": h}
                  for h, mid in sorted(por_handle.items()) if h not in usados]
    resumen = {"ultima": iso_local(ahora()), "error": "", "atadas": atadas, "sin_cuenta": sin_cuenta,
               "modelos": [{"id": mid, "nombre": nombres[mid], "instagram": cli.handles_instagram(fichas[mid])}
                           for mid in sorted(nombres, key=lambda i: nombres[i].lower())]}
    guardar_ajuste("pyro_estado", resumen)
    for cid, antes, despues in cambios:
        actividad("pyro", f"@{cid} quedó atada a {nombres.get(despues, 'ninguna modelo')} de Pyro" if despues
                  else f"@{cid} ya no está atada a una modelo de Pyro", cuenta=cid)
    return {"ok": True, "atadas": atadas, "cambios": len(cambios), "sin_cuenta": len(sin_cuenta)}


def atar(cid: str, modelo_id) -> None:
    """Atadura a mano (Cuentas → Editar). None o 0 la suelta."""
    mid = int(modelo_id or 0) or None
    nombres = {m["id"]: m["nombre"] for m in (ajuste("pyro_estado", {}) or {}).get("modelos", [])}
    if mid and mid not in nombres:
        raise ValueError("Esa modelo no está en Pyro (sincronizá primero).")
    with sesion() as s:
        c = s.get(Cuenta, cid)
        if not c:
            raise ValueError("Cuenta desconocida.")
        c.pyro_modelo_id, c.pyro_modelo = mid, nombres.get(mid, "") if mid else ""


def crear_cuenta(usuario: str, modelo_id) -> str:
    """Crea (sin conectar) la cuenta de Instagram que Pyro tiene y Hookline no, ya atada a su modelo."""
    cid = svc_cuentas.crear_local(usuario)
    atar(cid, modelo_id)
    return cid


# --- material -------------------------------------------------------------------------------------------------------
def _seccion(carpeta: dict | None, carpetas: dict) -> str:
    """La carpeta de Pyro ('Playa / Junio') → una carpeta de Material ('Playa - Junio'). Sin carpeta: 'Pyro'."""
    path = str((carpeta or {}).get("path") or (carpeta or {}).get("name") or "")
    nombre = re.sub(r"\s*/\s*", " - ", path)
    nombre = re.sub(r"\s+", " ", re.sub(r"[^\w áéíóúñÁÉÍÓÚÑ\-]", "", nombre)).strip(" -")[:40]
    if not nombre or nombre.startswith(("_", ".")) or nombre.lower() in ("modelo", "ejemplos", "musica", "general"):
        return "Pyro" if not nombre else f"Pyro - {nombre}"[:40]
    return nombre


def _extension(a: dict) -> str:
    ext = Path(str(a.get("name") or "")).suffix.lower()
    if ext in medios.FOTOS | medios.VIDEOS:
        return ext
    mt = str(a.get("mimetype") or "").lower()
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/heic": ".heic",
            "video/mp4": ".mp4", "video/quicktime": ".mov"}.get(mt, "")


def importar_material(cuenta: str, maximo: int = MAX_POR_VUELTA) -> dict:
    from . import material as svc_material
    url, clave = conexion()
    if not (url and clave):
        raise cli.ErrorPyro("Pyro no está conectado.")
    modelo = svc_cuentas.modelo_de(cuenta)
    c = svc_cuentas.obtener(modelo)
    if not c.pyro_modelo_id:
        raise cli.ErrorPyro("Esta modelo no está atada a Pyro (sincronizá en Ajustes → Pyro).")
    carpetas, archivos = cli.biblioteca(url, clave, c.pyro_modelo_id)
    with sesion() as s:
        ya = {str((m.meta or {}).get("pyro_id")) for m in
              s.scalars(select(MaterialMeta).where(MaterialMeta.modelo == modelo)) if (m.meta or {}).get("pyro_id")}
    nuevos = bajados = errores = 0
    for a in archivos:
        if a.get("kind") not in ("image", "video") or str(a["id"]) in ya:
            continue
        ext = _extension(a)
        if not ext:
            continue
        nuevos += 1
        if bajados >= maximo:
            continue
        sec = _seccion(carpetas.get(a.get("folderId")), carpetas)
        dest_dir = svc_material._base(modelo) / sec
        dest_dir.mkdir(parents=True, exist_ok=True)
        nombre = almacen.nombre_seguro(Path(str(a.get("name") or f"pyro_{a['id']}")).stem + ext)
        destino = almacen.libre(dest_dir / nombre)
        try:
            cli.bajar(url, clave, str(a.get("url") or ""), destino)
        except cli.ErrorPyro as ex:
            errores += 1
            log.warning("pyro material %s: %s", a.get("id"), ex)
            continue
        texto = f"{sec} {a.get('name', '')} {a.get('caption', '')}".lower()
        svc_material.poner_meta(modelo, sec, destino.name, etiquetas=["noche"] if "noche" in texto else [],
                                de="pyro", pyro_id=a["id"], pyro_carpeta=str((carpetas.get(a.get("folderId")) or {})
                                                                              .get("path") or ""),
                                rating=a.get("rating"), titulo=str(a.get("caption") or "")[:300])
        bajados += 1
        time.sleep(PAUSA / 5)
    r = {"cuenta": modelo, "bajados": bajados, "faltan": max(0, nuevos - bajados - errores), "errores": errores,
         "fecha": iso_local(ahora())}
    est = ajuste("pyro_material_estado", {}) or {}
    guardar_ajuste("pyro_material_estado", {**est, modelo: r})
    if bajados:
        actividad("pyro", f"Pyro: {bajados} archivos nuevos en el material de @{modelo}", cuenta=modelo)
    return r


# --- trabajos ------------------------------------------------------------------------------------------------------
@manejador("pyro_sincronizar")
def _t_sincronizar(d: dict) -> None:
    if conectado():
        sincronizar()


@manejador("pyro_material")
def _t_material(d: dict) -> None:
    if conectado():
        r = importar_material(d["cuenta"])
        if r["faltan"]:                                         # quedó más: sigue en un rato
            encolar("pyro_material", {"cuenta": d["cuenta"]}, clave=f"pyro_material:{d['cuenta']}:{time.time():.0f}")


def periodicas(hora: str, dia: str) -> None:
    if not conectado():
        return
    if int(hora[-2:]) % 6 == 0:
        encolar("pyro_sincronizar", {}, clave=f"pyro_sincronizar:{hora}", max_intentos=1)
    if ajuste("pyro_material_auto", True):
        with sesion() as s:
            mains = [c for (c,) in s.execute(select(Cuenta.id).where(Cuenta.pyro_modelo_id.is_not(None),
                                                                       Cuenta.main_id.is_(None)))]
        for cid in mains:
            encolar("pyro_material", {"cuenta": cid}, clave=f"pyro_material:{cid}:{dia}")
