"""Importa una carpeta de Hookline Lite a la base y al volumen del servicio. Se puede correr más de una vez
(lo que ya existe se actualiza, no se duplica). Si un registro está roto, se saltea y se cuenta en "salteados".

Qué se importa:
  datos/config.json            → App ID y App Secret (cifrado) en ajustes
  cuentas/<id>/cuenta.json     → cuentas (nicho, tono, apodo, main, activa_desde)
  cuentas/<id>/token.json      → credenciales_ig (token CIFRADO; en Lite estaba en claro)
  cuentas/<id>/estado.json, medios.json, historial.json, comentarios.json, tendencias.json
  datos/piezas.json            → piezas (las horas locales pasan a UTC). Lo que estaba "subiendo" entra como
                                 "error" para revisarlo a mano (puede haber salido en Instagram).
  datos/chat.json              → chat (últimos 400)
  Para subir/<cuenta>/         → para-subir/<cuenta>/
  Material/<modelo>/...        → material/<modelo>/...  (+ material.json → material_meta)
  Material/_copias/<id>/       → copias/<id>/ (+ copia.json → copias; _links.json → banco_links)
"""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from . import almacen, seguridad
from .comun import desde_local, guardar_ajuste
from .db import sesion
from .modelos import (Comentario, Copia, CredencialIG, Cuenta, HistorialCuenta, LinkBanco, MaterialMeta, MedioIG,
                      MensajeChat, Pieza, Tendencias)

log = logging.getLogger("hookline.migrar")
PREFIJOS = [("Para subir/", "para-subir/"), ("Material/_copias/", "copias/"), ("Material/", "material/"),
            ("Resultados/", "resultados/")]
INT_MAX = 2 ** 31 - 1


# --- lectura y conversión tolerante ---------------------------------------------------------------------------------------
def _leer(p: Path, defecto):
    """JSON del archivo, o `defecto` si no existe, está roto o no es del tipo esperado (mismo tipo que defecto)."""
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return defecto
    if defecto is not None and not isinstance(v, type(defecto)):
        return defecto
    return v


def _dicts(v) -> list[dict]:
    return [x for x in (v if isinstance(v, list) else []) if isinstance(x, dict)]


def _s(v, largo: int = 10000) -> str:
    return v[:largo] if isinstance(v, str) else ("" if v is None or isinstance(v, (dict, list)) else str(v)[:largo])


def _i(v) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if abs(n) <= INT_MAX else None


def _f(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _id(v) -> str:
    """Ids (cuentas, medios, comentarios, piezas): texto corto sin caracteres raros."""
    t = _s(v, 60).strip()
    return t if re.fullmatch(r"[\w.\-]{1,60}", t) else ""


def _fecha(texto) -> datetime | None:
    if not isinstance(texto, str) or not texto:
        return None
    try:
        return desde_local(texto)
    except (ValueError, OverflowError):
        return None


def ruta_nueva(rel) -> str:
    rel = _s(rel).replace("\\", "/")
    for viejo, nuevo in PREFIJOS:
        if rel.startswith(viejo):
            return nuevo + rel[len(viejo):]
    return rel


def _copiar_arbol(src: Path, dst: Path) -> int:
    n = 0
    if not src.is_dir():
        return 0
    for f in src.rglob("*"):
        if f.is_file() and not f.is_symlink():
            q = dst / f.relative_to(src)
            if not q.exists():
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, q)
                n += 1
    return n


class _Registro:
    """Cuenta lo importado y lo salteado; cada registro va en su propia transacción."""

    def __init__(self):
        self.res = {"cuentas": 0, "tokens": 0, "medios": 0, "comentarios": 0, "piezas": 0, "chat": 0, "archivos": 0,
                    "material_meta": 0, "copias": 0, "links": 0, "salteados": 0}

    def intentar(self, clave: str | None, f) -> bool:
        try:
            f()
        except Exception as ex:   # un registro roto no frena la migración
            log.warning("migrar: salteado (%s): %s", clave, ex)
            self.res["salteados"] += 1
            return False
        if clave:
            self.res[clave] += 1
        return True


