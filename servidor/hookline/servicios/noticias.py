"""Espacio "Noticias" (cuentas como @loqueborraron): Radar → Guion → Jurado, con chequeo automático y costo por reel.

  Radar   (web, una vez por día o a pedido): temas de las últimas 12-24 h en las áreas de la cuenta, con urgencia y
          potencial; no repite temas de los últimos 14 días.
  Guion   un reel hablado de 60-75 s: gancho de 3 s, "Seguime y dale like" entre el segundo 33 y el 43, cierre con dato.
          Lo revisa `chequear()` (sin IA: cuenta palabras, busca frases repetidas, palabras prohibidas, datos ya usados).
  Jurado  10 personas (edades, libertarios, peronistas, indeciso...) puntúan y dicen en qué segundo se irían; aplica las 3
          mejoras más importantes. Si el promedio queda por debajo del umbral (5,5), el guion se descarta.
  Costos  lo que costó cada etapa y cada reel, sacado de los tokens que devuelve la API (TareaIA.uso) y los precios.

Nada de esto publica: el guion aprobado queda listo para voz y render (próximas etapas) y la dueña aprueba.
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..comun import ajuste, aviso, candado, evento, guardar_ajuste, iso_local, local
from ..config import config
from ..db import ahora, sesion
from ..ia import prompts, tareas
from ..modelos import Cuenta, Guion, Noticia, TareaIA
from ..trabajos import encolar, manejador

log = logging.getLogger("hookline.noticias")
ESPACIOS = ("modelos", "noticias")
DIAS_MEMORIA = 14                     # no repetir temas ni datos de las últimas 2 semanas

# Precios en dólares por millón de tokens (API de Anthropic) y por búsqueda web. Se pueden cambiar en Ajustes.
PRECIOS = {
    "claude-opus-5-5": {"entrada": 4.0, "salida": 20.0, "cache": 0.20},
    "claude-sonnet-5-5": {"entrada": 2.0, "salida": 10.0, "cache": 0.10},
    "claude-haiku-5-5": {"entrada": 0.10, "salida": 0.50, "cache": 0.01},
    "claude-fable-5-2": {"entrada": 8.0, "salida": 40.0, "cache": 0.20},
}
PRECIO_BUSQUEDA = 0.01                # USD 10 cada 1000 búsquedas
ESCRITURA_CACHE = 1.25                # escribir en la caché cuesta 1,25 × la entrada

DEFECTO = {
    "areas": list(prompts.AREAS_NOTICIAS),
    "hora_radar": 8,                  # hora local del radar automático
    "auto_guiones": 1,                # cuántos temas del radar automático pasan solos a guion (0 = ninguno)
    "pps": 2.5,                       # palabras por segundo de la locución
    "min_s": 60, "max_s": 75,
    "gancho_s": 3,
    "cta": "Seguime y dale like", "cta_desde": 33, "cta_hasta": 43,
    "umbral": 5.5,                    # nota mínima del jurado
    "prohibidas": ["hola a todos", "en el día de hoy", "matar", "muerte a", "golpe de estado", "fraude electoral",
                   "drogadicto", "delincuente", "corrupto", "asesino", "pelotudo", "boludo"],
    "presupuesto_usd": 0.0,           # por mes (0 = sin límite); al 80 % avisa, al 100 % frena lo automático
    "precio_busqueda": PRECIO_BUSQUEDA,
    "precios": {},                    # modelo → {entrada, salida, cache}, pisa los de PRECIOS
}

NUMEROS = {"cero", "uno", "una", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once",
           "doce", "quince", "veinte", "treinta", "cuarenta", "cincuenta", "cien", "ciento", "mil", "millon",
           "millones", "billon", "billones", "mitad", "doble", "triple"}
VACIAS = {"el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "a", "en", "y", "o", "que", "por",
          "para", "con", "sin", "se", "su", "sus", "lo", "le", "les", "es", "son", "fue", "como", "mas", "pero", "ya",
          "hoy", "ayer", "este", "esta", "esto", "sobre", "tras", "entre", "hasta", "desde", "no", "si"}


# --- configuración --------------------------------------------------------------------------------------------------------
def conf() -> dict:
    guardada = ajuste("noticias_config") or {}
    c = dict(DEFECTO)
    if isinstance(guardada, dict):
        c.update({k: v for k, v in guardada.items() if k in DEFECTO})
    return c


def _num(v, minimo: float, maximo: float, entero: bool = False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError("Escribí un número.")
    if math.isnan(x) or math.isinf(x) or not minimo <= x <= maximo:
        raise ValueError(f"Tiene que estar entre {minimo:g} y {maximo:g}.")
    return int(round(x)) if entero else round(x, 4)


def _lista_textos(v, largo: int = 120, n: int = 60) -> list[str]:
    if isinstance(v, str):
        v = v.split("\n")
    if not isinstance(v, list):
        raise ValueError("Mandá una lista.")
    out = []
    for x in v:
        t = " ".join(str(x or "").split())[:largo]
        if t and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out[:n]


def guardar_conf(d: dict) -> dict:
    """Valida cada campo: lo que manda el panel (o una integración) nunca rompe el chequeo ni los costos."""
    c = {k: v for k, v in (ajuste("noticias_config") or {}).items() if k in DEFECTO} \
        if isinstance(ajuste("noticias_config"), dict) else {}
    enteros = {"hora_radar": (0, 23), "auto_guiones": (0, 5), "min_s": (15, 180), "max_s": (15, 180),
               "gancho_s": (1, 10), "cta_desde": (0, 180), "cta_hasta": (0, 180)}
    reales = {"pps": (1.0, 5.0), "umbral": (1.0, 10.0), "presupuesto_usd": (0.0, 100000.0),
              "precio_busqueda": (0.0, 1.0)}
    for k, (a, b) in enteros.items():
        if k in d:
            c[k] = _num(d[k], a, b, entero=True)
    for k, (a, b) in reales.items():
        if k in d:
            c[k] = _num(d[k], a, b)
    if "areas" in d:
        areas = _lista_textos(d["areas"], 80, 8)
        if not areas:
            raise ValueError("Dejá al menos un área para el radar.")
        c["areas"] = areas
    if "prohibidas" in d:
        c["prohibidas"] = _lista_textos(d["prohibidas"], 60, 200)
    if "cta" in d:
        cta = " ".join(str(d["cta"] or "").split())[:60]
        if len(_palabras(cta)) < 2:
            raise ValueError("La frase para pedir el seguimiento tiene que tener al menos 2 palabras.")
        c["cta"] = cta
    if "precios" in d:
        if not isinstance(d["precios"], dict):
            raise ValueError("Precios: mandá {modelo: {entrada, salida, cache}}.")
        precios = {}
        for m, p in list(d["precios"].items())[:20]:
            m = str(m).strip()[:60]
            if not re.fullmatch(r"[a-z0-9.\-]+", m) or not isinstance(p, dict):
                raise ValueError("Precios: modelo inválido.")
            precios[m] = {k: _num(p.get(k, 0), 0, 1000) for k in ("entrada", "salida", "cache")}
        c["precios"] = precios
    final = {**DEFECTO, **c}
    if final["min_s"] >= final["max_s"]:
        raise ValueError("La duración mínima tiene que ser menor que la máxima.")
    if not (final["cta_desde"] < final["cta_hasta"] <= final["max_s"]):
        raise ValueError("El momento del 'seguime' tiene que estar dentro del reel (desde < hasta ≤ duración máxima).")
    guardar_ajuste("noticias_config", c)
    return conf()


# --- texto ------------------------------------------------------------------------------------------------------------------
def normalizar(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w%$]+", " ", t).split())


def _palabras(t: str) -> list[str]:
    return [w for w in re.findall(r"\S+", str(t or "")) if re.search(r"\w", w)]


def _oraciones(t: str) -> list[str]:
    return [o.strip() for o in re.split(r"(?<=[.!?…])\s+|\n+", str(t or "").strip()) if _palabras(o)]


def _tiene_dato(oracion: str) -> bool:
    n = normalizar(oracion)
    return bool(re.search(r"(?<!\w)\d", n)) or bool(set(n.split()) & NUMEROS) or "por ciento" in n


def _cifras(t: str) -> set[str]:
    """Los números que aparecen (2.500, 2500 y 2,5 % se comparan sin puntos de miles)."""
    out = set()
    for m in re.findall(r"(?<![\w.,])\d[\d.,]*", str(t or "")):   # "COVID19" o "G20" no son cifras
        m = m.rstrip(".,")
        limpio = re.sub(r"\.(?=\d{3}(\D|$))", "", m).replace(",", ".")
        if len(limpio.replace(".", "")) >= 2:          # un "3" suelto no es un dato reconocible
            out.add(limpio)
    return out


def _tema(t: str) -> set[str]:
    return {w for w in normalizar(t).split() if w not in VACIAS and len(w) > 2}


def parecidos(a: str, b: str) -> bool:
    x, y = _tema(a), _tema(b)
    if not x or not y:
        return normalizar(a) == normalizar(b)
    return len(x & y) / len(x | y) >= 0.5


def chequear(texto: str, fuentes: list | None = None, datos_usados: list | None = None, c: dict | None = None) -> dict:
    """Las reglas del guion que se pueden medir sin IA. problemas = hay que arreglarlo; avisos = mirarlo."""
    c = c or conf()
    pps = float(c["pps"])
    pal = _palabras(texto)
    n = len(pal)
    seg = round(n / pps, 1)
    problemas, avisos = [], []
    min_p, max_p = math.ceil(c["min_s"] * pps), math.floor(c["max_s"] * pps)
    if not n:
        return {"ok": False, "palabras": 0, "segundos": 0, "cta_segundo": None,
                "problemas": ["El guion está vacío."], "avisos": []}
    if n < min_p:
        problemas.append(f"Dura ~{seg:g} s: le faltan {min_p - n} palabras para llegar a {c['min_s']} s.")
    elif n > max_p:
        problemas.append(f"Dura ~{seg:g} s: le sobran {n - max_p} palabras para no pasar de {c['max_s']} s.")
    ors = _oraciones(texto)
    gancho = len(_palabras(ors[0])) if ors else 0
    max_gancho = math.ceil(c["gancho_s"] * pps)
    if gancho > max_gancho:
        problemas.append(f"El gancho (primera oración) tiene {gancho} palabras: tiene que entrar en {c['gancho_s']} s "
                         f"({max_gancho} palabras como mucho).")
    norm = normalizar(texto)
    if re.match(r"(hola|buenas|buen dia|buenos dias|en el dia de hoy)\b", norm):
        problemas.append("Empieza saludando: arrancá directo con el dato o la pregunta.")
    # el pedido de seguimiento: una vez, en su ventana
    cta = normalizar(c["cta"]).split()
    nw = norm.split()
    pos = [i for i in range(len(nw) - len(cta) + 1) if nw[i:i + len(cta)] == cta] if cta else []
    cta_seg = None
    if not pos:
        problemas.append(f"Falta \"{c['cta']}\" (entre el segundo {c['cta_desde']} y el {c['cta_hasta']}).")
    else:
        if len(pos) > 1:
            problemas.append(f"\"{c['cta']}\" aparece {len(pos)} veces: tiene que ir una sola.")
        cta_seg = round(pos[0] / pps, 1)          # segundo en que se empieza a decir
        if not c["cta_desde"] <= cta_seg <= c["cta_hasta"]:
            problemas.append(f"\"{c['cta']}\" cae en el segundo ~{cta_seg:g}: tiene que ir entre el {c['cta_desde']} "
                             f"y el {c['cta_hasta']}.")
    if ors and not _tiene_dato(ors[-1]):
        problemas.append("El cierre no tiene un dato: terminá con una cifra concreta.")
    # frases repetidas (4 palabras seguidas iguales), sin contar el pedido de seguimiento
    vistas, repetidas = {}, []
    for i in range(len(nw) - 3):
        g = tuple(nw[i:i + 4])
        if cta and " ".join(g) in " ".join(cta) or (cta and set(g) <= set(cta)):
            continue
        if g in vistas and i - vistas[g] >= 4 and " ".join(g) not in repetidas:
            repetidas.append(" ".join(g))
        vistas.setdefault(g, i)
    if repetidas:
        problemas.append("Frases repetidas: " + "; ".join(f"«{r}»" for r in repetidas[:3]) + ".")
    con_bordes = f" {norm} "
    malas = [p for p in c["prohibidas"] if normalizar(p) and f" {normalizar(p)} " in con_bordes]
    if malas:
        problemas.append("Palabras que no van: " + ", ".join(malas[:6]) + ".")
    if re.search(r"[\[\]{}*_#]|https?://", texto or ""):
        problemas.append("La locución tiene acotaciones, links o símbolos: va solo lo que se dice.")
    if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", texto or ""):
        avisos.append("Tiene emojis: la voz no los lee.")
    if not [f for f in (fuentes or []) if str(f).startswith(("http://", "https://"))]:
        problemas.append("No tiene fuentes: cada dato tiene que poder respaldarse.")
    usadas = set()
    for d in datos_usados or []:
        usadas |= _cifras(d)
    repetidos = sorted(_cifras(texto) & usadas)
    if repetidos:
        avisos.append("Repite datos de reels de las últimas 2 semanas: " + ", ".join(repetidos[:5]) + ".")
    return {"ok": not problemas, "palabras": n, "segundos": seg, "cta_segundo": cta_seg, "gancho_palabras": gancho,
            "problemas": problemas, "avisos": avisos}


# --- cuentas del espacio ----------------------------------------------------------------------------------------------------
def _cuenta_noticias(s, cid: str) -> Cuenta:
    c = s.get(Cuenta, cid)
    if not c:
        raise ValueError("Cuenta desconocida.")
    if (c.espacio or "modelos") != "noticias":
        raise ValueError("Esa cuenta no está en el espacio Noticias (cambiala en Cuentas → Editar).")
    return c


def cuentas_noticias() -> list[str]:
    with sesion() as s:
        return list(s.scalars(select(Cuenta.id).where(Cuenta.espacio == "noticias").order_by(Cuenta.id)))


def _desde(dias: int) -> datetime:
    return ahora() - timedelta(days=dias)


def _datos_usados(s, cid: str, salvo_noticia: int | None = None) -> list[str]:
    gs = s.scalars(select(Guion).where(Guion.cuenta_id == cid, Guion.creado >= _desde(DIAS_MEMORIA),
                                       Guion.estado.in_(("jurado", "listo", "aprobado")))).all()
    out = []
    for g in gs:
        if salvo_noticia is not None and g.noticia_id == salvo_noticia:
            continue
        out += [str(d)[:160] for d in (g.datos or [])]
    return out[-60:]


# --- radar --------------------------------------------------------------------------------------------------------------------
def pedir_radar(cid: str, auto: bool = False) -> dict:
    with candado("noticias"):       # dos clics (o el automático y un clic) no lanzan dos radares
        return _pedir_radar(cid, auto)


def _pedir_radar(cid: str, auto: bool) -> dict:
    c = conf()
    with sesion() as s:
        _cuenta_noticias(s, cid)
        abierta = s.scalar(select(TareaIA.id).where(TareaIA.tipo == "radar", TareaIA.referencia == cid,
                                                    TareaIA.estado.in_(("pendiente", "enviada"))).limit(1))
        if abierta:
            raise ValueError("El radar ya está buscando para esta cuenta: esperá que termine.")
        ya = [n.titulo for n in s.scalars(select(Noticia).where(Noticia.cuenta_id == cid,
                                                               Noticia.creado >= _desde(DIAS_MEMORIA))
                                          .order_by(Noticia.creado.desc()).limit(80))]
        usados = _datos_usados(s, cid)
    tid = tareas.crear("radar", {"cuenta": "@" + cid, "ahora": iso_local(ahora()), "areas": c["areas"],
                                 "ya_cubiertos": ya, "datos_usados": usados, "auto": auto}, referencia=cid)
    return {"ok": True, "tarea": tid}


def _url_ok(u) -> bool:
    u = str(u or "").strip()
    return bool(re.fullmatch(r"https?://[^\s<>\"']{4,500}", u))


def _entero(v, a: int = 1, b: int = 10) -> int:
    try:
        return max(a, min(b, int(round(float(v)))))
    except (TypeError, ValueError, OverflowError):
        return a


@tareas.al_terminar("radar")
def _radar(t, r: dict) -> None:
    cid = t.referencia
    c = conf()
    nuevas = []
    with sesion() as s:
        cu = s.get(Cuenta, cid)
        if not cu:
            return
        previas = [n.titulo for n in s.scalars(select(Noticia).where(Noticia.cuenta_id == cid,
                                                                    Noticia.creado >= _desde(DIAS_MEMORIA)))]
        for it in r.get("items") or []:
            titulo = " ".join(str(it.get("titulo") or "").split())[:300]
            if not titulo or any(parecidos(titulo, p) for p in previas):
                continue
            previas.append(titulo)
            area = " ".join(str(it.get("area") or "").split())[:80]
            area = next((a for a in c["areas"] if normalizar(a) == normalizar(area)), area)
            u, p = _entero(it.get("urgencia")), _entero(it.get("potencial"))
            n = Noticia(cuenta_id=cid, area=area, titulo=titulo, resumen=str(it.get("resumen") or "")[:2000],
                        por_que=str(it.get("por_que") or "")[:600],
                        datos=[str(d)[:300] for d in (it.get("datos") or [])][:5],
                        fuentes=[str(f).strip() for f in (it.get("fuentes") or []) if _url_ok(f)][:4],
                        urgencia=u, potencial=p, puntaje=round(0.6 * u + 0.4 * p, 1), tarea_id=t.id)
            s.add(n)
            nuevas.append(n)
        s.flush()
        ids = [n.id for n in sorted(nuevas, key=lambda x: -x.puntaje)]
    aviso(f"Radar de @{cid}: {len(ids)} temas nuevos." if ids else f"Radar de @{cid}: nada nuevo (todo ya cubierto).")
    evento("noticias.radar", cuenta=cid, temas=len(ids))
    if (t.entrada.get("datos") or {}).get("auto") and c["auto_guiones"] and not presupuesto_agotado():
        for nid in ids[:int(c["auto_guiones"])]:
            try:
                pedir_guion(nid)
            except ValueError as ex:
                log.info("auto guion %s: %s", nid, ex)


@tareas.al_fallar("radar")
def _radar_fallo(t, motivo: str) -> None:
    aviso(f"No pude correr el radar de @{t.referencia}: {motivo[:150]}")


def descartar_noticia(nid: int) -> None:
    with sesion() as s:
        n = s.get(Noticia, nid)
        if not n:
            raise ValueError("Ese tema ya no está.")
        n.estado = "descartada"


# --- guion --------------------------------------------------------------------------------------------------------------------
def _params(c: dict) -> dict:
    pps = float(c["pps"])
    return {"pps": pps, "min_s": c["min_s"], "max_s": c["max_s"], "min_p": math.ceil(c["min_s"] * pps),
            "max_p": math.floor(c["max_s"] * pps), "cta": c["cta"], "cta_desde": c["cta_desde"],
            "cta_hasta": c["cta_hasta"], "cta_desde_p": math.ceil(c["cta_desde"] * pps),
            "cta_hasta_p": math.floor(c["cta_hasta"] * pps), "prohibidas": c["prohibidas"]}


def _noticia_dict(n: Noticia) -> dict:
    return {"area": n.area, "titulo": n.titulo, "resumen": n.resumen, "por_que": n.por_que, "datos": n.datos,
            "fuentes": n.fuentes}


def pedir_guion(nid: int) -> dict:
    with candado("noticias"):
        return _pedir_guion(nid)


def _pedir_guion(nid: int) -> dict:
    c = conf()
    with sesion() as s:
        n = s.get(Noticia, nid)
        if not n:
            raise ValueError("Ese tema ya no está.")
        _cuenta_noticias(s, n.cuenta_id)
        abierto = s.scalar(select(Guion.id).where(Guion.noticia_id == nid,
                                                  Guion.estado.in_(("escribiendo", "jurado"))).limit(1))
        if abierto:
            raise ValueError("Ya se está escribiendo un guion para este tema.")
        g = Guion(cuenta_id=n.cuenta_id, noticia_id=nid, titulo=n.titulo[:300], estado="escribiendo")
        s.add(g)
        n.estado = "elegida"
        s.flush()
        gid, cid, nd = g.id, n.cuenta_id, _noticia_dict(n)
        usados = _datos_usados(s, cid, salvo_noticia=nid)
    tareas.crear("guion", {"cuenta": "@" + cid, "noticia": nd, "datos_usados": usados, **_params(c)},
                 referencia=str(gid))
    return obtener(gid)


def reescribir(gid: int) -> dict:
    """Otra versión desde cero, con lo que falló (chequeo + mejoras del jurado) como guía."""
    with candado("noticias"):
        return _reescribir(gid)


def _reescribir(gid: int) -> dict:
    c = conf()
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g:
            raise ValueError("Ese guion ya no está.")
        if g.estado in ("escribiendo", "jurado"):
            raise ValueError("Ese guion se está trabajando: esperá que termine.")
        _cuenta_noticias(s, g.cuenta_id)
        n = s.get(Noticia, g.noticia_id) if g.noticia_id else None
        nd = _noticia_dict(n) if n else {"titulo": g.titulo, "datos": g.datos, "fuentes": g.fuentes}
        anterior = {"texto": g.texto, "problemas": (g.chequeo or {}).get("problemas") or [],
                    "mejoras": (g.jurado or {}).get("mejoras") or []}
        g.estado, g.error, g.version, g.actualizado = "escribiendo", "", g.version + 1, ahora()
        g.jurado, g.nota = None, None
        cid = g.cuenta_id
        usados = _datos_usados(s, cid, salvo_noticia=g.noticia_id)
    tareas.crear("guion", {"cuenta": "@" + cid, "noticia": nd, "anterior": anterior, "datos_usados": usados,
                           **_params(c)}, referencia=str(gid))
    return obtener(gid)


def _hashtags(v) -> list[str]:
    out = []
    for h in v or []:
        h = re.sub(r"[^\w]", "", str(h))
        if h and ("#" + h).lower() not in [o.lower() for o in out]:
            out.append("#" + h[:40])
    return out[:6]


@tareas.al_terminar("guion")
def _guion(t, r: dict) -> None:
    gid = int(t.referencia)
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g or g.estado != "escribiendo":
            return
        g.titulo = " ".join(str(r.get("titulo") or g.titulo).split())[:300]
        g.texto = str(r.get("texto") or "").strip()[:6000]
        g.original = ""
        g.caption = str(r.get("caption") or "").strip()[:2200]
        g.hashtags = _hashtags(r.get("hashtags"))
        g.fuentes = [str(f).strip() for f in (r.get("fuentes") or []) if _url_ok(f)][:8]
        if not g.fuentes and g.noticia_id:
            n = s.get(Noticia, g.noticia_id)
            g.fuentes = list((n.fuentes if n else []) or [])
        g.datos = [str(d)[:300] for d in (r.get("datos") or [])][:10]
        g.chequeo = chequear(g.texto, g.fuentes, _datos_usados(s, g.cuenta_id, salvo_noticia=g.noticia_id))
        g.estado, g.actualizado = "jurado", ahora()
    pedir_jurado(gid)


def pedir_jurado(gid: int) -> None:
    c = conf()
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g:
            raise ValueError("Ese guion ya no está.")
        g.estado, g.actualizado = "jurado", ahora()
        datos = {"texto": g.texto, "segundos": (g.chequeo or {}).get("segundos"), "chequeo": g.chequeo,
                 "personas": prompts.JURADO_NOTICIAS, "pps": c["pps"], "cta": c["cta"]}
    tareas.crear("jurado", datos, referencia=str(gid))


def _fallo_guion(t, motivo: str) -> None:
    try:
        gid = int(t.referencia)
    except ValueError:
        return
    with sesion() as s:
        g = s.get(Guion, gid)
        if g and g.estado in ("escribiendo", "jurado"):
            g.estado, g.error, g.actualizado = "error", motivo[:500], ahora()
            cid = g.cuenta_id
        else:
            return
    aviso(f"No pude terminar el guion de @{cid}: {motivo[:150]}")


tareas.al_fallar("guion")(_fallo_guion)
tareas.al_fallar("jurado")(_fallo_guion)


def _peor(ch: dict | None) -> int:
    return len((ch or {}).get("problemas") or []) if ch is not None else 99


def _coma(x: float) -> str:
    return f"{x:g}".replace(".", ",")


@tareas.al_terminar("jurado")
def _jurado(t, r: dict) -> None:
    gid = int(t.referencia)
    c = conf()
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g or g.estado != "jurado":
            return
        nombres = {p["nombre"] for p in prompts.JURADO_NOTICIAS}
        personas, vistos = [], set()
        dur = float((g.chequeo or {}).get("segundos") or c["max_s"])
        for p in r.get("personas") or []:
            nom = str(p.get("nombre") or "").strip()[:40]
            if not nom or nom in vistos:
                continue
            vistos.add(nom)
            se_iria = p.get("se_iria")
            try:
                se_iria = None if se_iria is None else max(0.0, min(dur, float(se_iria)))
            except (TypeError, ValueError):
                se_iria = None
            personas.append({"nombre": nom, "nota": _entero(p.get("nota")), "se_iria": se_iria,
                             "comentario": str(p.get("comentario") or "")[:400], "del_jurado": nom in nombres})
        personas = personas[:len(prompts.JURADO_NOTICIAS)]
        if not personas:
            g.estado, g.error = "error", "El jurado no devolvió notas."
            return
        prom = round(sum(p["nota"] for p in personas) / len(personas), 1)
        mirado = [p["se_iria"] if p["se_iria"] is not None else dur for p in personas]
        retencion = round(100 * sum(mirado) / (len(mirado) * dur)) if dur else 0
        aplicado = False
        mejorado = str(r.get("texto_mejorado") or "").strip()[:6000]
        usados = _datos_usados(s, g.cuenta_id, salvo_noticia=g.noticia_id)
        if mejorado and normalizar(mejorado) != normalizar(g.texto):
            ch = chequear(mejorado, g.fuentes, usados, c)
            nuevos = _cifras(mejorado) - _cifras(g.texto) - set().union(*[_cifras(d) for d in g.datos or [""]])
            if not nuevos and _peor(ch) <= _peor(g.chequeo):
                g.original, g.texto, g.chequeo, aplicado = g.texto, mejorado, ch, True
        g.jurado = {"personas": personas, "mejoras": [str(m)[:300] for m in (r.get("mejoras") or [])][:3],
                    "resumen": str(r.get("resumen") or "")[:400], "promedio": prom, "retencion": retencion,
                    "aplicado": aplicado}
        g.nota = prom
        if prom < float(c["umbral"]):
            g.estado, g.error = "descartado", f"El jurado le puso {_coma(prom)}: menos de {_coma(c['umbral'])}."
        else:
            g.estado, g.error = "listo", ""
        g.actualizado = ahora()
        cid, estado = g.cuenta_id, g.estado
    if estado == "listo":
        aviso(f"Guion listo para @{cid}: el jurado le puso {_coma(prom)}/10 (retención {retencion} %). Miralo en Noticias.")
    else:
        aviso(f"Guion descartado para @{cid}: el jurado le puso {_coma(prom)}/10. Podés reescribirlo.")
    evento("noticias.guion", cuenta=cid, guion=gid, estado=estado, nota=prom)


def id_valido(v, que: str = "el guion") -> int:
    """Un id que manda el panel o una integración: entero positivo que entra en la base (Postgres: 32 bits)."""
    if isinstance(v, bool):
        raise ValueError(f"Falta {que}.")
    try:
        x = int(str(v).strip()) if isinstance(v, str) else int(v) if isinstance(v, int) else None
    except ValueError:
        x = None
    if x is None or not 0 < x < 2 ** 31:
        raise ValueError(f"Falta {que}.")
    return x


def accion_guion(d: dict) -> dict:
    op = str(d.get("op") or "")
    gid = id_valido(d.get("id"))
    if op == "reescribir":
        return reescribir(gid)
    with candado("noticias"):
        return _accion(gid, op, d)


def _accion(gid: int, op: str, d: dict) -> dict:
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g:
            raise ValueError("Ese guion ya no está.")
        trabajando = g.estado in ("escribiendo", "jurado")
        if op == "editar":
            if trabajando:
                raise ValueError("Ese guion se está trabajando: esperá que termine.")
            texto = str(d.get("texto") or "").strip()
            if not texto:
                raise ValueError("El guion no puede quedar vacío.")
            g.texto = texto[:6000]
            if "caption" in d:
                g.caption = str(d.get("caption") or "").strip()[:2200]
            if "fuentes" in d and isinstance(d["fuentes"], list):
                g.fuentes = [str(f).strip() for f in d["fuentes"] if _url_ok(f)][:8]
            g.chequeo = chequear(g.texto, g.fuentes, _datos_usados(s, g.cuenta_id, salvo_noticia=g.noticia_id))
            if g.estado == "aprobado":
                g.estado = "listo"          # cambió el texto: se vuelve a aprobar
        elif op == "aprobar":
            if g.estado not in ("listo", "descartado"):
                raise ValueError("Solo se aprueba un guion terminado.")
            if not (g.chequeo or {}).get("ok"):
                raise ValueError("Primero arreglá lo que marca el chequeo.")
            g.estado = "aprobado"
        elif op == "descartar":
            if trabajando:
                raise ValueError("Ese guion se está trabajando: esperá que termine.")
            g.estado = "descartado"
        elif op == "jurado":
            if trabajando:
                raise ValueError("Ese guion se está trabajando: esperá que termine.")
            g.estado = "jurado"
        else:
            raise ValueError("op")
        g.actualizado = ahora()
    if op == "jurado":
        pedir_jurado(gid)
    return obtener(gid)


# --- costos -------------------------------------------------------------------------------------------------------------------
def precio(modelo: str, c: dict | None = None) -> dict:
    c = c or conf()
    tabla = {**PRECIOS, **(c.get("precios") or {})}
    m = str(modelo or "")
    for k in sorted(tabla, key=len, reverse=True):     # "claude-opus-5-5-xxx" usa el de "claude-opus-5-5"
        if m == k or m.startswith(k):
            return tabla[k]
    return tabla.get(config().modelo) or PRECIOS["claude-opus-5-5"]


def costo(uso: dict | None, c: dict | None = None) -> float:
    if not uso:
        return 0.0
    c = c or conf()
    p = precio(uso.get("modelo") or "", c)

    def n(k):
        try:
            return max(0.0, float(uso.get(k) or 0))
        except (TypeError, ValueError):
            return 0.0
    usd = (n("entrada") * p["entrada"] + n("cache_escrita") * p["entrada"] * ESCRITURA_CACHE
           + n("cache") * p["cache"] + n("salida") * p["salida"]) / 1e6 + n("busquedas") * float(c["precio_busqueda"])
    return round(usd, 6)


def _inicio_mes() -> datetime:
    t = local(ahora())
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def gasto_mes() -> float:
    c = conf()
    with sesion() as s:
        usos = s.scalars(select(TareaIA.uso).where(TareaIA.creado >= _inicio_mes(), TareaIA.uso.is_not(None))).all()
    return round(sum(costo(u, c) for u in usos), 4)


def presupuesto_agotado() -> bool:
    p = float(conf()["presupuesto_usd"] or 0)
    return bool(p) and gasto_mes() >= p


def _costo_radar_por_tema(s, c: dict, tarea_ids: set[str]) -> dict[str, float]:
    """El radar es uno para varios temas: cada tema carga con su parte."""
    out = {}
    for tid in tarea_ids:
        t = s.get(TareaIA, tid)
        n = s.scalar(select(func.count()).select_from(Noticia).where(Noticia.tarea_id == tid)) or 1
        out[tid] = costo(t.uso, c) / n if t else 0.0
    return out


def costo_guiones(s, gs: list[Guion], c: dict) -> dict[int, dict]:
    if not gs:
        return {}
    refs = [str(g.id) for g in gs]
    ts = s.scalars(select(TareaIA).where(TareaIA.tipo.in_(("guion", "jurado")), TareaIA.referencia.in_(refs))).all()
    nids = [g.noticia_id for g in gs if g.noticia_id]
    ns = {n.id: n for n in s.scalars(select(Noticia).where(Noticia.id.in_(nids)))} if nids else {}
    radar = _costo_radar_por_tema(s, c, {n.tarea_id for n in ns.values() if n.tarea_id})
    out = {g.id: {"radar": 0.0, "guion": 0.0, "jurado": 0.0, "total": 0.0} for g in gs}
    for t in ts:
        x = out.get(int(t.referencia))
        if x is not None:
            x[t.tipo] += costo(t.uso, c)
    for g in gs:
        n = ns.get(g.noticia_id)
        if n and n.tarea_id:
            out[g.id]["radar"] = radar.get(n.tarea_id, 0.0)
        x = out[g.id]
        x["total"] = x["radar"] + x["guion"] + x["jurado"]
        for k in x:
            x[k] = round(x[k], 4)
    return out


ETAPAS = {"radar": "Radar", "guion": "Guion", "jurado": "Jurado", "jueces": "Jueces (fotos y videos)",
          "lectura": "Copiar video: lectura", "juez": "Copiar video: control", "filtrar": "Buscador de memes",
          "respuestas": "Respuestas a comentarios", "tendencias": "Tendencias", "chat": "Chat"}


def costos(dias: int = 30) -> dict:
    """Para la página Costos: por etapa, por día, por reel y el mes contra el presupuesto."""
    dias = max(1, min(366, int(dias)))
    c = conf()
    desde = _desde(dias)
    with sesion() as s:
        ts = s.scalars(select(TareaIA).where(TareaIA.creado >= desde)).all()
        por_etapa: dict[str, dict] = {}
        por_dia: dict[str, float] = {}
        sin_medir = 0
        total = 0.0
        for t in ts:
            if t.estado not in ("hecha", "error"):
                continue
            e = por_etapa.setdefault(t.tipo, {"etapa": t.tipo, "nombre": ETAPAS.get(t.tipo, t.tipo), "pedidos": 0,
                                               "usd": 0.0, "entrada": 0, "salida": 0, "busquedas": 0})
            e["pedidos"] += 1
            if not t.uso:
                sin_medir += 1
                continue
            u = costo(t.uso, c)
            e["usd"] += u
            for k in ("entrada", "salida", "busquedas"):
                try:
                    e[k] += int(t.uso.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            dia = local(t.creado).strftime("%Y-%m-%d")
            por_dia[dia] = por_dia.get(dia, 0.0) + u
            total += u
        gs = s.scalars(select(Guion).where(Guion.creado >= desde).order_by(Guion.creado.desc()).limit(200)).all()
        cg = costo_guiones(s, gs, c)
        reels = [{"id": g.id, "cuenta": g.cuenta_id, "titulo": g.titulo, "estado": g.estado, "nota": g.nota,
                  "creado": iso_local(g.creado), **cg[g.id]} for g in gs]
    terminados = [r for r in reels if r["estado"] in ("listo", "aprobado")]
    mes = gasto_mes()
    pres = float(c["presupuesto_usd"] or 0)
    alerta = ""
    if pres and mes >= pres:
        alerta = f"Se pasó el presupuesto del mes (US$ {mes:.2f} de {pres:.2f}): lo automático está frenado."
    elif pres and mes >= 0.8 * pres:
        alerta = f"Ya se usó el {round(100 * mes / pres)} % del presupuesto del mes."
    etapas = sorted(por_etapa.values(), key=lambda e: -e["usd"])
    for e in etapas:
        e["usd"] = round(e["usd"], 4)
    return {"dias": dias, "total_usd": round(total, 4), "mes_usd": mes, "presupuesto_usd": pres, "alerta": alerta,
            "etapas": etapas, "por_dia": [{"dia": k, "usd": round(v, 4)} for k, v in sorted(por_dia.items())],
            "reels": reels,
            "promedio_reel_usd": round(sum(r["total"] for r in terminados) / len(terminados), 4) if terminados else 0,
            "sin_medir": sin_medir, "config": c,
            "nota": ("Los pedidos hechos con Claude Code no tienen costo por token (van por la suscripción)."
                     if sin_medir else "")}


# --- lo que ve el panel -------------------------------------------------------------------------------------------------------
def _guion_dict(g: Guion, cst: dict | None = None) -> dict:
    return {"id": g.id, "cuenta": g.cuenta_id, "noticia": g.noticia_id, "titulo": g.titulo, "texto": g.texto,
            "original": g.original, "caption": g.caption, "hashtags": g.hashtags or [], "fuentes": g.fuentes or [],
            "datos": g.datos or [], "chequeo": g.chequeo, "jurado": g.jurado, "nota": g.nota, "version": g.version,
            "estado": g.estado, "error": g.error, "creado": iso_local(g.creado), "actualizado": iso_local(g.actualizado),
            "costo": cst or {}}


def _noticia_out(n: Noticia) -> dict:
    return {"id": n.id, "cuenta": n.cuenta_id, "area": n.area, "titulo": n.titulo, "resumen": n.resumen,
            "por_que": n.por_que, "datos": n.datos or [], "fuentes": n.fuentes or [], "urgencia": n.urgencia,
            "potencial": n.potencial, "puntaje": n.puntaje, "estado": n.estado, "creado": iso_local(n.creado)}


def obtener(gid: int) -> dict:
    with sesion() as s:
        g = s.get(Guion, gid)
        if not g:
            raise ValueError("Ese guion ya no está.")
        return _guion_dict(g, costo_guiones(s, [g], conf())[g.id])


def listar(cid: str) -> dict:
    c = conf()
    with sesion() as s:
        _cuenta_noticias(s, cid)
        ns = s.scalars(select(Noticia).where(Noticia.cuenta_id == cid, Noticia.creado >= _desde(3))
                       .order_by(Noticia.puntaje.desc(), Noticia.id.desc()).limit(60)).all()
        gs = s.scalars(select(Guion).where(Guion.cuenta_id == cid, Guion.creado >= _desde(30))
                       .order_by(Guion.id.desc()).limit(60)).all()
        cg = costo_guiones(s, gs, c)
        buscando = bool(s.scalar(select(TareaIA.id).where(TareaIA.tipo == "radar", TareaIA.referencia == cid,
                                                          TareaIA.estado.in_(("pendiente", "enviada"))).limit(1)))
        ultimo = s.scalar(select(func.max(Noticia.creado)).where(Noticia.cuenta_id == cid))
        return {"noticias": [_noticia_out(n) for n in ns], "guiones": [_guion_dict(g, cg[g.id]) for g in gs],
                "buscando": buscando, "ultimo_radar": iso_local(ultimo), "config": c,
                "jurado": prompts.JURADO_NOTICIAS}


NOMBRES_DEFECTO = {"modelos": "Modelos", "noticias": "Noticias"}


def nombres_espacios() -> dict:
    g = ajuste("espacios_nombres") or {}
    return {k: (str(g.get(k) or "").strip() or v) if isinstance(g, dict) else v for k, v in NOMBRES_DEFECTO.items()}


def guardar_nombres(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ValueError("Mandá los nombres.")
    actual = nombres_espacios()
    for k in NOMBRES_DEFECTO:
        if k in d:
            v = " ".join(str(d[k] or "").split())[:30]
            actual[k] = v or NOMBRES_DEFECTO[k]
    if actual["modelos"].lower() == actual["noticias"].lower():
        raise ValueError("Los dos espacios necesitan nombres distintos.")
    guardar_ajuste("espacios_nombres", actual)
    return actual


def guardar_espacio(cid: str, espacio: str) -> None:
    if espacio not in ESPACIOS:
        raise ValueError("El espacio tiene que ser 'modelos' o 'noticias'.")
    with sesion() as s:
        c = s.get(Cuenta, cid)
        if not c:
            raise ValueError("Cuenta desconocida.")
        c.espacio = espacio


# --- automático: un radar por día y por cuenta ----------------------------------------------------------------------------------
def periodicas() -> None:
    t = local(ahora())
    if t.hour < int(conf()["hora_radar"]):
        return
    for cid in cuentas_noticias():
        encolar("radar_diario", {"cuenta": cid}, clave=f"radar:{cid}:{t.strftime('%Y%m%d')}")


@manejador("radar_diario")
def _radar_diario(d: dict) -> None:
    if tareas.estado_puente()["puente"] == "apagado":
        return
    if presupuesto_agotado():
        aviso("Se pasó el presupuesto de IA del mes: hoy no corrí el radar de noticias (cambialo en Costos).")
        return
    try:
        pedir_radar(str(d.get("cuenta") or ""), auto=True)
    except ValueError as ex:
        log.info("radar diario: %s", ex)
