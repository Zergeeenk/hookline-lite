"""Comentarios: traerlos de Instagram, pedir sugerencias a Claude, responder/ocultar (siempre con aprobación)."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select

from .. import instagram as ig
from ..comun import iso_local
from ..db import ahora, sesion
from ..modelos import Comentario, Cuenta, MedioIG

CLASES = {"positivo", "neutro", "pregunta", "acoso", "insulto", "spam"}
NOMBRE_CLASE = {"positivo": "buena onda", "neutro": "neutro", "pregunta": "pregunta", "acoso": "acoso / zarpado",
                "insulto": "insulto", "spam": "spam"}
SIN_RESPUESTA = {"acoso", "insulto", "spam"}   # se sugiere ocultar, no contestar
# lo que nunca puede salir en una respuesta (si aparece, se marca para revisar)
PROHIBIDO = re.compile(r"https?://|whats ?app|telegram|onlyfans|\bof\b|precio|privado|\bdm\b|tarjeta|"
                       r"idiota|pelotud|bolud|forr[oa]|imb[eé]cil|puto|puta|mierda", re.I)


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", t)).strip()


def revisar(clase: str, resp: str, usadas=()) -> tuple[str, str]:
    """Reglas fijas sobre la sugerencia de Claude. Devuelve (alerta, respuesta). Alerta '' = se ve bien."""
    resp = (resp or "").strip()
    if clase in SIN_RESPUESTA:
        return f"{NOMBRE_CLASE[clase]}: mejor ocultarlo", ""
    if not resp:
        return "sin sugerencia", resp
    if PROHIBIDO.search(resp) or "@" in resp or len(resp) > 220:
        return "revisá la respuesta antes de mandar", resp
    if _norm(resp) in {_norm(x) for x in usadas}:
        return "respuesta repetida", resp
    return "", resp


def como_dict(c: Comentario) -> dict:
    return {"id": c.id, "media": c.media_id, "usuario": c.usuario, "texto": c.texto, "fecha": iso_local(c.fecha),
            "likes": c.likes, "respuestas": c.respuestas, "respondido_por_mi": c.respondido_por_mi, "post": c.post,
            "post_texto": c.post_texto, "miniatura": c.miniatura, "estado": c.estado, "clase": c.clase,
            "sugerencia": c.sugerencia, "alerta": c.alerta, "respuesta_enviada": c.respuesta_enviada,
            "respondido": iso_local(c.respondido)}


def de_cuenta(cid: str) -> dict:
    with sesion() as s:
        cs = s.scalars(select(Comentario).where(Comentario.cuenta_id == cid).order_by(Comentario.fecha.desc())).all()
        sugiriendo = any(c.sugiriendo for c in cs)
    return {"items": {c.id: como_dict(c) for c in cs}, "sugiriendo": "sí" if sugiriendo else ""}


def actualizar(cid: str) -> int:
    cli = ig.Cliente(cid)
    with sesion() as s:
        c = s.get(Cuenta, cid)
        yo = c.usuario_ig if c else ""
        ms = s.scalars(select(MedioIG).where(MedioIG.cuenta_id == cid, MedioIG.comentarios > 0)
                       .order_by(MedioIG.publicado.desc()).limit(10)).all()
    nuevos = 0
    for m in ms:
        for c in cli.comentarios(m.id, yo):
            with sesion() as s:
                viejo = s.get(Comentario, c["id"])
                if viejo:
                    viejo.likes, viejo.respuestas = c["likes"], c["respuestas"]
                    if c["respondido_por_mi"] and viejo.estado == "nuevo":
                        viejo.estado = "respondido"
                    continue
                s.add(Comentario(id=c["id"], cuenta_id=cid, media_id=m.id, usuario=c["usuario"], texto=c["texto"],
                                 fecha=c["fecha"], likes=c["likes"], respuestas=c["respuestas"],
                                 respondido_por_mi=c["respondido_por_mi"], post=m.url, post_texto=(m.caption or "")[:120],
                                 miniatura=m.miniatura, estado="respondido" if c["respondido_por_mi"] else "nuevo"))
                nuevos += 1
    return nuevos


def sugerir(cid: str) -> dict:
    """Hasta 30 comentarios nuevos sin sugerencia → UNA tarea de IA (modelo rápido)."""
    from ..ia import tareas
    with sesion() as s:
        pend = s.scalars(select(Comentario).where(Comentario.cuenta_id == cid, Comentario.estado == "nuevo",
                                                  Comentario.sugerencia == "", Comentario.sugiriendo.is_(False))
                         .limit(30)).all()
        if not pend:
            return {"ok": True, "via": "nada", "n": 0}
        cu = s.get(Cuenta, cid)
        for c in pend:
            c.sugiriendo = True
        coms = [{"id": c.id, "usuario": c.usuario, "texto": (c.texto or "")[:400]} for c in pend]
        nicho, tono = cu.nicho, cu.tono
    t = tareas.crear("respuestas", {"cuenta": "@" + cid, "nicho": nicho, "tono": tono, "comentarios": coms},
                     referencia=cid)
    return {"ok": True, "via": tareas.motor_actual(), "n": len(coms), "tarea": t}


def aplicar_sugerencias(cid: str, items: list[dict]) -> None:
    with sesion() as s:
        usadas = [c.respuesta_enviada for c in s.scalars(select(Comentario).where(
            Comentario.cuenta_id == cid, Comentario.respuesta_enviada != ""))]
        for it in items:
            c = s.get(Comentario, str(it.get("id", "")))
            clase = str(it.get("clase", "")).strip().lower()
            if not c or c.cuenta_id != cid or clase not in CLASES:
                continue
            alerta, resp = revisar(clase, str(it.get("respuesta") or "").strip().strip('"')[:300], usadas)
            c.clase, c.sugerencia, c.alerta, c.sugiriendo = clase, resp, alerta, False
            usadas.append(resp)
        for c in s.scalars(select(Comentario).where(Comentario.cuenta_id == cid, Comentario.sugiriendo.is_(True))):
            c.sugiriendo = False


def accion(d: dict, usuario: str = "") -> dict:
    cid, k, op = str(d.get("cuenta", "")), str(d.get("comentario", "")), d.get("op")
    from . import cuentas
    if not cuentas.existe(cid):
        raise ValueError("Cuenta desconocida.")
    if op == "actualizar":
        return {"ok": True, "nuevos": actualizar(cid)}
    if op == "sugerir":
        return sugerir(cid)
    with sesion() as s:
        c = s.get(Comentario, k)
        if not c or c.cuenta_id != cid:
            raise ValueError("Comentario desconocido.")
    if op == "responder":
        texto = str(d.get("texto") or "").strip()
        ig.Cliente(cid).responder(k, texto)
        cambios = {"estado": "respondido", "respuesta_enviada": texto, "respondido": ahora(), "respondido_por": usuario}
    elif op == "ocultar":
        ig.Cliente(cid).ocultar(k)
        cambios = {"estado": "oculto"}
    elif op == "saltar":
        cambios = {"estado": "saltado"}
    else:
        raise ValueError("Acción desconocida.")
    with sesion() as s:
        c = s.get(Comentario, k)
        for a, v in cambios.items():
            setattr(c, a, v)
    return {"ok": True}
