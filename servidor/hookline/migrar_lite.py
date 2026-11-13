"""Importa una carpeta de Hookline Lite a la base y al volumen del servicio. Se puede correr más de una vez
(lo que ya existe se actualiza, no se duplica).

Qué se importa:
  datos/config.json            → App ID y App Secret (cifrado) en ajustes
  cuentas/<id>/cuenta.json     → cuentas (nicho, tono, apodo, main, activa_desde)
  cuentas/<id>/token.json      → credenciales_ig (token CIFRADO; en Lite estaba en claro)
  cuentas/<id>/estado.json, medios.json, historial.json, comentarios.json, tendencias.json
  datos/piezas.json            → piezas (las horas locales pasan a UTC)
  datos/chat.json              → chat (últimos 400)
  Para subir/<cuenta>/         → para-subir/<cuenta>/
  Material/<modelo>/...        → material/<modelo>/...  (+ material.json → material_meta)
  Material/_copias/<id>/       → copias/<id>/ (+ copia.json → copias; _links.json → banco_links)
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import almacen, seguridad
from .comun import desde_local, guardar_ajuste
from .db import sesion
from .modelos import (Comentario, Copia, CredencialIG, Cuenta, HistorialCuenta, LinkBanco, MaterialMeta, MedioIG,
                      MensajeChat, Pieza, Tendencias)

PREFIJOS = [("Para subir/", "para-subir/"), ("Material/_copias/", "copias/"), ("Material/", "material/"),
            ("Resultados/", "resultados/")]


def _leer(p: Path, defecto):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return defecto


def ruta_nueva(rel: str) -> str:
    rel = str(rel or "").replace("\\", "/")
    for viejo, nuevo in PREFIJOS:
        if rel.startswith(viejo):
            return nuevo + rel[len(viejo):]
    return rel


def _fecha(texto) -> datetime | None:
    if not texto:
        return None
    try:
        return desde_local(str(texto))
    except ValueError:
        return None


def _copiar_arbol(src: Path, dst: Path) -> int:
    n = 0
    if not src.is_dir():
        return 0
    for f in src.rglob("*"):
        if f.is_file():
            q = dst / f.relative_to(src)
            if not q.exists():
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, q)
                n += 1
    return n


def migrar(carpeta: str, copiar_archivos: bool = True) -> dict:
    base = Path(carpeta)
    if not (base / "cuentas").is_dir() and not (base / "datos").is_dir():
        raise ValueError("Esa carpeta no parece de Hookline Lite (no tiene cuentas/ ni datos/).")
    res = {"cuentas": 0, "tokens": 0, "medios": 0, "comentarios": 0, "piezas": 0, "chat": 0, "archivos": 0,
           "material_meta": 0, "copias": 0, "links": 0}
    cfg = _leer(base / "datos" / "config.json", {})
    if cfg.get("app_id"):
        guardar_ajuste("meta_app_id", str(cfg["app_id"]))
    if cfg.get("app_secret"):
        guardar_ajuste("meta_app_secret", str(cfg["app_secret"]), cifrado=True)

    cuentas_dir = base / "cuentas"
    infos = {}
    for d in sorted(cuentas_dir.iterdir()) if cuentas_dir.is_dir() else []:
        info = _leer(d / "cuenta.json", None)
        if d.is_dir() and isinstance(info, dict) and d.name != "ejemplo":
            infos[d.name] = (d, info)
    # primero las main, después las multicuentas (por la FK main_id)
    for cid, (d, info) in sorted(infos.items(), key=lambda kv: bool(kv[1][1].get("main"))):
        est = _leer(d / "estado.json", {})
        with sesion() as s:
            c = s.get(Cuenta, cid) or Cuenta(id=cid)
            c.nombre = info.get("nombre") or est.get("nombre") or ""
            c.nicho, c.tono, c.apodo = info.get("nicho") or "", info.get("tono") or "", info.get("apodo") or ""
            c.main_id = info.get("main") if info.get("main") in infos else None
            c.activa_desde = str(info.get("activa_desde") or "")[:10]
            c.creada = _fecha(info.get("creada")) or c.creada or datetime.now(timezone.utc)
            c.usuario_ig = est.get("usuario") or info.get("usuario") or cid
            c.tipo_cuenta, c.foto = est.get("tipo_cuenta") or "", est.get("foto") or ""
            c.seguidores, c.siguiendo, c.publicaciones = est.get("seguidores"), est.get("siguiendo"), est.get("publicaciones")
            c.insights_28d, c.limite, c.metricas_ok = est.get("cuenta_28d"), est.get("limite"), est.get("metricas_ok")
            tok = _leer(d / "token.json", {})
            c.ig_user_id = str(tok.get("ig_id") or tok.get("user_id") or "")
            s.add(c)
        res["cuentas"] += 1
        if tok.get("access_token"):
            with sesion() as s:
                cr = s.get(CredencialIG, cid) or CredencialIG(cuenta_id=cid)
                cr.token_cifrado = seguridad.cifrar(tok["access_token"])
                cr.permisos = tok.get("permisos") or ""
                cr.obtenido = float(tok.get("obtenido") or time.time())
                cr.refrescado = float(tok.get("refrescado") or cr.obtenido)
                cr.vence = float(tok.get("vence") or 0)
                cr.vencido = bool(tok.get("vencido"))
                s.add(cr)
            res["tokens"] += 1
        for i, m in enumerate((_leer(d / "medios.json", {}) or {}).get("medios") or []):
            with sesion() as s:
                s.merge(MedioIG(id=str(m["id"]), cuenta_id=cid, caption=m.get("caption") or "", tipo=m.get("tipo") or "",
                                media_type=m.get("media_type") or "", reel=bool(m.get("reel")), url=m.get("url") or "",
                                publicado=_fecha(m.get("publicado")), miniatura=m.get("miniatura") or "",
                                likes=int(m.get("likes") or 0), comentarios=int(m.get("comentarios") or 0),
                                vistas=m.get("vistas"), alcance=m.get("alcance"), guardados=m.get("guardados"),
                                compartidos=m.get("compartidos"), interacciones=m.get("interacciones"), orden=i))
            res["medios"] += 1
        for h in _leer(d / "historial.json", []) or []:
            f = _fecha(h.get("fecha"))
            if not f:
                continue
            hora = f.strftime("%Y-%m-%dT%H")
            with sesion() as s:
                from sqlalchemy import select
                if not s.scalar(select(HistorialCuenta.id).where(HistorialCuenta.cuenta_id == cid, HistorialCuenta.hora == hora)):
                    s.add(HistorialCuenta(cuenta_id=cid, hora=hora, fecha=f, seguidores=h.get("seguidores"),
                                          vistas=h.get("vistas"), publicaciones=h.get("publicaciones")))
        for k, c in ((_leer(d / "comentarios.json", {}) or {}).get("items") or {}).items():
            with sesion() as s:
                s.merge(Comentario(id=str(k), cuenta_id=cid, media_id=str(c.get("media") or ""), usuario=c.get("usuario") or "",
                                   texto=c.get("texto") or "", fecha=_fecha(c.get("fecha")), likes=int(c.get("likes") or 0),
                                   respuestas=int(c.get("respuestas") or 0), respondido_por_mi=bool(c.get("respondido_por_mi")),
                                   post=c.get("post") or "", post_texto=c.get("post_texto") or "",
                                   miniatura=c.get("miniatura") or "", estado=c.get("estado") or "nuevo",
                                   clase=c.get("clase") or "", sugerencia=c.get("sugerencia") or "",
                                   alerta=c.get("alerta") or "", respuesta_enviada=c.get("respuesta_enviada") or "",
                                   respondido=_fecha(c.get("respondido"))))
            res["comentarios"] += 1
        t = _leer(d / "tendencias.json", None)
        if isinstance(t, dict) and t.get("items"):
            with sesion() as s:
                s.merge(Tendencias(cuenta_id=cid, fecha=_fecha(t.get("fecha")) or datetime.now(timezone.utc),
                                   nicho=t.get("nicho") or "", items=t["items"]))

    for x in _leer(base / "datos" / "piezas.json", []) or []:
        if x.get("cuenta") not in infos:
            continue
        estado = x.get("estado") or "nueva"
        if estado == "subiendo":
            estado = "programada"
        with sesion() as s:
            s.merge(Pieza(id=x["id"], cuenta_id=x["cuenta"], tipo=x.get("tipo") or "foto",
                          archivos=[ruta_nueva(a) for a in x.get("archivos") or []], estado=estado,
                          caption=x.get("caption") or "", cuando=_fecha(x.get("cuando")), ahora=bool(x.get("ahora")),
                          error=x.get("error") or "", jueces=x.get("jueces"), sugerido=x.get("sugerido") or "",
                          aprobada=_fecha(x.get("aprobada")), media_id=x.get("media_id") or "",
                          permalink=x.get("permalink") or "", publicado=_fecha(x.get("publicado")),
                          subida=x.get("subida") or "", plan=bool(x.get("plan")), dia=x.get("dia") or "",
                          copia_id=x.get("copia") or "", foto=x.get("foto") or "",
                          creado=_fecha(x.get("creado")) or datetime.now(timezone.utc)))
        res["piezas"] += 1

    for m in (_leer(base / "datos" / "chat.json", []) or [])[-400:]:
        if m.get("de") == "pedido":
            continue   # los pedidos al puente viejo no tienen sentido acá
        with sesion() as s:
            s.add(MensajeChat(de=m.get("de") or "panel", texto=m.get("texto") or ""))
        res["chat"] += 1

    if copiar_archivos:
        res["archivos"] += _copiar_arbol(base / "Para subir", almacen.raiz() / "para-subir")
        mat = base / "Material"
        if mat.is_dir():
            for d in mat.iterdir():
                if d.is_dir() and d.name != "_copias":
                    res["archivos"] += _copiar_arbol(d, almacen.raiz() / "material" / d.name)
            res["archivos"] += _copiar_arbol(mat / "_copias", almacen.raiz() / "copias")
    mat = base / "Material"
    for d in (mat.iterdir() if mat.is_dir() else []):
        meta = _leer(d / "material.json", {}) if d.is_dir() else {}
        for clave, v in (meta or {}).items():
            if "/" not in clave or not isinstance(v, dict):
                continue
            sec, nombre = clave.split("/", 1)
            et = v.pop("etiquetas", []) if isinstance(v.get("etiquetas"), list) else []
            with sesion() as s:
                from sqlalchemy import select
                x = s.scalar(select(MaterialMeta).where(MaterialMeta.modelo == d.name, MaterialMeta.seccion == sec,
                                                        MaterialMeta.nombre == nombre))
                x = x or MaterialMeta(modelo=d.name, seccion=sec, nombre=nombre)
                x.etiquetas, x.meta = et, v
                s.add(x)
            res["material_meta"] += 1
    cop = mat / "_copias"
    for d in (cop.iterdir() if cop.is_dir() else []):
        c = _leer(d / "copia.json", None) if d.is_dir() else None
        if not isinstance(c, dict) or not c.get("id"):
            continue
        with sesion() as s:
            s.merge(Copia(id=c["id"], cuenta_id=c.get("cuenta") or "", estado=c.get("estado") or "lista",
                          link=c.get("link") or "", datos=c,
                          creado=_fecha(c.get("creado")) or datetime.now(timezone.utc)))
        res["copias"] += 1
    for x in (_leer(cop / "_links.json", {}) or {}).get("links") or []:
        if not x.get("link"):
            continue
        ficha = {k: x[k] for k in ("ficha", "dur", "titulo_orig", "mini", "error") if k in x}
        with sesion() as s:
            s.merge(LinkBanco(link=x["link"][:400], agregado=_fecha(x.get("agregado")) or datetime.now(timezone.utc),
                              copia_id=x.get("copia") or "", titulo=x.get("titulo") or "", cat=x.get("cat") or "",
                              voto=x.get("voto") or "", motivo=x.get("motivo") or "", origen=x.get("origen") or "",
                              autor=x.get("autor") or "", datos=ficha))
        res["links"] += 1
    if (base / "buscador" / "fuentes.txt").exists():
        try:
            from .servicios import buscador
            res["fuentes"] = buscador.sumar_fuentes((base / "buscador" / "fuentes.txt").read_text(encoding="utf-8").splitlines())
        except (ImportError, AttributeError):
            pass
    return res
