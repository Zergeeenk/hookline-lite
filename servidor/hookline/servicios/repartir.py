"""Repartir / encargo: el cronograma del día para cada modelo y sus multicuentas. Portado de Lite (panel.py).

- Reels aprobados por el juez, DISTINTOS a cada multicuenta (nunca el mismo en dos cuentas).
- Historias del día a día de su modelo a las multicuentas, sin repetir fotos; de noche, fotos de noche.
- La main es la cuenta oficial: ese contenido lo sube ella; la fábrica solo trabaja en las multicuentas.
- Calentamiento: una multicuenta nueva no publica a full de entrada (semana 1 → 1+1, semana 2 → 2+2).

Aprobación (cambio respecto de Lite, ver REQUERIMIENTOS §11.5), ajuste "encargo_aprueba":
  False (por defecto): las piezas quedan "Para aprobar" en la Bandeja (estado 'juzgada', con la hora sugerida).
  True: el clic en "Mandar a trabajar" cuenta como aprobación: conectada → 'programada'; sin conectar → 'planificada'.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta

from sqlalchemy import select

from .. import almacen, medios
from ..comun import actividad, ajuste, desde_local, iso_local
from ..config import config
from ..db import ahora, sesion
from ..modelos import Encargo, Pieza
from . import cuentas as svc_cuentas, fabrica, material, piezas as svc_piezas

HORAS_REELS = ["12:30", "14:00", "16:00", "18:30", "20:00", "21:30", "23:00", "00:30"]   # hora local (Argentina)
HORAS_HISTORIAS = ["09:30", "11:00", "13:30", "15:00", "17:00", "19:30", "21:00", "22:30", "01:00"]
CALENTAMIENTO = [(7, 1, 1), (14, 2, 2)]   # (hasta el día N de vida, reels, historias); después, lo pedido


def _es_noche(hora: str) -> bool:
    h = int(hora[:2])
    return h >= 19 or h < 6


def _ahora_local() -> datetime:
    return datetime.now(config().tz)


def _horarios(base: list[str], n: int, i: int, dia: str) -> list[datetime]:
    """n horarios distintos para la cuenta número i (cada cuenta corrida un poco). Si es hoy, solo los que no pasaron."""
    tz = config().tz
    ya = _ahora_local()
    out = []
    paso = max(1, len(base) // max(1, n))
    orden = [(i + k * paso) % len(base) for k in range(len(base))]
    orden += [x for x in range(len(base)) if x not in orden]
    for k in orden:
        hh, mm = map(int, base[k].split(":"))
        mm += (i * 17) % 50
        hh, mm = (hh + mm // 60) % 24, mm % 60
        cuando = datetime.fromisoformat(f"{dia}T{hh:02d}:{mm:02d}").replace(tzinfo=tz)
        if hh < 6:
            cuando += timedelta(days=1)   # 00:30 / 01:00 = esa misma noche
        if cuando < ya + timedelta(minutes=15) or any(abs((cuando - o).total_seconds()) < 3600 for o in out):
            continue
        out.append(cuando)
        if len(out) >= n:
            break
    return sorted(out)


def calentamiento(c: dict, dia: str, reels: int, historias: int) -> tuple[int, int]:
    desde = str(c.get("activa_desde") or c.get("creada") or "")[:10]
    try:
        edad = (datetime.fromisoformat(dia) - datetime.fromisoformat(desde)).days
    except ValueError:
        return reels, historias
    for hasta, r, h in CALENTAMIENTO:
        if edad < hasta:
            return min(reels, r), min(historias, h)
    return reels, historias


def _libres(candidatos: list[datetime], ocupadas: list[datetime], n: int) -> list[datetime]:
    out = []
    for h in candidatos:
        if all(abs((h - o).total_seconds()) >= 3600 for o in ocupadas + out):
            out.append(h)
        if len(out) >= n:
            break
    return sorted(out)


def _hora_de(x: Pieza) -> datetime | None:
    if x.cuando:
        return x.cuando.astimezone(config().tz)
    if x.sugerido:
        try:
            return datetime.fromisoformat(x.sugerido[:16]).replace(tzinfo=config().tz)
        except ValueError:
            return None
    return None


def _estado_pieza(c: dict, cuando: datetime, usuario: str) -> dict:
    hora = cuando.strftime("%Y-%m-%dT%H:%M")
    if not ajuste("encargo_aprueba", False):
        return {"estado": "juzgada", "sugerido": hora, "cuando": None}
    if c["conectada"] and c["puede_publicar"]:
        return {"estado": "programada", "cuando": desde_local(hora), "sugerido": hora, "aprobada": ahora(),
                "aprobada_por": f"encargo:{usuario or 'panel'}"}
    if c["conectada"]:
        return {"estado": "juzgada", "sugerido": hora, "cuando": None}
    return {"estado": "planificada", "cuando": desde_local(hora), "sugerido": hora}


def repartir(dia: str, reels: int = 3, historias: int = 2, usuario: str = "") -> dict:
    datetime.fromisoformat(dia)
    cs = sorted(svc_cuentas.listar(), key=lambda c: (c.get("main") or c["id"], bool(c.get("main")), c["id"]))
    hechos = {"reels": 0, "historias": 0, "faltan": []}
    copias = fabrica.listar()
    with svc_piezas._lock, sesion() as s:
        lista = s.scalars(select(Pieza)).all()
        ya_repartidas = {x.copia_id for x in lista if x.copia_id}
        fotos_usadas = {x.foto for x in lista if x.foto}

        def del_dia(cid, tipo):
            return [x for x in lista if x.cuenta_id == cid and x.plan and x.dia == dia and x.tipo == tipo]

        def ocupadas(cid):
            return [h for h in (_hora_de(x) for x in lista if x.cuenta_id == cid) if h]

        for i, c in enumerate(cs):
            if not c.get("main"):
                continue   # la main es la influencer: ese contenido lo sube ella
            m = c["main"]
            cr, ch = calentamiento(c, dia, reels, historias)
            # reels
            faltan_r = cr - len(del_dia(c["id"], "reel"))
            if faltan_r > 0:
                pool = sorted([x for x in copias if x.get("resultado") and (x.get("juez") or {}).get("estado") == "aprobado"
                               and svc_cuentas.modelo_de(x.get("cuenta") or "") == m and x["id"] not in ya_repartidas
                               and not (x.get("marca_agua") and not x.get("remate"))],
                              key=lambda x: x.get("armado") or "")
                cands = _horarios(HORAS_REELS, reels, i, dia) + _horarios(HORAS_REELS, len(HORAS_REELS), i, dia)
                for cuando in _libres(cands, ocupadas(c["id"]), faltan_r):
                    if not pool:
                        hechos["faltan"].append(f"@{c['usuario']}: faltan reels aprobados de @{m}")
                        break
                    x = pool.pop(0)
                    ya_repartidas.add(x["id"])
                    dst = almacen.libre(almacen.dir_de("subir", c["id"]) / f".reel-{x['id']}.mp4")
                    shutil.copy2(fabrica.resultado(x["id"]), dst)
                    hora = cuando.strftime("%H:%M")
                    actividad("programando", f"Reel para @{c['usuario']} a las {hora}", img=almacen.relativa(dst),
                              cuenta=c["id"], copia=x["id"])
                    p = Pieza(id=svc_piezas.nuevo_id(), cuenta_id=c["id"], tipo="reel", archivos=[almacen.relativa(dst)],
                              plan=True, dia=dia, copia_id=x["id"], **_estado_pieza(c, cuando, usuario))
                    s.add(p)
                    lista.append(p)
                    hechos["reels"] += 1
            # historias: fotos del día a día de su modelo, nunca la misma en dos cuentas, de noche a la noche
            faltan_h = ch - len(del_dia(c["id"], "historia"))
            if faltan_h > 0:
                todas = [{**x, "_sec": k} for _, et, k, x in material.material_en(m, material.CARPETAS_HISTORIAS + ("Fotos",))
                         if medios.es_foto(x["nombre"]) and f"{m}/{x['nombre']}" not in fotos_usadas
                         and (k != "Fotos" or "noche" in et)]
                cands = _horarios(HORAS_HISTORIAS, historias, i, dia) + _horarios(HORAS_HISTORIAS, len(HORAS_HISTORIAS), i, dia)
                for cuando in _libres(cands, ocupadas(c["id"]), faltan_h):
                    hora = cuando.strftime("%H:%M")
                    noche = _es_noche(hora)
                    elegibles = [x for x in todas if ("noche" in (x.get("etiquetas") or [])) == noche] or \
                                ([x for x in todas if "noche" not in (x.get("etiquetas") or [])] if not noche else [])
                    if not elegibles:
                        hechos["faltan"].append(f"@{c['usuario']} {hora}: faltan fotos {'de noche' if noche else 'de día'} de @{m}")
                        continue
                    f = elegibles[0]
                    todas.remove(f)
                    fotos_usadas.add(f"{m}/{f['nombre']}")
                    dst = almacen.libre(almacen.dir_de("subir", c["id"]) / f".hist-{f['nombre']}")
                    shutil.copy2(material.carpeta(m, f["_sec"]) / f["nombre"], dst)
                    actividad("programando", f"Historia para @{c['usuario']} a las {hora}", img=almacen.relativa(dst),
                              cuenta=c["id"])
                    p = Pieza(id=svc_piezas.nuevo_id(), cuenta_id=c["id"], tipo="historia", archivos=[almacen.relativa(dst)],
                              plan=True, dia=dia, foto=f"{m}/{f['nombre']}", **_estado_pieza(c, cuando, usuario))
                    s.add(p)
                    lista.append(p)
                    hechos["historias"] += 1
    return hechos


def _ultimo() -> Encargo | None:
    with sesion() as s:
        return s.scalar(select(Encargo).order_by(Encargo.id.desc()).limit(1))


def encargo_actual() -> dict:
    e = _ultimo()
    if not e:
        return {}
    return {"dia": e.dia, "reels": e.reels, "historias": e.historias, "estado": e.estado, "creado": iso_local(e.creado),
            "pedidos": e.pedidos or {}, "aprobado_por": e.aprobado_por}


def encargo(dia: str, reels, historias, usuario: str = "") -> dict:
    """'Mandar a trabajar': reparte lo que ya hay y, si faltan reels aprobados, le pide a la fábrica que haga más con
    los memes guardados. Mientras esté 'trabajando', el worker sigue completando solo (seguir_encargo)."""
    datetime.fromisoformat(dia)
    reels, historias = max(0, min(6, int(reels))), max(0, min(6, int(historias)))
    with sesion() as s:
        e = Encargo(dia=dia, reels=reels, historias=historias, estado="trabajando", pedidos={},
                    aprobado_por=usuario if ajuste("encargo_aprueba", False) else "")
        s.add(e)
        s.flush()
        eid = e.id
    actividad("encargo", f"Encargo para el {dia}: {reels} reels por multicuenta y {historias} historias por cuenta")
    hechos = repartir(dia, reels, historias, usuario)
    cs = svc_cuentas.listar()
    pedidos = {}
    with sesion() as s:
        del_dia = s.scalars(select(Pieza).where(Pieza.dia == dia, Pieza.tipo == "reel")).all()
    copias = fabrica.listar()
    for m in [c for c in cs if not c.get("main")]:
        hijas = [c for c in cs if c.get("main") == m["id"]]
        if not hijas:
            continue
        puestos = sum(1 for x in del_dia if x.cuenta_id in {h["id"] for h in hijas})
        en_camino = sum(1 for x in copias if svc_cuentas.modelo_de(x.get("cuenta") or "") == m["id"]
                        and x.get("estado") in fabrica.ACTIVOS)
        falta = len(hijas) * reels - puestos - en_camino
        if falta > 0:
            try:
                n = len(fabrica.hacer_guardados(falta + 1, m["id"]))   # uno de más por si el juez tacha alguno
                pedidos[m["id"]] = n
                actividad("pedido", f"Pidiendo {n} reels nuevos de @{m['usuario']} a la fábrica")
            except ValueError as ex:
                actividad("aviso", f"@{m['usuario']}: {ex}")
    with sesion() as s:
        s.get(Encargo, eid).pedidos = pedidos
    return hechos


def seguir_encargo() -> None:
    """Periódico: si hay un encargo trabajando, va repartiendo los reels que el juez aprueba."""
    e = _ultimo()
    if not e or e.estado != "trabajando":
        return
    h = repartir(e.dia, e.reels, e.historias, e.aprobado_por)
    falta = 0
    with sesion() as s:
        ps = s.scalars(select(Pieza).where(Pieza.dia == e.dia)).all()
    for c in svc_cuentas.listar():
        if not c.get("main"):
            continue
        mias = [x for x in ps if x.cuenta_id == c["id"]]
        cr, ch = calentamiento(c, e.dia, e.reels, e.historias)
        falta += max(0, cr - sum(1 for x in mias if x.tipo == "reel"))
        falta += max(0, ch - sum(1 for x in mias if x.tipo == "historia"))
    ocupadas = any(x.get("estado") in fabrica.ACTIVOS or (x.get("juez") or {}).get("estado") == "mirando"
                   for x in fabrica.listar())
    with sesion() as s:
        x = s.get(Encargo, e.id)
        if not falta:
            x.estado = "listo"
            actividad("listo_encargo", f"Listo el cronograma del {e.dia}: todo repartido")
        elif not ocupadas and not h["reels"] and not h["historias"] and ahora() - e.creado > timedelta(minutes=10):
            x.estado = "incompleto"
            actividad("aviso", f"El encargo quedó incompleto: faltan {falta} piezas (faltan memes o fotos del día a día)")
