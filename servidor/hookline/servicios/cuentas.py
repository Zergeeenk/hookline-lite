"""Cuentas de Instagram: listar, crear (local o al conectar), editar, conectar (OAuth), sincronizar."""
from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

from .. import instagram as ig
from ..comun import actividad, app_meta, aviso, config_id_meta, evento, iso_local
from ..config import config
from ..db import ahora, sesion
from ..modelos import Cuenta, CredencialIG, EstadoOAuth, HistorialCuenta, MedioIG, Tendencias

ID_VALIDO = re.compile(r"[\w.]{1,40}")


def limpiar_id(texto: str) -> str:
    return re.sub(r"[^\w.]", "", str(texto or "").strip().lstrip("@"))[:30]


def existe(cid: str) -> bool:
    if not cid or not ID_VALIDO.fullmatch(str(cid)):
        return False
    with sesion() as s:
        return s.get(Cuenta, cid) is not None


def obtener(cid: str) -> Cuenta:
    with sesion() as s:
        c = s.get(Cuenta, cid) if cid and ID_VALIDO.fullmatch(str(cid)) else None
    if not c:
        raise ValueError("Cuenta desconocida.")
    return c


def modelo_de(cid: str) -> str:
    """Las multicuentas usan el material de su cuenta main (la main es la modelo)."""
    try:
        c = obtener(cid)
    except ValueError:
        return cid
    return c.main_id or c.id


def estado_token(cid: str) -> dict:
    with sesion() as s:
        cr = s.get(CredencialIG, cid)
    permisos = (cr.permisos or "") if cr else ""
    conectada = bool(cr and not cr.vencido and cr.vence > time.time())
    return {"conectada": conectada, "vencida": bool(cr and (cr.vencido or cr.vence <= time.time())),
            "dias_token": round((cr.vence - time.time()) / 86400, 1) if cr else None,
            "puede_publicar": ig.P_PUBLICAR in permisos, "puede_comentar": ig.P_COMENTARIOS in permisos,
            "puede_insights": ig.P_INSIGHTS in permisos}


def como_dict(c: Cuenta) -> dict:
    """El mismo formato que usaba el panel de Lite (cuentas() de panel.py)."""
    with sesion() as s:
        tend = s.get(Tendencias, c.id)
    t = estado_token(c.id)
    return {"id": c.id, "usuario": c.usuario_ig or c.id, "nombre": c.nombre, "nicho": c.nicho, "tono": c.tono,
            "apodo": c.apodo, "main": c.main_id or "", "activa_desde": c.activa_desde, "creada": iso_local(c.creada),
            "foto": c.foto, **t, "tipo_cuenta": c.tipo_cuenta, "seguidores": c.seguidores,
            "publicaciones": c.publicaciones, "ultima": iso_local(c.ultima_sync), "error": c.error, "aviso": c.aviso,
            "limite": c.limite or {}, "cuenta_28d": c.insights_28d or {},
            "tendencias_fecha": iso_local(tend.fecha) if tend else "",
            "pyro_modelo_id": c.pyro_modelo_id, "pyro_modelo": c.pyro_modelo or "",
            "espacio": c.espacio or "modelos",
            "perfil": f"https://www.instagram.com/{c.usuario_ig or c.id}/"}


def listar() -> list[dict]:
    with sesion() as s:
        cs = s.scalars(select(Cuenta).order_by(Cuenta.id)).all()
    return [como_dict(c) for c in cs]


def crear_local(cid: str, main: str = "", espacio: str = "modelos") -> str:
    """Cuenta sin conectar todavía (para ir cargando contenido). Se conecta después."""
    cid = limpiar_id(cid)
    if not cid:
        raise ValueError("Escribí el usuario de Instagram de la cuenta.")
    main = limpiar_id(main)
    with sesion() as s:
        if s.get(Cuenta, cid):
            raise ValueError("Esa cuenta ya existe.")
        if main:
            m = s.get(Cuenta, main)
            if not m or m.main_id:
                raise ValueError("La cuenta main elegida no existe (o es una multicuenta).")
            espacio = m.espacio or "modelos"           # la multicuenta va al mismo espacio que su main
        if espacio not in ("modelos", "noticias"):
            raise ValueError("El espacio tiene que ser 'modelos' o 'noticias'.")
        s.add(Cuenta(id=cid, main_id=main or None, espacio=espacio))
    return cid