# --- migrar ---------------------------------------------------------------------------------------------------------------------
def migrar(carpeta: str, copiar_archivos: bool = True) -> dict:
    base = Path(carpeta)
    if not (base / "cuentas").is_dir() and not (base / "datos").is_dir():
        raise ValueError("Esa carpeta no parece de Hookline Lite (no tiene cuentas/ ni datos/).")
    r = _Registro()
    cfg = _leer(base / "datos" / "config.json", {})
    if _s(cfg.get("app_id")):
        guardar_ajuste("meta_app_id", re.sub(r"\D", "", _s(cfg["app_id"])))
    if _s(cfg.get("app_secret")):
        guardar_ajuste("meta_app_secret", _s(cfg["app_secret"]).strip(), cifrado=True)

    cuentas_dir = base / "cuentas"
    infos = {}
    for d in sorted(cuentas_dir.iterdir()) if cuentas_dir.is_dir() else []:
        info = _leer(d / "cuenta.json", None)
        if d.is_dir() and isinstance(info, dict) and d.name != "ejemplo" and _id(d.name):
            infos[d.name] = (d, info)
    # primero las main, después las multicuentas (por la FK main_id)
    for cid, (d, info) in sorted(infos.items(), key=lambda kv: bool(kv[1][1].get("main"))):
        _cuenta(r, cid, d, info, infos)
    for x in _dicts(_leer(base / "datos" / "piezas.json", [])):
        r.intentar("piezas", lambda x=x: _pieza(x, infos)) if _id(x.get("id")) and isinstance(x.get("cuenta"), str) and x["cuenta"] in infos else None
    for m in _dicts(_leer(base / "datos" / "chat.json", []))[-400:]:
        if m.get("de") != "pedido" and _s(m.get("texto")):
            r.intentar("chat", lambda m=m: _guardar(MensajeChat(de=_s(m.get("de"), 12) or "panel", texto=_s(m["texto"], 6000))))

    mat = base / "Material"
    if copiar_archivos:
        r.res["archivos"] += _copiar_arbol(base / "Para subir", almacen.raiz() / "para-subir")
        for d in (mat.iterdir() if mat.is_dir() else []):
            if d.is_dir() and d.name != "_copias" and _id(d.name.replace(" ", "_")):
                r.res["archivos"] += _copiar_arbol(d, almacen.raiz() / "material" / d.name)
        r.res["archivos"] += _copiar_arbol(mat / "_copias", almacen.raiz() / "copias")
    for d in (mat.iterdir() if mat.is_dir() else []):
        meta = _leer(d / "material.json", {}) if d.is_dir() else {}
        for clave, v in meta.items():
            if isinstance(clave, str) and "/" in clave and isinstance(v, dict):
                r.intentar("material_meta", lambda d=d, clave=clave, v=v: _meta(d.name, clave, v))
    cop = mat / "_copias"
    for d in (cop.iterdir() if cop.is_dir() else []):
        c = _leer(d / "copia.json", {}) if d.is_dir() else {}
        if re.fullmatch(r"[0-9a-f]{8}", _s(c.get("id"))):
            r.intentar("copias", lambda c=c: _merge(Copia(id=c["id"], cuenta_id=_s(c.get("cuenta"), 40),
                                                          estado=_s(c.get("estado"), 15) or "lista", link=_s(c.get("link")),
                                                          datos=c, creado=_fecha(c.get("creado")) or _ahora())))
    for x in _dicts((_leer(cop / "_links.json", {}) or {}).get("links")):
        if _s(x.get("link")):
            r.intentar("links", lambda x=x: _link(x))
    if (base / "buscador" / "fuentes.txt").exists():
        try:
            from .servicios import buscador
            r.res["fuentes"] = buscador.sumar_fuentes((base / "buscador" / "fuentes.txt").read_text(encoding="utf-8").splitlines())
        except Exception as ex:
            log.warning("migrar fuentes: %s", ex)
    return r.res


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _guardar(obj) -> None:
    with sesion() as s:
        s.add(obj)


def _merge(obj) -> None:
    with sesion() as s:
        s.merge(obj)


