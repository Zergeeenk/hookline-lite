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

from sqlalchemy import select

from .. import almacen, instagram as ig, medios
from ..comun import actividad, aviso, desde_local, evento, iso_local
from ..db import ahora, sesion
from ..modelos import Cuenta, Pieza, Trabajo
from ..trabajos import encolar, manejador
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
    Con Postgres, un candado evita que dos procesos escaneen a la vez (y creen piezas repetidas)."""
    from ..db import es_postgres
    from sqlalchemy import text
    nuevos_n = 0
    with _lock, sesion() as s:
        if es_postgres() and not s.scalar(text("SELECT pg_try_advisory_xact_lock(4242001)")):
            return 0
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
    op, pid = d.get("op"), str(d.get("id", ""))
    if op == "jueces":
        from .jueces import pedir_jueces
        return pedir_jueces(pid)
    programar_ya = False
    with _lock, sesion() as s:
        if op == "juntar":
            ids = [str(i) for i in d.get("ids") or []]
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
        revisar_cola()
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
def revisar_cola() -> None:
    """Cada 30 s (scheduler del worker). Encola lo que llega a su hora; marca 'vencida' lo que se pasó."""
    t = ahora()
    vencidas = []
    with sesion() as s:
        ps = s.scalars(select(Pieza).where(Pieza.estado == "programada").order_by(Pieza.cuando)).all()
        for x in ps:
            if not x.cuando:
                continue
            clave = f"publicar:{x.id}:{x.cuando.isoformat(timespec='minutes')}"
            trab = s.scalar(select(Trabajo).where(Trabajo.clave == clave))
            if not x.ahora and t > x.cuando + TOLERANCIA and (trab is None or trab.estado == "pendiente"):
                x.estado, x.etapa = "vencida", ""
                if trab is not None:
                    trab.estado, trab.terminado, trab.error = "hecho", t, "cancelado: vencida"
                vencidas.append((x.id, x.cuenta_id, x.tipo, iso_local(x.cuando)))
            elif trab is None and (x.ahora or t >= x.cuando - ANTICIPO):
                s.add(Trabajo(tipo="publicar", datos={"pieza": x.id}, clave=clave, max_intentos=1))
    for pid, cid, tipo, cuando in vencidas:
        aviso(f"NO se publicó la pieza de @{cid} ({tipo}, era para el {cuando.replace('T', ' ')}) porque el sistema "
              "no estuvo disponible a esa hora. Reprogramala o tocá Publicar ahora.", pieza=pid)
        evento("pieza.vencida", pieza=pid, cuenta=cid)


def _cambiar(pid: str, **kw) -> None:
    with sesion() as s:
        x = s.get(Pieza, pid)
        if x:
            for k, v in kw.items():
                setattr(x, k, v)


@manejador("publicar")
def publicar(datos: dict) -> None:
    pid = datos["pieza"]
    with sesion() as s:
        x = s.get(Pieza, pid)
        if not x or x.media_id or x.estado not in ("programada", "subiendo"):
            return   # cancelada, borrada o ya publicada: nada que hacer
        x.estado, x.etapa, x.inicio, x.error = "subiendo", "arrancando", ahora(), ""
        tipo, archivos, caption, cuando, ya, cid = x.tipo, list(x.archivos), x.caption, x.cuando, x.ahora, x.cuenta_id
        contenedor = x.contenedor_id
    salida = almacen.dir_de("salida", pid)
    try:
        cli = ig.Cliente(cid)
        avisar = lambda e: _cambiar(pid, etapa=str(e)[:200])
        listos = []
        prop = medios.proporcion_carrusel([almacen.ruta(a) for a in archivos]) if tipo == "carrusel" else None
        for i, a in enumerate(archivos):
            if medios.es_foto(a):
                listos.append(almacen.relativa(medios.jpeg_para_instagram(almacen.ruta(a), salida / f"{i + 1}.jpg", tipo, prop)))
            else:
                listos.append(a)
        usado = "url"
        if contenedor:   # un intento anterior ya lo dejó listo en Instagram: se reusa si sigue vivo
            try:
                cli.esperar_contenedor(contenedor, avisar)
            except Exception:
                contenedor = ""
        if not contenedor:
            contenedor, usado = cli.preparar(tipo, listos, caption, avisar)
            _cambiar(pid, contenedor_id=contenedor, subida=usado)
        if not ya and cuando and cuando > ahora():
            avisar("listo en Instagram, esperando la hora programada")
            while cuando > ahora():
                time.sleep(min(20, max(1, (cuando - ahora()).total_seconds())))
                with sesion() as s:
                    if s.get(Pieza, pid) is None:
                        return
        avisar("publicando")
        r = cli.publicar_contenedor(contenedor)
        # los originales pasan a para-subir/<cuenta>/_publicados/ (la bandeja queda limpia)
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
        _cambiar(pid, estado="publicada", etapa="", media_id=r["id"], permalink=r.get("permalink", ""),
                 publicado=r.get("publicado"), archivos=nuevos)
        aviso(f"Publicado en @{cid} ({tipo}). {r.get('permalink', '')}", pieza=pid)
        actividad("publicado", f"Publicado en @{cid} ({tipo})", img=nuevos[0] if nuevos else "", cuenta=cid)
        evento("pieza.publicada", pieza=pid, cuenta=cid, tipo=tipo, media_id=r["id"], permalink=r.get("permalink", ""))
        encolar("sincronizar", {"cuenta": cid}, clave=f"sincronizar:{cid}:pub:{pid}",
                cuando=ahora() + timedelta(minutes=2))
    except Exception as ex:
        msg = "El permiso de Instagram venció: reconectá la cuenta." if isinstance(ex, ig.TokenVencido) else str(ex)[:300]
        _cambiar(pid, estado="error", etapa="", error=msg)
        aviso(f"No se pudo publicar en @{cid} ({tipo}): {msg[:200]}", pieza=pid)
        evento("pieza.error", pieza=pid, cuenta=cid, error=msg)
    finally:
        shutil.rmtree(salida, ignore_errors=True)


def al_arrancar() -> None:
    """Lo que quedó 'subiendo' cuando se cortó un worker vuelve a 'programada' (el trabajo se reencola o vence).
    Si otro worker vivo lo tiene tomado (tomado_hasta en el futuro), no se toca."""
    with sesion() as s:
        trabajos = [t for t in s.scalars(select(Trabajo).where(Trabajo.tipo == "publicar", Trabajo.estado != "hecho"))]
        for x in s.scalars(select(Pieza).where(Pieza.estado == "subiendo")):
            if x.media_id:
                continue
            suyos = [t for t in trabajos if (t.datos or {}).get("pieza") == x.id]
            if any(t.estado == "corriendo" and t.tomado_hasta and t.tomado_hasta > ahora() for t in suyos):
                continue
            x.estado, x.etapa = "programada", ""
            for t in suyos:   # revisar_cola lo vuelve a encolar si todavía está en hora (o lo marca vencida)
                s.delete(t)