def guardar(cid: str, d: dict) -> None:
    with sesion() as s:
        c = s.get(Cuenta, cid)
        if not c:
            raise ValueError("Cuenta desconocida.")
        for k in ("nicho", "tono", "apodo"):
            if k in d:
                setattr(c, k, str(d[k] or "").strip()[:300])
        if "espacio" in d:
            if d["espacio"] not in ("modelos", "noticias"):
                raise ValueError("El espacio tiene que ser 'modelos' o 'noticias'.")
            c.espacio = d["espacio"]
        if "activa_desde" in d:
            v = str(d["activa_desde"] or "")[:10]
            if v:
                datetime.fromisoformat(v)
            c.activa_desde = v
        if "main" in d:
            main = limpiar_id(d["main"])
            if main == cid:
                raise ValueError("Una cuenta no puede ser multicuenta de sí misma.")
            if main:
                m = s.get(Cuenta, main)
                if not m or m.main_id:
                    raise ValueError("La cuenta main elegida no existe (o es una multicuenta).")
                if s.scalar(select(func.count()).select_from(Cuenta).where(Cuenta.main_id == cid)):
                    raise ValueError("Esta cuenta tiene multicuentas: no puede ser multicuenta de otra.")
            c.main_id = main or None


def desconectar(cid: str) -> None:
    with sesion() as s:
        s.execute(delete(CredencialIG).where(CredencialIG.cuenta_id == cid))


def borrar(cid: str) -> None:
    with sesion() as s:
        if s.scalar(select(func.count()).select_from(Cuenta).where(Cuenta.main_id == cid)):
            raise ValueError("Primero borrá o mové sus multicuentas.")
        c = s.get(Cuenta, cid)
        if c:
            s.delete(c)


# --- OAuth ---------------------------------------------------------------------------------------------------------
def url_conectar(usuario_id: int | None = None) -> str:
    app_id, secret = app_meta()
    if not app_id or not secret:
        raise ValueError("Primero cargá el App ID y el App Secret de la app de Meta en Ajustes.")
    state = secrets.token_urlsafe(24)
    with sesion() as s:
        s.execute(delete(EstadoOAuth).where(EstadoOAuth.creado < ahora() - timedelta(hours=1)))
        s.add(EstadoOAuth(state=state, usuario_id=usuario_id))
    if config_id_meta():
        return ig.url_login_unificado(app_id, config_id_meta(), config().redirect_instagram, state)
    return ig.url_login(app_id, config().redirect_instagram, state)


def conectar_con_codigo(code: str, state: str) -> tuple[str, str]:
    """El `state` es obligatorio y de un solo uso (en Lite era opcional: login CSRF)."""
    if not code:
        raise ValueError("Falta el código de Instagram.")
    with sesion() as s:
        e = s.get(EstadoOAuth, str(state or ""))
        if not e or e.creado < ahora() - timedelta(hours=1):
            raise ValueError("El inicio de sesión expiró: tocá Conectar cuenta de nuevo.")
        s.delete(e)
    app_id, secret = app_meta()
    if config_id_meta():
        tok = ig.canjear_codigo_unificado(app_id, secret, config().redirect_instagram, code)
        perfiles = ig.cuentas_unificado(tok["access_token"])
        if not perfiles:
            raise ValueError("Meta no devolvió ninguna cuenta de Instagram. Al autorizar, elegí la cuenta de Instagram "
                             "en la lista (y que sea profesional y evaluadora de la app mientras esté en prueba).")
        hechas = [crear_desde_token({**tok, "user_id": p["user_id"]}, p) for p in perfiles]
        return hechas[0][0], ("" if len(hechas) == 1 else f"{len(hechas)} cuentas")
    tok = ig.canjear_codigo(app_id, secret, config().redirect_instagram, code)
    return crear_desde_token(tok)


def crear_desde_token(tok: dict, u: dict | None = None) -> tuple[str, str]:
    u = u or ig.perfil_con_token(tok["access_token"])
    cid = limpiar_id(u.get("username") or "") or f"cuenta{tok.get('user_id', '')}"
    with sesion() as s:
        c = s.get(Cuenta, cid) or Cuenta(id=cid)
        c.nombre = u.get("name") or c.nombre or ""
        c.usuario_ig = u.get("username") or cid
        c.ig_user_id = str(u.get("user_id") or tok.get("user_id") or "")
        c.tipo_cuenta = u.get("account_type") or ""
        s.add(c)
    ig.guardar_credencial(cid, tok)
    actividad("cuenta", f"@{cid} conectada a Instagram", cuenta=cid)
    evento("cuenta.conectada", cuenta=cid)
    from ..trabajos import encolar
    encolar("sincronizar", {"cuenta": cid}, clave=f"sincronizar:{cid}:{int(time.time() // 60)}")
    # las piezas que se armaron mientras no estaba conectada quedan esperando aprobación (no se publican solas)
    from .piezas import planificadas_a_revisar
    planificadas_a_revisar(cid)
    return cid, u.get("account_type", "")