def _cuenta(r: _Registro, cid: str, d: Path, info: dict, infos: dict) -> None:
    est = _leer(d / "estado.json", {})
    tok = _leer(d / "token.json", {})

    def guardar():
        with sesion() as s:
            c = s.get(Cuenta, cid) or Cuenta(id=cid)
            c.nombre = _s(info.get("nombre") or est.get("nombre"), 200)
            c.nicho, c.tono, c.apodo = _s(info.get("nicho"), 300), _s(info.get("tono"), 300), _s(info.get("apodo"), 300)
            main = _s(info.get("main"), 40)
            c.main_id = main if main and main in infos and main != cid else None
            c.activa_desde = _s(info.get("activa_desde"))[:10]
            c.creada = _fecha(info.get("creada")) or c.creada or _ahora()
            c.usuario_ig = _s(est.get("usuario") or info.get("usuario"), 40) or cid
            c.tipo_cuenta, c.foto = _s(est.get("tipo_cuenta"), 30), _s(est.get("foto"))
            c.seguidores, c.siguiendo, c.publicaciones = _i(est.get("seguidores")), _i(est.get("siguiendo")), _i(est.get("publicaciones"))
            for col, k in (("insights_28d", "cuenta_28d"), ("limite", "limite"), ("metricas_ok", "metricas_ok")):
                setattr(c, col, est.get(k) if isinstance(est.get(k), dict) else None)
            c.ig_user_id = _s(tok.get("ig_id") or tok.get("user_id"), 40)
            s.add(c)
    if not r.intentar("cuentas", guardar):
        return
    if _s(tok.get("access_token")):
        def token():
            with sesion() as s:
                cr = s.get(CredencialIG, cid) or CredencialIG(cuenta_id=cid)
                cr.token_cifrado = seguridad.cifrar(_s(tok["access_token"]))
                cr.permisos = _s(tok.get("permisos"))
                cr.obtenido = _f(tok.get("obtenido")) or time.time()
                cr.refrescado = _f(tok.get("refrescado")) or cr.obtenido
                cr.vence = _f(tok.get("vence")) or 0.0
                cr.vencido = bool(tok.get("vencido") is True)
                s.add(cr)
        r.intentar("tokens", token)
    for i, m in enumerate(_dicts((_leer(d / "medios.json", {}) or {}).get("medios"))):
        if _id(m.get("id")):
            r.intentar("medios", lambda m=m, i=i: _merge(MedioIG(
                id=_id(m["id"]), cuenta_id=cid, caption=_s(m.get("caption")), tipo=_s(m.get("tipo"), 30),
                media_type=_s(m.get("media_type"), 30), reel=m.get("reel") is True, url=_s(m.get("url")),
                publicado=_fecha(m.get("publicado")), miniatura=_s(m.get("miniatura")), likes=_i(m.get("likes")) or 0,
                comentarios=_i(m.get("comentarios")) or 0, vistas=_i(m.get("vistas")), alcance=_i(m.get("alcance")),
                guardados=_i(m.get("guardados")), compartidos=_i(m.get("compartidos")),
                interacciones=_i(m.get("interacciones")), orden=i)))
    for h in _dicts(_leer(d / "historial.json", [])):
        f = _fecha(h.get("fecha"))
        if f:
            r.intentar(None, lambda h=h, f=f: _historial(cid, h, f))
    items = (_leer(d / "comentarios.json", {}) or {}).get("items")
    for k, c in (items.items() if isinstance(items, dict) else []):
        if isinstance(c, dict) and _id(k):
            r.intentar("comentarios", lambda k=k, c=c: _merge(Comentario(
                id=_id(k), cuenta_id=cid, media_id=_s(c.get("media"), 40), usuario=_s(c.get("usuario"), 80),
                texto=_s(c.get("texto")), fecha=_fecha(c.get("fecha")), likes=_i(c.get("likes")) or 0,
                respuestas=_i(c.get("respuestas")) or 0, respondido_por_mi=c.get("respondido_por_mi") is True,
                post=_s(c.get("post")), post_texto=_s(c.get("post_texto")), miniatura=_s(c.get("miniatura")),
                estado=_s(c.get("estado"), 20) if c.get("estado") in ("nuevo", "respondido", "oculto", "saltado") else "nuevo",
                clase=_s(c.get("clase"), 20), sugerencia=_s(c.get("sugerencia")), alerta=_s(c.get("alerta")),
                respuesta_enviada=_s(c.get("respuesta_enviada")), respondido=_fecha(c.get("respondido")))))
    t = _leer(d / "tendencias.json", {})
    if isinstance(t.get("items"), list) and t["items"]:
        r.intentar(None, lambda: _merge(Tendencias(cuenta_id=cid, fecha=_fecha(t.get("fecha")) or _ahora(),
                                                   nicho=_s(t.get("nicho"), 300), items=_dicts(t["items"]))))


