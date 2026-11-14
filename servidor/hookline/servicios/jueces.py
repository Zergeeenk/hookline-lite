"""Jueces (Público, Gancho y tendencia, Seguridad), tendencias semanales y chat: pedidos a Claude y qué se hace con
la respuesta."""
from __future__ import annotations

import shutil
from datetime import timedelta

from sqlalchemy import select

from .. import almacen, medios
from ..comun import aviso, chat_agregar, evento, iso_local, mensaje_dict
from ..db import ahora, sesion
from ..ia import tareas
from ..modelos import Cuenta, MedioIG, MensajeChat, Pieza, Tendencias
from ..trabajos import manejador
from . import cuentas as svc_cuentas
from .piezas import validar_tipo


# --- jueces ---------------------------------------------------------------------------------------------------------------
def pedir_jueces(pid: str) -> dict:
    with sesion() as s:
        x = s.get(Pieza, pid)
        if not x:
            raise ValueError("Esa pieza ya no está.")
        if x.estado in ("subiendo", "publicada"):
            raise ValueError("Ya se publicó.")
        err = validar_tipo(x.tipo, x.archivos)
        if err:
            raise ValueError(err)
        x.estado, x.jueces, x.pedido_jueces, x.error = "jueces", None, ahora(), ""
    from ..trabajos import encolar
    encolar("preparar_jueces", {"pieza": pid}, clave=f"jueces:{pid}:{ahora().timestamp():.0f}")
    from .piezas import obtener
    return obtener(pid)


@manejador("preparar_jueces")
def preparar_jueces(datos: dict) -> None:
    """Saca los cuadros (ffmpeg) y arma el pedido. En un trabajo aparte: con videos largos tarda."""
    pid = datos["pieza"]
    carpeta = almacen.dir_de("jueces", pid)
    try:
        with sesion() as s:
            x = s.get(Pieza, pid)
            if not x or x.estado != "jueces":
                return
            tipo, archivos, cid, caption = x.tipo, list(x.archivos), x.cuenta_id, x.caption
            cu = s.get(Cuenta, cid)
            nicho, tono, seguidores = cu.nicho, cu.tono, cu.seguidores
            ya = [{"cuando": iso_local(p.cuando), "tipo": p.tipo} for p in
                  s.scalars(select(Pieza).where(Pieza.cuenta_id == cid, Pieza.estado == "programada"))]
        shutil.rmtree(carpeta, ignore_errors=True)
        carpeta.mkdir(parents=True, exist_ok=True)
        partes, imagenes = [], []
        for i, a in enumerate(archivos):
            cuadros, dur = medios.cuadros_jueces(almacen.ruta(a), carpeta / f"parte{i + 1}")
            rels = [almacen.relativa(c) for c in cuadros]
            imagenes += rels
            partes.append({"parte": i + 1, "es_video": medios.es_video(a), "duracion_s": dur, "cuadros": len(rels)})
        if not imagenes:
            raise RuntimeError("No pude sacar cuadros del video (¿falta ffmpeg?).")
        pedido = {"pieza": pid, "cuenta": "@" + cid, "nicho": nicho or "sin definir", "tono": tono or "", "tipo": tipo,
                  "seguidores": seguidores, "texto_actual": caption or "", "partes": partes,
                  "ahora": iso_local(ahora()), "mejores_horarios": svc_cuentas.mejores_horarios(cid), "ya_programado": ya}
        tareas.crear("jueces", pedido, referencia=pid, imagenes=imagenes)
    except Exception as ex:
        with sesion() as s:
            y = s.get(Pieza, pid)
            if y and y.estado == "jueces":
                y.estado, y.error = "nueva", f"Jueces: {str(ex)[:200]}"


@tareas.al_terminar("jueces")
def _veredicto(t, v: dict) -> None:
    with sesion() as s:
        x = s.get(Pieza, t.referencia)
        if not x or x.estado != "jueces":
            return
        jueces = v["jueces"][:3]
        notas = [float(j.get("nota") or 0) for j in jueces]
        v["promedio"] = round(sum(notas) / len(notas), 1) if notas else None
        tags = [h if str(h).startswith("#") else "#" + str(h) for h in v.get("hashtags") or []][:8]
        v["hashtags"] = tags
        x.jueces = v
        if not (x.caption or "").strip():
            x.caption = ((v.get("caption") or "").strip() + ("\n\n" + " ".join(tags) if tags else "")).strip()
        x.sugerido = str((v.get("horario") or {}).get("cuando") or "")[:16]
        x.estado, x.error = "juzgada", ""
        cid = x.cuenta_id
    aviso(f"Los jueces terminaron con la pieza de @{cid}: {v['promedio']}/10 · "
          f"{'aprobar' if v.get('veredicto') == 'aprobar' else 'cambiar'}. Miralo en la Bandeja.", pieza=t.referencia)
    evento("pieza.juzgada", pieza=t.referencia, cuenta=cid, promedio=v["promedio"], veredicto=v.get("veredicto"))