# --- sincronización (perfil, medios, insights, historial, límite) --------------------------------------------------------
def sincronizar(cid: str) -> dict:
    cli = ig.Cliente(cid)
    try:
        u = cli.perfil()
        crudos = cli.medios()
        with sesion() as s:
            c = s.get(Cuenta, cid)
            metricas_ok = dict(c.metricas_ok or {})
            if u.get("user_id"):
                c.ig_user_id = str(u["user_id"])
        puede_ins = ig.P_INSIGHTS in cli.permisos()
        avisos = []
        with sesion() as s:
            previos = {m.id: m for m in s.scalars(select(MedioIG).where(MedioIG.cuenta_id == cid))}
        filas = []
        for i, m in enumerate(crudos):
            es_reel = m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO"
            ant = previos.get(m["id"])
            f = {"id": m["id"], "cuenta_id": cid, "caption": m.get("caption") or "",
                 "tipo": m.get("media_product_type") or m.get("media_type") or "", "media_type": m.get("media_type") or "",
                 "reel": es_reel, "url": m.get("permalink", ""), "publicado": ig.local_iso(m.get("timestamp") or ""),
                 "miniatura": m.get("thumbnail_url") or (m.get("media_url") if m.get("media_type") != "VIDEO" else "") or "",
                 "likes": int(m.get("like_count") or 0), "comentarios": int(m.get("comments_count") or 0), "orden": i}
            for k in ("vistas", "alcance", "guardados", "compartidos", "interacciones"):
                f[k] = getattr(ant, k) if ant else None
            if puede_ins and i < ig.INSIGHTS_MAX:
                try:
                    ins, ok = cli.insights(m["id"], es_reel, metricas_ok)
                    if ok:
                        metricas_ok["reel" if es_reel else "post"] = ok
                    f.update(vistas=ins.get("views", f["vistas"]), alcance=ins.get("reach", f["alcance"]),
                             guardados=ins.get("saved", f["guardados"]), compartidos=ins.get("shares", f["compartidos"]),
                             interacciones=ins.get("total_interactions", f["interacciones"]))
                except ig.TokenVencido:
                    raise
                except Exception as ex:
                    if not avisos:
                        avisos.append(f"Estadísticas: {str(ex)[:150]}")
            filas.append(f)
        cuenta28 = None
        if puede_ins:
            try:
                cuenta28 = cli.insights_cuenta()
            except ig.TokenVencido:
                raise
            except Exception:
                pass
        try:
            lim = cli.limite()
        except ig.TokenVencido:
            raise
        except Exception:
            lim = None
        if not puede_ins:
            avisos.append("Sin permiso de estadísticas: reconectá la cuenta y aceptá 'insights'.")
        t = ahora()
        with sesion() as s:
            for f in filas:
                s.merge(MedioIG(**f))
            c = s.get(Cuenta, cid)
            c.usuario_ig = u.get("username") or c.usuario_ig
            c.nombre = u.get("name") or c.nombre
            c.tipo_cuenta = u.get("account_type") or c.tipo_cuenta
            c.foto = u.get("profile_picture_url") or ""
            c.seguidores, c.siguiendo, c.publicaciones = u.get("followers_count"), u.get("follows_count"), u.get("media_count")
            c.ultima_sync, c.error, c.aviso, c.metricas_ok = t, "", " · ".join(avisos), metricas_ok
            if cuenta28 is not None:
                c.insights_28d = cuenta28
            if lim:
                c.limite = lim
            hora = t.strftime("%Y-%m-%dT%H")
            h = s.scalar(select(HistorialCuenta).where(HistorialCuenta.cuenta_id == cid, HistorialCuenta.hora == hora))
            h = h or HistorialCuenta(cuenta_id=cid, hora=hora)
            h.fecha, h.seguidores, h.publicaciones = t, u.get("followers_count"), u.get("media_count")
            h.vistas = sum(f.get("vistas") or 0 for f in filas)
            s.add(h)
        return {"ok": True, "medios": len(filas), "aviso": " · ".join(avisos)}
    except ig.TokenVencido:
        msg = "El permiso de Instagram venció o se revocó: reconectá la cuenta."
        _anotar_error(cid, msg)
        aviso(f"@{cid}: {msg}")
        return {"ok": False, "vencido": True, "error": msg}
    except Exception as ex:   # el sistema sigue andando aunque Instagram falle
        _anotar_error(cid, str(ex)[:300])
        return {"ok": False, "error": str(ex)[:300]}


