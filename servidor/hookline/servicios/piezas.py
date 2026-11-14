"""Bandeja (piezas) y cola de publicación.

Reglas que vienen de Hookline Lite y se mantienen:
  - Nada se publica sin aprobación: solo `programar` (o `Publicar ahora`) pasa una pieza a 'programada'.
  - La subida arranca 8 min antes de la hora; Instagram procesa y se publica a la hora exacta.
  - Si pasó la hora por más de 15 min sin que se haya podido arrancar (worker caído), queda 'vencida' y NO se publica
    sola tarde: la dueña la reprograma o toca Publicar ahora.
Nuevo: cada publicación es un trabajo con clave única (publicar:<pieza>:<hora>), y la pieza guarda el creation_id y el
media_id: si el worker se reinicia en el medio, no se publica dos veces.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import timedelta

from sqlalchemy import select, update

from .. import almacen, instagram as ig, medios
from ..comun import actividad, aviso, desde_local, evento, iso_local
from ..db import ahora, sesion
from ..modelos import Cuenta, Pieza, Trabajo
from ..trabajos import encolar, latido, manejador
from . import cuentas as svc_cuentas

log = logging.getLogger("hookline.piezas")

TIPOS = ("historia", "reel", "carrusel", "foto")
ANTICIPO = timedelta(minutes=8)
TOLERANCIA = timedelta(minutes=15)
BLOQUEADAS = ("subiendo", "publicada")
_lock = threading.RLock()
_ultimo_id = [0]


def nuevo_id() -> str:
    with _lock:
        n = max(int(time.time() * 1000), _ultimo_id[0] + 1)
        _ultimo_id[0] = n
        return f"p{n}"


def tipo_por_archivo(p) -> str:
    return "reel" if medios.es_video(p) else "foto"


def como_dict(x: Pieza) -> dict:
    import urllib.parse
    return {"id": x.id, "cuenta": x.cuenta_id, "tipo": x.tipo, "archivos": list(x.archivos or []), "estado": x.estado,
            "creado": iso_local(x.creado), "caption": x.caption, "cuando": iso_local(x.cuando), "ahora": x.ahora,
            "etapa": x.etapa, "error": x.error, "jueces": x.jueces, "sugerido": x.sugerido,
            "pedido_jueces": iso_local(x.pedido_jueces), "aprobada": iso_local(x.aprobada), "aprobada_por": x.aprobada_por,
            "inicio": iso_local(x.inicio), "media_id": x.media_id, "permalink": x.permalink,
            "publicado": iso_local(x.publicado), "subida": x.subida, "plan": x.plan, "dia": x.dia, "copia": x.copia_id,
            "foto": x.foto, "vistas_previas": [f"/mini?p={urllib.parse.quote(a)}" for a in (x.archivos or [])]}


def listar(cuenta: str | None = None) -> list[dict]:
    with sesion() as s:
        q = select(Pieza).order_by(Pieza.creado)
        if cuenta:
            q = q.where(Pieza.cuenta_id == cuenta)
        return [como_dict(x) for x in s.scalars(q)]


def obtener(pid: str) -> dict:
    with sesion() as s:
        x = s.get(Pieza, str(pid))
        if not x:
            raise ValueError("Esa pieza ya no está en la bandeja.")
        return como_dict(x)


def validar_tipo(tipo: str, archivos: list[str]) -> str:
    fotos = [a for a in archivos if medios.es_foto(a)]
    videos = [a for a in archivos if medios.es_video(a)]
    if tipo == "reel" and not (len(archivos) == 1 and videos):
        return "Un Reel es un solo video."
    if tipo == "foto" and not (len(archivos) == 1 and fotos):
        return "Una publicación de foto es una sola foto."
    if tipo == "historia" and len(archivos) != 1:
        return "Una historia es una sola foto o un solo video."
    if tipo == "carrusel" and not 2 <= len(archivos) <= 10:
        return "Un carrusel lleva entre 2 y 10 fotos."
    return ""


def dir_cuenta(cid: str):
    return almacen.dir_de("subir", cid)


# --- entrada ------------------------------------------------------------------------------------------------------------
def guardar_subida(cuenta: str, nombre: str, datos: bytes) -> str:
    if not svc_cuentas.existe(cuenta):
        raise ValueError("Elegí una cuenta primero.")
    limpio = almacen.nombre_seguro(nombre)
    if not (medios.es_foto(limpio) or medios.es_video(limpio)):
        raise ValueError("Solo fotos (jpg, png, webp, heic) o videos (mp4, mov).")
    destino = almacen.libre(dir_cuenta(cuenta) / ("." + limpio))   # oculto: el escaneo no lo toma (no se duplica)
    destino.write_bytes(datos)
    agregar_archivo(cuenta, destino)
    return destino.name


def copiar_a_bandeja(cuenta: str, src, nombre: str, **extra) -> str:
    """Copia un archivo del sistema (fábrica, material) a la bandeja y crea su pieza en el mismo paso."""
    import shutil
    destino = almacen.libre(dir_cuenta(cuenta) / ("." + almacen.nombre_seguro(nombre)))
    shutil.copyfile(src, destino)
    return agregar_archivo(cuenta, destino, **extra)


def agregar_archivo(cuenta: str, ruta_abs, **extra) -> str:
    """Una pieza nueva a partir de un archivo que ya está en para-subir/<cuenta>/ (lo usan la fábrica y el material)."""
    rel = almacen.relativa(ruta_abs)
    with sesion() as s:
        x = Pieza(id=nuevo_id(), cuenta_id=cuenta, tipo=extra.pop("tipo", None) or tipo_por_archivo(rel),
                  archivos=[rel], **extra)
        s.add(x)
        return x.id


def escanear() -> int:
    """Archivos que alguien dejó a mano en para-subir/<cuenta>/ → una pieza cada uno. Archivos borrados → se sacan
    de su pieza. Los que agrega el sistema van con nombre oculto (".x.jpg") y ya traen su pieza: acá se ignoran.
    Un candado entre procesos evita escanear a la vez que otro escaneo o que repartir (piezas repetidas)."""
    from ..comun import candado
    nuevos_n = 0
    with candado("bandeja"), sesion() as s:
        lista = s.scalars(select(Pieza)).all()
        usados = {a for x in lista for a in (x.archivos or [])}
        for (cid,) in s.execute(select(Cuenta.id)).all():
            d = dir_cuenta(cid)
            for f in sorted((f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")
                             and (medios.es_foto(f) or medios.es_video(f))),
                            key=lambda f: f.stat().st_mtime):
                r = almacen.relativa(f)
                if r in usados:
                    continue
                s.add(Pieza(id=nuevo_id(), cuenta_id=cid, tipo=tipo_por_archivo(f), archivos=[r]))
                usados.add(r)
                nuevos_n += 1
        for x in lista:
            if x.estado in BLOQUEADAS:
                continue
            vivos = [a for a in (x.archivos or []) if almacen.ruta(a).exists()]
            if vivos != (x.archivos or []):
                if vivos:
                    x.archivos = vivos
                else:
                    s.delete(x)
    return nuevos_n


# --- acciones de la bandeja ---------------------------------------------------------------------------------------------
def accion(d: dict, usuario: str = "") -> dict:
    from sqlalchemy.orm.exc import StaleDataError
    try:
        return _accion(d, usuario)
    except StaleDataError:
        raise ValueError("La pieza cambió mientras tanto (¿se está publicando?): recargá y probá de nuevo.")


def _accion(d: dict, usuario: str = "") -> dict:
    op, pid = d.get("op"), str(d.get("id", ""))
    if op == "jueces":
        from .jueces import pedir_jueces
        return pedir_jueces(pid)
    programar_ya = False
    with _lock, sesion() as s:
        if op == "juntar":
            ids = list(dict.fromkeys(str(i) for i in (d.get("ids") if isinstance(d.get("ids"), list) else [])))
            sel = [s.get(Pieza, i) for i in ids]
            if len(sel) < 2 or any(x is None for x in sel):
                raise ValueError("Elegí al menos 2 fotos de la bandeja.")
            if len({x.cuenta_id for x in sel}) != 1:
                raise ValueError("Todas tienen que ser de la misma cuenta.")
            if any(x.estado in ("programada", "subiendo", "publicada") for x in sel):
                raise ValueError("Hay alguna ya programada o publicada: cancelala primero.")
            archivos = [a for x in sel for a in x.archivos]
            if len(archivos) > 10:
                raise ValueError("Un carrusel lleva hasta 10 fotos.")
            base = sel[0]
            base.tipo, base.archivos, base.estado, base.jueces = "carrusel", archivos, "nueva", None
            for x in sel[1:]:
                s.delete(x)
            s.flush()
            return como_dict(base)
        x = s.get(Pieza, pid)
        if not x:
            raise ValueError("Esa pieza ya no está en la bandeja.")
        bloqueada = x.estado in BLOQUEADAS
        if op == "tipo":
            t = d.get("tipo")
            if t not in TIPOS:
                raise ValueError("Tipo desconocido.")
            if bloqueada or x.estado == "programada":
                raise ValueError("Ya está programada: cancelala para cambiar el tipo.")
            err = validar_tipo(t, x.archivos)
            if err:
                raise ValueError(err)
            x.tipo = t
        elif op == "orden":
            nuevo = [str(a) for a in d.get("archivos") or []]
            if sorted(nuevo) != sorted(x.archivos):
                raise ValueError("El orden no coincide con los archivos.")
            if bloqueada:
                raise ValueError("Ya se publicó.")
            x.archivos = nuevo
        elif op == "separar":
            a = str(d.get("archivo", ""))
            if a not in x.archivos or bloqueada or len(x.archivos) < 2:
                raise ValueError("No se puede separar.")
            x.archivos = [y for y in x.archivos if y != a]
            s.add(Pieza(id=nuevo_id(), cuenta_id=x.cuenta_id, tipo=tipo_por_archivo(a), archivos=[a]))
            if len(x.archivos) == 1:
                x.tipo = tipo_por_archivo(x.archivos[0])
        elif op == "caption":
            if bloqueada:
                raise ValueError("Ya se está subiendo o ya salió.")
            x.caption = str(d.get("caption") or "")[:2200]
        elif op == "programar":
            _programar(x, d, usuario)
            programar_ya = True
        elif op == "cancelar":
            if x.estado == "subiendo":
                raise ValueError("Ya se está subiendo: no se puede cancelar.")
            if x.estado == "publicada":
                raise ValueError("Ya se publicó.")
            x.estado = "juzgada" if x.jueces or x.plan else "nueva"
            x.ahora, x.error, x.etapa = False, "", ""
        elif op == "borrar":
            if x.estado == "subiendo":
                raise ValueError("Se está subiendo: esperá a que termine.")
            if x.estado != "publicada":  # los archivos van a una carpeta aparte: no se borra nada de verdad
                destino = dir_cuenta(x.cuenta_id) / "_descartados"
                for a in x.archivos:
                    p = almacen.ruta(a)
                    if p.exists():
                        almacen.mover(p, destino)
            s.delete(x)
            return {"ok": True}
        else:
            raise ValueError("Acción desconocida.")
        s.flush()
        out = como_dict(x)
    if programar_ya:
        try:
            revisar_cola()   # para que "Publicar ahora" arranque sin esperar la vuelta del worker
        except Exception as ex:   # la pieza ya quedó programada: el worker la toma en su próxima vuelta
            log.info("revisar_cola desde la API: %s", ex)
    return out


def _programar(x: Pieza, d: dict, usuario: str) -> None:
    if x.estado in BLOQUEADAS:
        raise ValueError("Ya se está subiendo o ya salió.")
    t = svc_cuentas.estado_token(x.cuenta_id)
    if not t["conectada"]:
        raise ValueError("Esta cuenta todavía no está conectada a Instagram: conectala en Cuentas para programar.")
    if not t["puede_publicar"]:
        raise ValueError("Falta el permiso de publicar: reconectá la cuenta y aceptalo.")
    caption = str(d.get("caption") if d.get("caption") is not None else x.caption or "").strip()
    if len(caption) > 2200:
        raise ValueError("El texto pasa los 2.200 caracteres que permite Instagram.")
    if len(re.findall(r"#\w+", caption)) > 30:
        raise ValueError("Instagram permite hasta 30 hashtags (lo ideal: 5 a 8).")
    err = validar_tipo(x.tipo, x.archivos)
    if err:
        raise ValueError(err)
    ya = not d.get("cuando")
    if ya:
        cuando = ahora()
    else:
        try:
            cuando = desde_local(str(d["cuando"]))
        except ValueError:
            raise ValueError("Hora inválida.")
        if cuando < ahora() - timedelta(minutes=1):
            raise ValueError("Esa hora ya pasó: elegí una hora futura o tocá Publicar ahora.")
    x.caption, x.cuando, x.ahora, x.estado = caption, cuando, ya, "programada"
    x.error, x.etapa, x.aprobada, x.aprobada_por = "", "", ahora(), usuario or "panel"


def planificadas_a_revisar(cid: str) -> int:
    """Cuando se conecta una cuenta, lo 'planificado' (armado sin conexión) pasa a 'Para aprobar' (no se publica solo).
    En Lite esas piezas quedaban 'planificada' para siempre."""
    with sesion() as s:
        ps = s.scalars(select(Pieza).where(Pieza.cuenta_id == cid, Pieza.estado == "planificada")).all()
        for x in ps:
            x.estado = "juzgada"
            x.sugerido = iso_local(x.cuando) if x.cuando and x.cuando > ahora() else ""
            x.cuando = None
        if ps:
            aviso(f"@{cid} quedó conectada: {len(ps)} piezas planificadas pasaron a la Bandeja para aprobar.")
        return len(ps)


# --- la cola -------------------------------------------------------------------------------------------------------------
def _clave(x: Pieza) -> str:
    """Una clave por aprobación: si la cancelan y la vuelven a programar (aunque sea a la misma hora), es otro trabajo."""
    return f"publicar:{x.id}:{x.aprobada.timestamp() if x.aprobada else 0:.0f}:{x.cuando.isoformat(timespec='minutes')}"


def revisar_cola() -> None:
    """Cada 30 s (scheduler del worker). Encola lo que llega a su hora; marca 'vencida' lo que se pasó;
    y resuelve lo que quedó 'subiendo' sin un worker vivo detrás."""
    t = ahora()
    vencidas, cortadas, a_encolar = [], [], []
    with sesion() as s:
        trabajos_ = {x.clave: x for x in s.scalars(select(Trabajo).where(Trabajo.tipo == "publicar",
                                                                         Trabajo.creado > t - timedelta(days=30)))}
        for x in s.scalars(select(Pieza).where(Pieza.estado.in_(("programada", "subiendo"))).order_by(Pieza.cuando)):
            if not x.cuando:
                continue
            trab = trabajos_.get(_clave(x))
            vivo = trab is not None and trab.estado == "corriendo" and trab.tomado_hasta and trab.tomado_hasta > t
            if x.estado == "subiendo":
                if not vivo and not x.media_id:
                    cortadas.append(x.id)
                continue
            if not x.ahora and t > x.cuando + TOLERANCIA and not vivo:
                vencidas.append(x.id)
            elif trab is None and (x.ahora or t >= x.cuando - ANTICIPO):
                a_encolar.append((x.id, _clave(x)))
    for pid, clave in a_encolar:
        encolar("publicar", {"pieza": pid, "clave": clave}, clave=clave, max_intentos=1)
    for pid in vencidas:
        if _pasar(pid, "programada", estado="vencida", etapa=""):
            _cancelar_trabajos(pid)
            x = obtener(pid)
            aviso(f"NO se publicó la pieza de @{x['cuenta']} ({x['tipo']}, era para el {x['cuando'].replace('T', ' ')}) "
                  "porque el sistema no estuvo disponible a esa hora. Reprogramala o tocá Publicar ahora.", pieza=pid)
            evento("pieza.vencida", pieza=pid, cuenta=x["cuenta"])
    for pid in cortadas:
        if _pasar(pid, "subiendo", estado="error", etapa="",
                  error="Se cortó mientras se publicaba. Fijate en Instagram si salió antes de volver a programarla."):
            x = obtener(pid)
            aviso(f"Se cortó la publicación de @{x['cuenta']} ({x['tipo']}): revisá en Instagram si salió.", pieza=pid)
            evento("pieza.error", pieza=pid, cuenta=x["cuenta"], error="cortada")


def _pasar(pid: str, desde: str, **cambios) -> bool:
    """Cambio de estado atómico: solo si la pieza sigue en `desde` (entre procesos, sin pisar a nadie)."""
    with sesion() as s:
        r = s.execute(update(Pieza).where(Pieza.id == pid, Pieza.estado == desde)
                      .values(version=Pieza.version + 1, actualizado=ahora(), **cambios))
        return r.rowcount == 1


def _cancelar_trabajos(pid: str) -> None:
    with sesion() as s:
        for t in s.scalars(select(Trabajo).where(Trabajo.tipo == "publicar", Trabajo.estado == "pendiente")):
            if (t.datos or {}).get("pieza") == pid:
                t.estado, t.terminado, t.error = "hecho", ahora(), "cancelado"


def _cambiar(pid: str, **kw) -> None:
    with sesion() as s:
        s.execute(update(Pieza).where(Pieza.id == pid).values(version=Pieza.version + 1, actualizado=ahora(), **kw))


@manejador("publicar")
def publicar(datos: dict) -> None:
    pid = datos["pieza"]
    with sesion() as s:
        x = s.get(Pieza, pid)
        if not x or x.media_id or x.estado != "programada" or (datos.get("clave") and _clave(x) != datos["clave"]):
            return   # cancelada, reprogramada, borrada o ya publicada: este trabajo quedó viejo
        tipo, archivos, caption, cuando, ya, cid = x.tipo, list(x.archivos), x.caption, x.cuando, x.ahora, x.cuenta_id
        contenedor = x.contenedor_id
    if not ya and cuando and ahora() > cuando + TOLERANCIA:   # nunca se publica sola tarde
        if _pasar(pid, "programada", estado="vencida", etapa=""):
            aviso(f"NO se publicó la pieza de @{cid} ({tipo}): se pasó la hora. Reprogramala o tocá Publicar ahora.", pieza=pid)
        return
    # la toma de la pieza es atómica: si dos trabajos llegaran a la vez, uno solo pasa de 'programada' a 'subiendo'
    if not _pasar(pid, "programada", estado="subiendo", etapa="arrancando", inicio=ahora(), error=""):
        return
    salida = almacen.dir_de("salida", pid)

    def avisar(e: str) -> None:
        _cambiar(pid, etapa=str(e)[:200])
        latido()
    try:
        cli = ig.Cliente(cid)
        listos = []
        prop = medios.proporcion_carrusel([almacen.ruta(a) for a in archivos]) if tipo == "carrusel" else None
        for i, a in enumerate(archivos):
            if medios.es_foto(a):
                listos.append(almacen.relativa(medios.jpeg_para_instagram(almacen.ruta(a), salida / f"{i + 1}.jpg", tipo, prop)))
            else:
                listos.append(a)
        if contenedor:   # un intento anterior ya lo dejó en Instagram: ¿se puede reusar? ¿ya salió?
            estado_c = (cli.get(contenedor, fields="status_code").get("status_code") or "") if contenedor else ""
            if estado_c == "PUBLISHED":
                _cambiar(pid, estado="publicada", etapa="", error="",
                         permalink="", publicado=ahora())
                aviso(f"La pieza de @{cid} ya figuraba publicada en Instagram (de un intento anterior).", pieza=pid)
                return
            if estado_c != "FINISHED":
                contenedor = ""
        usado = "url"
        if not contenedor:
            contenedor, usado = cli.preparar(tipo, listos, caption, avisar)
            _cambiar(pid, contenedor_id=contenedor, subida=usado)
        if not ya and cuando and cuando > ahora():
            avisar("listo en Instagram, esperando la hora programada")
            while cuando > ahora():
                time.sleep(min(20, max(1, (cuando - ahora()).total_seconds())))
                latido()
        with sesion() as s:                     # última verificación antes de publicar
            y = s.get(Pieza, pid)
            if not y or y.estado != "subiendo" or y.media_id:
                return
        avisar("publicando")
        r = cli.publicar_contenedor(contenedor)
        _cambiar(pid, media_id=r["id"])           # lo primero que se guarda: que nunca se vuelva a publicar
        nuevos = []
        destino = dir_cuenta(cid) / "_publicados"
        for a in archivos:
            p = almacen.ruta(a)
            try:
                nuevos.append(almacen.relativa(almacen.mover(p, destino)) if p.exists() else a)
            except Exception:
                nuevos.append(a)
        for a in listos:
            almacen.borrar_publica(a)
        _cambiar(pid, estado="publicada", etapa="", permalink=r.get("permalink", ""), publicado=r.get("publicado"),
                 archivos=nuevos)
        aviso(f"Publicado en @{cid} ({tipo}). {r.get('permalink', '')}", pieza=pid)
        actividad("publicado", f"Publicado en @{cid} ({tipo})", img=nuevos[0] if nuevos else "", cuenta=cid)
        evento("pieza.publicada", pieza=pid, cuenta=cid, tipo=tipo, media_id=r["id"], permalink=r.get("permalink", ""))
        encolar("sincronizar", {"cuenta": cid}, clave=f"sincronizar:{cid}:pub:{pid}",
                cuando=ahora() + timedelta(minutes=2))
    except Exception as ex:
        msg = "El permiso de Instagram venció: reconectá la cuenta." if isinstance(ex, ig.TokenVencido) else str(ex)[:300]
        with sesion() as s:
            y = s.get(Pieza, pid)
            ya_salio = bool(y and y.media_id)
        if ya_salio:   # salió en Instagram y falló algo después (mover archivos, etc.): queda publicada igual
            _cambiar(pid, estado="publicada", etapa="", error=f"Salió, pero: {msg[:200]}")
            return
        _cambiar(pid, estado="error", etapa="", error=msg)
        aviso(f"No se pudo publicar en @{cid} ({tipo}): {msg[:200]}", pieza=pid)
        evento("pieza.error", pieza=pid, cuenta=cid, error=msg)
    finally:
        shutil.rmtree(salida, ignore_errors=True)


def al_arrancar() -> None:
    """Al arrancar el worker: lo que quedó 'subiendo' sin worker vivo se resuelve en la primera vuelta de la cola."""
    revisar_cola()