@tareas.al_fallar("jueces")
def _jueces_fallo(t, motivo: str) -> None:
    with sesion() as s:
        x = s.get(Pieza, t.referencia)
        if x and x.estado == "jueces":
            x.estado, x.error = "nueva", f"Jueces: {motivo[:200]}"


# --- respuestas a comentarios -----------------------------------------------------------------------------------------------
@tareas.al_terminar("respuestas")
def _respuestas(t, r: dict) -> None:
    from .comentarios import aplicar_sugerencias
    aplicar_sugerencias(t.referencia, r["items"])


@tareas.al_fallar("respuestas")
def _respuestas_fallo(t, motivo: str) -> None:
    from .comentarios import aplicar_sugerencias
    aplicar_sugerencias(t.referencia, [])
    aviso(f"No pude sugerir respuestas para @{t.referencia}: {motivo[:150]}")


# --- tendencias ----------------------------------------------------------------------------------------------------------------
def pedir_tendencias(cid: str, auto: bool = False) -> dict:
    with sesion() as s:
        c = s.get(Cuenta, cid)
        if not c:
            raise ValueError("Cuenta desconocida.")
        if not c.nicho:
            raise ValueError("Primero escribí el nicho de la cuenta (Cuentas → Editar).")
        c.tendencias_pedido = ahora()
        ms = s.scalars(select(MedioIG).where(MedioIG.cuenta_id == cid).order_by(MedioIG.publicado.desc()).limit(25)).all()
        medios_ = [{"tipo": m.tipo, "caption": (m.caption or "")[:120], "vistas": m.vistas, "publicado": iso_local(m.publicado)}
                   for m in ms]
        nicho, tono = c.nicho, c.tono
    tid = tareas.crear("tendencias", {"cuenta": "@" + cid, "nicho": nicho, "tono": tono, "ahora": iso_local(ahora()),
                                      "medios": medios_, "auto": auto}, referencia=cid)
    return {"ok": True, "tarea": tid}


@tareas.al_terminar("tendencias")
def _tendencias(t, r: dict) -> None:
    with sesion() as s:
        x = s.get(Tendencias, t.referencia) or Tendencias(cuenta_id=t.referencia)
        x.fecha, x.nicho, x.items = ahora(), r.get("nicho") or "", r["items"][:8]
        s.add(x)


@tareas.al_fallar("tendencias")
def _tendencias_fallo(t, motivo: str) -> None:
    aviso(f"No pude buscar tendencias para @{t.referencia}: {motivo[:150]}")


def tendencias_de(cid: str) -> dict:
    with sesion() as s:
        x = s.get(Tendencias, cid)
        return {"fecha": iso_local(x.fecha), "nicho": x.nicho, "items": x.items} if x else {"items": []}


def tendencias_semanales() -> None:
    """De a una cuenta por vuelta (cada hora): las que tienen nicho y no tienen tendencias de los últimos 7 días."""
    with sesion() as s:
        cs = s.scalars(select(Cuenta).where(Cuenta.nicho != "")).all()
        for c in cs:
            tend = s.get(Tendencias, c.id)
            ult = max([d for d in (tend.fecha if tend else None, c.tendencias_pedido) if d], default=None)
            if not ult or ahora() - ult > timedelta(days=7):
                cid = c.id
                break
        else:
            return
    pedir_tendencias(cid, auto=True)


# --- chat ------------------------------------------------------------------------------------------------------------------------
def mensaje(texto: str, autor: str = "") -> dict:
    texto = str(texto or "").strip()
    if not texto:
        raise ValueError("Escribí algo.")
    m = chat_agregar("vos", texto, autor=autor)
    from .piezas import listar
    contexto = {"cuentas": [{k: c[k] for k in ("id", "nicho", "tono", "seguidores", "conectada", "main")}
                            for c in svc_cuentas.listar()],
                "bandeja": [{k: p[k] for k in ("id", "cuenta", "tipo", "estado", "cuando")} for p in listar()][-40:]}
    with sesion() as s:
        hist = [mensaje_dict(x) for x in s.scalars(select(MensajeChat).order_by(MensajeChat.id.desc()).limit(12))]
    tareas.crear("chat", {"texto": texto, "contexto": contexto, "historial": list(reversed(hist))},
                 referencia=str(m["id"]))
    return m


@tareas.al_terminar("chat")
def _chat(t, r: dict) -> None:
    chat_agregar("claude", r["respuesta"])


@tareas.al_fallar("chat")
def _chat_fallo(t, motivo: str) -> None:
    aviso(f"Claude no pudo contestar: {motivo[:150]}")