def _historial(cid: str, h: dict, f: datetime) -> None:
    hora = f.strftime("%Y-%m-%dT%H")
    with sesion() as s:
        if not s.scalar(select(HistorialCuenta.id).where(HistorialCuenta.cuenta_id == cid, HistorialCuenta.hora == hora)):
            s.add(HistorialCuenta(cuenta_id=cid, hora=hora, fecha=f, seguidores=_i(h.get("seguidores")),
                                  vistas=_i(h.get("vistas")), publicaciones=_i(h.get("publicaciones"))))


def _pieza(x: dict, infos: dict) -> None:
    estado = _s(x.get("estado"), 15)
    estados = ("nueva", "jueces", "juzgada", "programada", "planificada", "publicada", "vencida", "error")
    error = _s(x.get("error"), 1000)
    if estado == "subiendo":   # Lite se cortó mientras publicaba: pudo haber salido. Que lo revise una persona.
        estado, error = "error", "Se importó desde Lite mientras se estaba subiendo: fijate en Instagram si salió."
    elif estado == "jueces":
        estado = "nueva"
    elif estado not in estados:
        estado = "nueva"
    tipo = _s(x.get("tipo"), 12)
    _merge(Pieza(id=_id(x["id"]), cuenta_id=x["cuenta"], tipo=tipo if tipo in ("historia", "reel", "carrusel", "foto") else "foto",
                 archivos=[ruta_nueva(a) for a in (x.get("archivos") if isinstance(x.get("archivos"), list) else [])
                           if isinstance(a, str)],
                 estado=estado, caption=_s(x.get("caption"), 2200), cuando=_fecha(x.get("cuando")),
                 ahora=x.get("ahora") is True, error=error, jueces=x.get("jueces") if isinstance(x.get("jueces"), dict) else None,
                 sugerido=_s(x.get("sugerido"), 16), aprobada=_fecha(x.get("aprobada")), media_id=_s(x.get("media_id"), 40),
                 permalink=_s(x.get("permalink")), publicado=_fecha(x.get("publicado")), subida=_s(x.get("subida"), 12),
                 plan=x.get("plan") is True, dia=_s(x.get("dia"), 10), copia_id=_s(x.get("copia"), 12), foto=_s(x.get("foto")),
                 creado=_fecha(x.get("creado")) or _ahora()))


def _meta(modelo: str, clave: str, v: dict) -> None:
    sec, nombre = clave.split("/", 1)
    et = [e for e in v.get("etiquetas") or [] if isinstance(e, str)] if isinstance(v.get("etiquetas"), list) else []
    meta = {k: w for k, w in v.items() if k != "etiquetas"}
    with sesion() as s:
        x = s.scalar(select(MaterialMeta).where(MaterialMeta.modelo == modelo, MaterialMeta.seccion == sec[:60],
                                                MaterialMeta.nombre == nombre[:200]))
        x = x or MaterialMeta(modelo=modelo[:40], seccion=sec[:60], nombre=nombre[:200])
        x.etiquetas, x.meta = et[:12], meta
        s.add(x)


def _link(x: dict) -> None:
    ficha = {k: x[k] for k in ("ficha", "dur", "titulo_orig", "mini", "error") if k in x}
    _merge(LinkBanco(link=_s(x["link"], 400), agregado=_fecha(x.get("agregado")) or _ahora(),
                     copia_id=_s(x.get("copia"), 12), titulo=_s(x.get("titulo")), cat=_s(x.get("cat"), 30),
                     voto=_s(x.get("voto"), 10), motivo=_s(x.get("motivo")), origen=_s(x.get("origen"), 20),
                     autor=_s(x.get("autor"), 80), datos=ficha))