def _anotar_error(cid: str, msg: str) -> None:
    with sesion() as s:
        c = s.get(Cuenta, cid)
        if c:
            c.error = msg


def refrescar_token(cid: str) -> None:
    """Job diario: fuerza el refresco si corresponde (así no depende de que alguien use la cuenta)."""
    cli = ig.Cliente(cid)
    if not cli.conectada():
        return
    try:
        cli.token()
    except ig.TokenVencido:
        aviso(f"@{cid}: el permiso de Instagram venció. Tocá Reconectar en Cuentas.")
        return
    cr = cli.credencial()
    if cr and cr.vence - time.time() < 7 * 86400:
        evento("cuenta.token_por_vencer", cuenta=cid, dias=round((cr.vence - time.time()) / 86400, 1))


# --- estadísticas para el panel --------------------------------------------------------------------------------------
def medios_de(cid: str, n: int = 60) -> list[dict]:
    with sesion() as s:
        ms = s.scalars(select(MedioIG).where(MedioIG.cuenta_id == cid).order_by(MedioIG.publicado.desc()).limit(n)).all()
    return [medio_dict(m) for m in ms]


def medio_dict(m: MedioIG) -> dict:
    return {"id": m.id, "caption": m.caption, "tipo": m.tipo, "media_type": m.media_type, "reel": m.reel, "url": m.url,
            "publicado": iso_local(m.publicado), "miniatura": m.miniatura, "likes": m.likes, "comentarios": m.comentarios,
            "vistas": m.vistas, "alcance": m.alcance, "guardados": m.guardados, "compartidos": m.compartidos,
            "interacciones": m.interacciones}


def mejores_horarios(cid: str) -> dict:
    """Promedio de vistas por día de semana + hora (hora local). Lo mejor primero."""
    from ..comun import local
    with sesion() as s:
        ms = s.scalars(select(MedioIG).where(MedioIG.cuenta_id == cid, MedioIG.vistas.is_not(None))).all()
    grupos, por_hora = {}, {}
    for m in ms:
        if not m.publicado:
            continue
        t = local(m.publicado)
        grupos.setdefault((t.weekday(), t.hour), []).append(m.vistas)
        por_hora.setdefault(t.hour, []).append(m.vistas)
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    mejores = sorted(({"dia": dias[k[0]], "dia_n": k[0], "hora": f"{k[1]:02d}:00", "promedio": round(sum(v) / len(v)),
                       "n": len(v)} for k, v in grupos.items()), key=lambda x: -x["promedio"])
    horas = [{"hora": f"{h:02d}:00", "promedio": round(sum(v) / len(v)), "n": len(v)} for h, v in sorted(por_hora.items())]
    return {"mejores": mejores[:5], "por_hora": horas, "con_datos": sum(len(v) for v in grupos.values())}


def stats(cid: str) -> dict:
    c = obtener(cid)
    with sesion() as s:
        hs = s.scalars(select(HistorialCuenta).where(HistorialCuenta.cuenta_id == cid)
                       .order_by(HistorialCuenta.fecha.desc()).limit(24 * 60)).all()
    historial = [{"fecha": iso_local(h.fecha), "seguidores": h.seguidores, "vistas": h.vistas,
                  "publicaciones": h.publicaciones} for h in reversed(hs)]
    d = como_dict(c)
    estado = {**d, "conectado": d["conectada"], "vencido": d["vencida"], "siguiendo": c.siguiendo}
    return {"estado": estado, "historial": historial, "medios": medios_de(cid), "horarios": mejores_horarios(cid)}


def calendario(cid: str) -> list[dict]:
    out = []
    with sesion() as s:
        q = select(MedioIG).order_by(MedioIG.publicado.desc())
        if cid not in ("", "todas"):
            q = q.where(MedioIG.cuenta_id == cid)
        for m in s.scalars(q.limit(400)):
            out.append({"cuenta": m.cuenta_id, "id": m.id, "publicado": iso_local(m.publicado), "tipo": m.tipo,
                        "url": m.url, "miniatura": m.miniatura, "vistas": m.vistas, "caption": (m.caption or "")[:100]})
    return out
