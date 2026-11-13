#!/usr/bin/env python3
"""Hookline Lite: community manager de Instagram multi-cuenta (panel local en http://localhost:8791).

  py panel.py                      -> abre el panel (con consola)
  py panel.py fondo                -> el panel en segundo plano, sin consola (lo usa "Abrir Hookline Lite.bat")
  py panel.py esperar              -> (para Claude) espera un mensaje o pedido nuevo del panel y lo imprime
  py panel.py responder "texto"    -> (para Claude) contesta en el chat del panel
  py panel.py estado               -> (para Claude) resumen corto: cuentas, bandeja, pedidos abiertos

Nada de esto publica sin que la dueña lo apruebe en el panel.
"""
import json
import mimetypes
import os
import subprocess
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import instagram_api as ig
import buscador
import copiar
import material
import medios
import respuestas

RAIZ = Path(__file__).resolve().parent                      # el programa
BASE = Path(os.environ.get("HOOKLINE_DATOS") or RAIZ)       # los datos (se puede apuntar a otra carpeta para pruebas)
DATOS = BASE / "datos"
CUENTAS = BASE / "cuentas"
SUBIR = BASE / "Para subir"
MATERIAL = BASE / "Material"                                # fotos de la modelo, ejemplos y música (no se publica)
MINIS = DATOS / "miniaturas"
JUECES = DATOS / "jueces"
SALIDA = DATOS / "salida"
RESP_DIR = DATOS / "respuestas"
CONFIG = DATOS / "config.json"
CHAT = DATOS / "chat.json"
VISTO = DATOS / ".visto"
PUENTE = DATOS / "puente.json"
PIEZAS = DATOS / "piezas.json"
PUERTO = int(os.environ.get("HOOKLINE_PUERTO") or 8791)
TIPOS = ("historia", "reel", "carrusel", "foto")
ANTICIPO_S = 8 * 60      # la subida + el procesado tardan: se arranca 8 min antes de la hora
TOLERANCIA_S = 15 * 60   # pasada la hora por más de esto sin que el panel estuviera prendido -> "vencida" (no se publica sola)

_lock = threading.RLock()
_publicando = threading.Lock()
_vivo = {"desde": time.time(), "tick": time.time()}
_states = {}
_cache = {}


# --- archivos JSON (escritura atómica) -------------------------------------------------------------
def leer(p, defecto):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return defecto


def escribir(p, datos):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(p).with_suffix(Path(p).suffix + ".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def ahora_iso():
    return datetime.now().isoformat(timespec="minutes")


def config():
    c = leer(CONFIG, {})
    return c if isinstance(c, dict) else {}


def rel(p):
    try:
        return Path(p).resolve().relative_to(BASE.resolve()).as_posix()
    except ValueError:
        return str(p)


def absoluta(r):
    p = (BASE / r).resolve()
    return p if str(p).startswith(str(BASE.resolve())) else None


MAT = material.Material(MATERIAL, leer, escribir)
CARPETAS_REELS = ("modelo", "Fotos", "Videos", "Picantes")             # de acá elige la fábrica para los reels
CARPETAS_HISTORIAS = ("Salidas y comida", "Día a día", "historias")    # y de acá las historias


def carpetas_reels(cuenta):
    """Para los reels: todas las carpetas de la modelo menos las de historias, ejemplos, música y general
    (así las que ella crea, como «Boca», también se usan)."""
    if not cuenta_existe(cuenta):
        return []
    fuera = set(CARPETAS_HISTORIAS) | {"ejemplos", "musica", "general"}
    return [k for k in MAT.secciones(material_de(cuenta)) if k not in fuera]


def material_en(cuenta, carpetas):
    """[(ruta, etiquetas, carpeta, item)] de la modelo de esa cuenta, en esas carpetas."""
    if not cuenta_existe(cuenta):
        return []
    m = material_de(cuenta)
    todo = MAT.listar(m, rel)
    return [(MAT.carpeta(m, k) / x["nombre"], x.get("etiquetas") or [], k, x) for k in carpetas if k in todo for x in todo[k]]


COP = copiar.Copiar(MATERIAL / "_copias", leer, escribir, lambda *a, **k: pedido(*a, **k), rel,
                    lambda cuenta: [(r, et) for r, et, _, _ in material_en(cuenta, carpetas_reels(cuenta))])
material.COOKIES = DATOS / "cookies.txt"
ACTIVIDAD = DATOS / "actividad.json"
_act_lock = threading.Lock()


def actividad(tipo, texto, img="", cuenta="", copia=""):
    """Registro en vivo de lo que hace la fábrica (lo muestra Inicio)."""
    with _act_lock:
        ls = leer(ACTIVIDAD, []) or []
        ls.append({"t": datetime.now().isoformat(timespec="seconds"), "tipo": tipo, "texto": str(texto)[:200],
                   "img": img, "cuenta": cuenta, "copia": copia})
        escribir(ACTIVIDAD, ls[-300:])

COP.log = actividad


def _guardar_musica(video, cuenta, nombre):
    if not cuenta_existe(cuenta):
        return
    d = MAT.carpeta(material_de(cuenta), "musica")
    destino = d / f"{nombre}.m4a"
    if not destino.exists():
        MAT._sacar_audio(Path(video), destino)


COP.guardar_musica = _guardar_musica
COP.musicas = lambda cuenta: [p for p in (MAT.carpeta(material_de(cuenta), "musica").glob("*.*") if cuenta_existe(cuenta) else [])
                              if p.suffix.lower() in material.AUDIOS]
BUS = buscador.Buscador(MATERIAL / "_copias", DATOS / "navegador", BASE / "buscador" / "fuentes.txt", leer, escribir,
                        lambda *a, **k: pedido(*a, **k), COP)


# --- chat con Claude (puente) ----------------------------------------------------------------------
def chat_agregar(de, texto, extra=None):
    """de: vos (la dueña) | pedido (botón del panel) | claude | panel (avisos del sistema)."""
    with _lock:
        b = leer(CHAT, [])
        m = {"id": (b[-1]["id"] + 1) if b else 1, "de": de, "texto": str(texto)[:6000],
             "hora": datetime.now().strftime("%d/%m %H:%M")}
        if extra:
            m.update(extra)
        b.append(m)
        escribir(CHAT, b[-400:])
        return m


def latido(**kw):
    p = leer(PUENTE, {})
    p.update(kw)
    escribir(PUENTE, p)


def cmd_esperar():
    """Bloquea hasta que llegue un mensaje de la dueña o un pedido del panel y lo imprime (para Claude)."""
    visto = int(VISTO.read_text()) if VISTO.exists() else 0
    t = 0
    while True:
        if time.time() - t > 15:
            latido(escuchando=time.time(), pid=os.getpid())
            t = time.time()
        nuevos = [m for m in leer(CHAT, []) if m["de"] in ("vos", "pedido") and m["id"] > visto]
        if nuevos:
            VISTO.parent.mkdir(parents=True, exist_ok=True)
            VISTO.write_text(str(nuevos[-1]["id"]))
            latido(entregado=time.time(), escuchando=0)
            for m in nuevos:
                print(f"[{m['hora']}] {'PEDIDO DEL PANEL: ' if m['de'] == 'pedido' else 'La dueña dice: '}{m['texto']}")
            return
        time.sleep(2)


def estado_claude():
    """¿Está Claude Code instalado? ¿El puente está escuchando?"""
    c = _cache.get("claude")
    if not c or time.time() - c["t"] > 300:
        exe = respuestas.buscar_claude()
        ext = list((Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".vscode" / "extensions").glob("anthropic.claude-code*"))
        c = {"t": time.time(), "instalado": bool(exe or ext), "cli": bool(exe), "extension": bool(ext)}
        _cache["claude"] = c
    p = leer(PUENTE, {})
    ahora = time.time()
    if ahora - (p.get("escuchando") or 0) < 60:
        puente = "escuchando"
    elif ahora - (p.get("entregado") or 0) < 30 * 60:
        puente = "trabajando"
    else:
        puente = "apagado"
    ultimo = max(p.get("escuchando") or 0, p.get("entregado") or 0)
    return {"instalado": c["instalado"], "cli": c["cli"], "extension": c["extension"], "puente": puente,
            "ultimo": datetime.fromtimestamp(ultimo).isoformat(timespec="minutes") if ultimo else ""}


def pedido(texto, extra=None):
    """Pedido automático del panel para Claude (lo lee 'panel.py esperar')."""
    return chat_agregar("pedido", texto, extra)


# --- cuentas ----------------------------------------------------------------------------------------
def cloudflared():
    return ig.buscar_cloudflared(RAIZ, config().get("cloudflared"))


def cuenta_ig(cid):
    return ig.Cuenta(CUENTAS / cid, leer, escribir, config().get("version") or ig.VERSION, cloudflared)


def cuentas():
    out = []
    if not CUENTAS.exists():
        return out
    for d in sorted(CUENTAS.iterdir()):
        info = leer(d / "cuenta.json", None)
        if not d.is_dir() or not isinstance(info, dict):
            continue
        e = cuenta_ig(d.name).estado()
        tend = leer(d / "tendencias.json", {})
        out.append({**info, "id": d.name, "usuario": e.get("usuario") or info.get("usuario") or d.name,
                    "nombre": e.get("nombre") or info.get("nombre") or "", "foto": e.get("foto") or "",
                    "conectada": e["conectado"], "vencida": e["vencido"], "dias_token": e["dias_token"],
                    "tipo_cuenta": e.get("tipo_cuenta", ""), "seguidores": e.get("seguidores"),
                    "publicaciones": e.get("publicaciones"), "ultima": e.get("ultima", ""), "error": e.get("error", ""),
                    "aviso": e.get("aviso", ""), "puede_publicar": e["puede_publicar"],
                    "puede_comentar": e["puede_comentar"], "puede_insights": e["puede_insights"],
                    "limite": e.get("limite") or {}, "cuenta_28d": e.get("cuenta_28d") or {},
                    "tendencias_fecha": tend.get("fecha", ""), "perfil": f"https://www.instagram.com/{e.get('usuario') or d.name}/"})
    return out


def material_de(cid):
    """Las multicuentas usan el Material (fotos y videos) de su cuenta main: la main es la modelo."""
    info = leer(CUENTAS / str(cid) / "cuenta.json", None) if cid and re.fullmatch(r"[\w.]+", str(cid)) else None
    return (info or {}).get("main") or cid


def cuenta_existe(cid):
    return bool(cid) and re.fullmatch(r"[\w.]+", cid) and (CUENTAS / cid / "cuenta.json").exists()


def crear_cuenta(token):
    """Después del login: lee el perfil con el token nuevo y crea/actualiza cuentas/<usuario>/."""
    u = ig.perfil_con_token(token["access_token"], config().get("version") or ig.VERSION)
    usuario = re.sub(r"[^\w.]", "", u.get("username") or "") or f"cuenta{token.get('user_id', '')}"
    d = CUENTAS / usuario
    info = leer(d / "cuenta.json", {}) or {}
    info.update(usuario=usuario, nombre=u.get("name") or info.get("nombre", ""), creada=info.get("creada") or ahora_iso())
    info.setdefault("nicho", "")
    info.setdefault("tono", "")
    escribir(d / "cuenta.json", info)
    token["ig_id"] = str(u.get("user_id") or token.get("user_id") or "")
    escribir(d / "token.json", token)
    (SUBIR / usuario).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=lambda: cuenta_ig(usuario).sincronizar(), daemon=True).start()
    return usuario, u.get("account_type", "")


def redirect():
    return str(config().get("redirect") or f"https://localhost:{PUERTO}/instagram/callback")


def url_conectar():
    c = config()
    if not str(c.get("app_id") or "").strip() or not str(c.get("app_secret") or "").strip():
        raise RuntimeError("Primero cargá el App ID y el App Secret de tu app de Meta en Ajustes.")
    state = secrets.token_urlsafe(16)
    for k in [k for k, t in _states.items() if time.time() - t > 3600]:
        _states.pop(k, None)
    _states[state] = time.time()
    return ig.url_login(c["app_id"], redirect(), state)


def conectar_con_codigo(code, state):
    if state and state not in _states:
        raise RuntimeError("El inicio de sesión expiró o el panel se reinició: tocá Conectar cuenta de nuevo.")
    _states.pop(state, None)
    c = config()
    tok = ig.canjear_codigo(c.get("app_id"), c.get("app_secret"), redirect(), code)
    return crear_cuenta(tok)


# --- bandeja: piezas (lo que la dueña sube) ---------------------------------------------------------
def piezas():
    p = leer(PIEZAS, [])
    return p if isinstance(p, list) else []


def guardar_piezas(lista):
    escribir(PIEZAS, lista)


def buscar_pieza(lista, pid):
    return next((x for x in lista if x.get("id") == pid), None)


def nuevo_id():
    time.sleep(0.002)
    return f"p{int(time.time() * 1000)}"


def tipo_por_archivo(p):
    return "reel" if medios.es_video(p) else "foto"


def escanear():
    """Archivos nuevos en 'Para subir/<cuenta>/' -> una pieza cada uno. Archivos borrados -> se sacan de su pieza."""
    with _lock:
        lista, cambio = piezas(), False
        usados = {a for x in lista for a in x.get("archivos", [])}
        for c in cuentas():
            d = SUBIR / c["id"]
            d.mkdir(parents=True, exist_ok=True)
            nuevos = sorted((f for f in d.iterdir() if f.is_file() and (medios.es_foto(f) or medios.es_video(f))),
                            key=lambda f: f.stat().st_mtime)
            for f in nuevos:
                r = rel(f)
                if r in usados:
                    continue
                lista.append({"id": nuevo_id(), "cuenta": c["id"], "tipo": tipo_por_archivo(f), "archivos": [r],
                              "estado": "nueva", "creado": ahora_iso(), "caption": "", "cuando": "", "jueces": None})
                usados.add(r)
                cambio = True
        for x in list(lista):
            if x.get("estado") in ("publicada", "subiendo"):
                continue
            vivos = [a for a in x.get("archivos", []) if (BASE / a).exists()]
            if vivos != x.get("archivos"):
                x["archivos"] = vivos
                cambio = True
                if not vivos:
                    lista.remove(x)
        if cambio:
            guardar_piezas(lista)


def validar_tipo(tipo, archivos):
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


def accion_pieza(d):
    op, pid = d.get("op"), str(d.get("id", ""))
    with _lock:
        lista = piezas()
        x = buscar_pieza(lista, pid)
        if op == "juntar":  # varias piezas -> un carrusel (en el orden elegido)
            ids = [str(i) for i in d.get("ids") or []]
            sel = [buscar_pieza(lista, i) for i in ids]
            if len(sel) < 2 or any(s is None for s in sel):
                raise ValueError("Elegí al menos 2 fotos de la bandeja.")
            if len({s["cuenta"] for s in sel}) != 1:
                raise ValueError("Todas tienen que ser de la misma cuenta.")
            if any(s["estado"] in ("programada", "subiendo", "publicada") for s in sel):
                raise ValueError("Hay alguna ya programada o publicada: cancelala primero.")
            archivos = [a for s in sel for a in s["archivos"]]
            if len(archivos) > 10:
                raise ValueError("Un carrusel lleva hasta 10 fotos.")
            base = sel[0]
            base.update(tipo="carrusel", archivos=archivos, estado="nueva", jueces=None)
            for s in sel[1:]:
                lista.remove(s)
            guardar_piezas(lista)
            return base
        if not x:
            raise ValueError("Esa pieza ya no está en la bandeja.")
        bloqueada = x["estado"] in ("subiendo", "publicada")
        if op == "tipo":
            t = d.get("tipo")
            if t not in TIPOS:
                raise ValueError("Tipo desconocido.")
            if bloqueada or x["estado"] == "programada":
                raise ValueError("Ya está programada: cancelala para cambiar el tipo.")
            err = validar_tipo(t, x["archivos"])
            if err:
                raise ValueError(err)
            x["tipo"] = t
        elif op == "orden":
            nuevo = [str(a) for a in d.get("archivos") or []]
            if sorted(nuevo) != sorted(x["archivos"]):
                raise ValueError("El orden no coincide con los archivos.")
            if bloqueada:
                raise ValueError("Ya se publicó.")
            x["archivos"] = nuevo
        elif op == "separar":  # sacar un archivo del carrusel -> vuelve a la bandeja solo
            a = str(d.get("archivo", ""))
            if a not in x["archivos"] or bloqueada:
                raise ValueError("No se puede separar.")
            x["archivos"].remove(a)
            lista.append({"id": nuevo_id(), "cuenta": x["cuenta"], "tipo": tipo_por_archivo(a), "archivos": [a],
                          "estado": "nueva", "creado": ahora_iso(), "caption": "", "cuando": "", "jueces": None})
            if len(x["archivos"]) == 1:
                x["tipo"] = tipo_por_archivo(x["archivos"][0])
        elif op == "caption":
            x["caption"] = str(d.get("caption") or "")[:2200]
        elif op == "programar":
            if bloqueada:
                raise ValueError("Ya se está subiendo o ya salió.")
            if not cuenta_ig(x["cuenta"]).estado()["conectado"]:
                raise ValueError("Esta cuenta todavía no está conectada a Instagram: conectala en Cuentas para programar.")
            caption =str(d.get("caption") if d.get("caption") is not None else x.get("caption") or "").strip()
            if len(caption) > 2200:
                raise ValueError("El texto pasa los 2.200 caracteres que permite Instagram.")
            if len(re.findall(r"#\w+", caption)) > 30:
                raise ValueError("Instagram permite hasta 30 hashtags (lo ideal: 5 a 8).")
            err = validar_tipo(x["tipo"], x["archivos"])
            if err:
                raise ValueError(err)
            c = next((c for c in cuentas() if c["id"] == x["cuenta"]), None)
            if not c or not c["conectada"]:
                raise ValueError("Esa cuenta no está conectada a Instagram: andá a Cuentas → Conectar.")
            if not c["puede_publicar"]:
                raise ValueError("Falta el permiso de publicar: reconectá la cuenta y aceptalo.")
            ya = not d.get("cuando")
            if ya:
                cuando = ahora_iso()
            else:
                try:
                    t = datetime.fromisoformat(str(d["cuando"])[:16]).timestamp()
                except ValueError:
                    raise ValueError("Hora inválida.")
                if t < time.time() - 60:
                    raise ValueError("Esa hora ya pasó: elegí una hora futura o tocá Publicar ahora.")
                cuando = datetime.fromtimestamp(t).isoformat(timespec="minutes")
            x.update(caption=caption, cuando=cuando, ahora=ya, estado="programada", error="", etapa="",
                     aprobada=ahora_iso())
        elif op == "cancelar":
            if x["estado"] == "subiendo":
                raise ValueError("Ya se está subiendo: no se puede cancelar.")
            if x["estado"] == "publicada":
                raise ValueError("Ya se publicó.")
            x.update(estado="juzgada" if x.get("jueces") else "nueva", ahora=False, error="", etapa="")
        elif op == "borrar":
            if x["estado"] in ("subiendo",):
                raise ValueError("Se está subiendo: esperá a que termine.")
            if x["estado"] != "publicada":  # los archivos van a una carpeta aparte (no se borra nada de verdad)
                destino = SUBIR / x["cuenta"] / "_descartados"
                destino.mkdir(parents=True, exist_ok=True)
                for a in x["archivos"]:
                    p = BASE / a
                    if p.exists():
                        shutil.move(str(p), str(_libre(destino / p.name)))
            lista.remove(x)
            guardar_piezas(lista)
            return {"ok": True}
        else:
            raise ValueError("Acción desconocida.")
        guardar_piezas(lista)
    if op == "programar":
        threading.Thread(target=revisar_cola, daemon=True).start()
    return x


def _libre(p):
    p = Path(p)
    if not p.exists():
        return p
    for i in range(2, 999):
        q = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists():
            return q
    return p.with_name(f"{p.stem}_{int(time.time())}{p.suffix}")


def guardar_subida(cuenta, nombre, datos):
    if not cuenta_existe(cuenta):
        raise ValueError("Elegí una cuenta primero.")
    limpio = re.sub(r"[^\w.\- ]+", "_", Path(nombre).name)[-90:] or "archivo"
    if not (medios.es_foto(limpio) or medios.es_video(limpio)):
        raise ValueError("Solo fotos (jpg, png, webp, heic) o videos (mp4, mov).")
    destino = _libre(SUBIR / cuenta / limpio)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(datos)
    escanear()
    return destino.name


# --- jueces (pedido a Claude) -----------------------------------------------------------------------
def mejores_horarios(cid):
    """De las publicaciones con vistas: promedio por día de semana + hora. Devuelve lo mejor primero."""
    ms = (leer(CUENTAS / cid / "medios.json", {}) or {}).get("medios") or []
    grupos, por_hora = {}, {}
    for m in ms:
        if not m.get("publicado") or m.get("vistas") is None:
            continue
        try:
            t = datetime.fromisoformat(m["publicado"])
        except ValueError:
            continue
        grupos.setdefault((t.weekday(), t.hour), []).append(m["vistas"])
        por_hora.setdefault(t.hour, []).append(m["vistas"])
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    mejores = sorted(({"dia": dias[k[0]], "dia_n": k[0], "hora": f"{k[1]:02d}:00", "promedio": round(sum(v) / len(v)),
                       "n": len(v)} for k, v in grupos.items()), key=lambda x: -x["promedio"])
    horas = [{"hora": f"{h:02d}:00", "promedio": round(sum(v) / len(v)), "n": len(v)} for h, v in sorted(por_hora.items())]
    return {"mejores": mejores[:5], "por_hora": horas, "con_datos": sum(len(v) for v in grupos.values())}


def pedir_jueces(pid):
    with _lock:
        lista = piezas()
        x = buscar_pieza(lista, pid)
        if not x:
            raise ValueError("Esa pieza ya no está.")
        if x["estado"] in ("subiendo", "publicada"):
            raise ValueError("Ya se publicó.")
        err = validar_tipo(x["tipo"], x["archivos"])
        if err:
            raise ValueError(err)
        x.update(estado="jueces", jueces=None, pedido_jueces=ahora_iso())
        guardar_piezas(lista)
    threading.Thread(target=_preparar_jueces, args=(x,), daemon=True).start()
    return x


def _preparar_jueces(x):
    carpeta = JUECES / x["id"]
    try:
        if carpeta.exists():
            shutil.rmtree(carpeta, ignore_errors=True)
        carpeta.mkdir(parents=True, exist_ok=True)
        partes = []
        for i, a in enumerate(x["archivos"]):
            cuadros, dur = medios.cuadros_jueces(BASE / a, carpeta / f"parte{i + 1}")
            partes.append({"archivo": str(BASE / a), "es_video": medios.es_video(a), "duracion_s": dur,
                           "cuadros": [str(c) for c in cuadros]})
        info = leer(CUENTAS / x["cuenta"] / "cuenta.json", {}) or {}
        est = cuenta_ig(x["cuenta"]).estado()
        pedido_json = {"pieza": x["id"], "cuenta": "@" + x["cuenta"], "nicho": info.get("nicho") or "sin definir",
                       "tono": info.get("tono") or "", "tipo": x["tipo"], "seguidores": est.get("seguidores"),
                       "texto_actual": x.get("caption") or "", "partes": partes, "ahora": ahora_iso(),
                       "mejores_horarios": mejores_horarios(x["cuenta"]),
                       "ya_programado": [{"cuando": p["cuando"], "tipo": p["tipo"]} for p in piezas()
                                         if p["cuenta"] == x["cuenta"] and p["estado"] == "programada"],
                       "escribir_en": str(carpeta / "veredicto.json")}
        if not any(p["cuadros"] for p in partes):
            raise RuntimeError("No pude sacar cuadros del video (falta imageio-ffmpeg: corré Instalar.bat).")
        escribir(carpeta / "pedido.json", pedido_json)
        pedido(f"jueces · pieza {x['id']} ({x['tipo']}) de @{x['cuenta']}. Lanzá UN agente jueces-cm con el pedido "
               f"{carpeta / 'pedido.json'} (escribe {carpeta / 'veredicto.json'}). No contestes nada largo en el chat.",
               {"tipo_pedido": "jueces", "pieza": x["id"]})
    except Exception as ex:
        with _lock:
            lista = piezas()
            y = buscar_pieza(lista, x["id"])
            if y:
                y.update(estado="nueva", error=f"Jueces: {str(ex)[:200]}")
                guardar_piezas(lista)


def recoger_veredictos():
    """Si el agente ya escribió datos/jueces/<id>/veredicto.json, se pasa a la pieza."""
    with _lock:
        lista, cambio = piezas(), False
        for x in lista:
            if x.get("estado") != "jueces":
                continue
            v = leer(JUECES / x["id"] / "veredicto.json", None)
            if not isinstance(v, dict) or not v.get("jueces"):
                continue
            jueces = [j for j in v.get("jueces") or [] if isinstance(j, dict)][:3]
            notas = [float(j.get("nota") or 0) for j in jueces]
            v["promedio"] = round(sum(notas) / len(notas), 1) if notas else None
            tags = [t if str(t).startswith("#") else "#" + str(t) for t in (v.get("hashtags") or [])][:8]
            v["hashtags"] = tags
            x["jueces"] = v
            if not (x.get("caption") or "").strip():
                x["caption"] = ((v.get("caption") or "").strip() + ("\n\n" + " ".join(tags) if tags else "")).strip()
            hor = (v.get("horario") or {}).get("cuando") if isinstance(v.get("horario"), dict) else None
            x["sugerido"] = str(hor or "")[:16]
            x["estado"] = "juzgada"
            x["error"] = ""
            cambio = True
            chat_agregar("panel", f"Los jueces terminaron con la pieza de @{x['cuenta']}: {v['promedio']}/10 · "
                                  f"{'aprobar' if v.get('veredicto') == 'aprobar' else 'cambiar'}. Miralo en la Bandeja.")
        if cambio:
            guardar_piezas(lista)


# --- cola: publicar a la hora ------------------------------------------------------------------------
def cambiar(pid, **kw):
    with _lock:
        lista = piezas()
        x = buscar_pieza(lista, pid)
        if x:
            x.update(kw)
            guardar_piezas(lista)
        return x


def _publicar(x):
    if not _publicando.acquire(blocking=False):
        return
    try:
        cambiar(x["id"], estado="subiendo", etapa="arrancando", inicio=ahora_iso())
        salida = SALIDA / x["id"]
        archivos = []
        prop = medios.proporcion_carrusel([BASE / a for a in x["archivos"]]) if x["tipo"] == "carrusel" else None
        for i, a in enumerate(x["archivos"]):
            p = BASE / a
            if medios.es_foto(p):
                archivos.append(medios.jpeg_para_instagram(p, salida / f"{i + 1}.jpg", x["tipo"], prop))
            else:
                archivos.append(p)
        t = datetime.fromisoformat(x["cuando"]).timestamp()
        r = cuenta_ig(x["cuenta"]).publicar(x["tipo"], archivos, x.get("caption", ""),
                                            avisar=lambda e: cambiar(x["id"], etapa=e),
                                            publicar_en=None if x.get("ahora") else t)
        # los originales pasan a 'Para subir/<cuenta>/_publicados/' para que la bandeja quede limpia
        nuevos = []
        destino = SUBIR / x["cuenta"] / "_publicados"
        destino.mkdir(parents=True, exist_ok=True)
        for a in x["archivos"]:
            p = BASE / a
            try:
                q = _libre(destino / p.name)
                shutil.move(str(p), str(q))
                nuevos.append(rel(q))
            except Exception:
                nuevos.append(a)
        shutil.rmtree(salida, ignore_errors=True)
        cambiar(x["id"], estado="publicada", etapa="", media_id=r["id"], permalink=r.get("permalink", ""),
                publicado=r.get("publicado", ""), subida=r.get("subida"), archivos=nuevos)
        chat_agregar("panel", f"Publicado en @{x['cuenta']} ({x['tipo']}). {r.get('permalink', '')}")
        threading.Timer(120, lambda: cuenta_ig(x["cuenta"]).sincronizar()).start()
    except Exception as ex:
        cambiar(x["id"], estado="error", etapa="", error=str(ex)[:300])
        chat_agregar("panel", f"No se pudo publicar en @{x['cuenta']} ({x['tipo']}): {str(ex)[:200]}")
    finally:
        _publicando.release()


# --- repartir / encargo: el cronograma del día para cada modelo y sus multicuentas ---------------------------
HORAS_REELS = ["12:30", "14:00", "16:00", "18:30", "20:00", "21:30", "23:00", "00:30"]   # Argentina, buenos horarios
HORAS_HISTORIAS = ["09:30", "11:00", "13:30", "15:00", "17:00", "19:30", "21:00", "22:30", "01:00"]
ENCARGO = DATOS / "encargo.json"


def _es_noche(hora):
    h = int(hora[:2])
    return h >= 19 or h < 6


def _horarios(base, n, i, dia):
    """n horarios distintos para la cuenta número i (cada cuenta corrida un poco: no todas a la misma hora).
    Si es para hoy, solo los que todavía no pasaron."""
    ahora = datetime.now()
    out = []
    paso = max(1, len(base) // max(1, n))   # repartidos en el día (mañana, tarde, noche), no todos juntos
    orden = [(i + k * paso) % len(base) for k in range(len(base))]
    orden += [x for x in range(len(base)) if x not in orden]
    for k in orden:
        hh, mm = map(int, base[k].split(":"))
        mm += (i * 17) % 50
        hh, mm = (hh + mm // 60) % 24, mm % 60
        cuando = datetime.fromisoformat(f"{dia}T{hh:02d}:{mm:02d}")
        if hh < 6:
            cuando += timedelta(days=1)   # 00:30 / 01:00 = esa misma noche
        if cuando < ahora + timedelta(minutes=15) or any(abs((cuando - o).total_seconds()) < 3600 for o in out):
            continue
        out.append(cuando)
        if len(out) >= n:
            break
    return sorted(out)


CALENTAMIENTO = [(7, 1, 1), (14, 2, 2)]   # (hasta el día N de vida, reels, historias); después, lo pedido


def calentamiento(c, dia, reels, historias):
    """Una subcuenta nueva no publica a full de entrada: semana 1 → 1+1, semana 2 → 2+2, después lo pedido.
    La edad se cuenta desde «activa_desde» (o desde que se creó en el panel)."""
    desde = str(c.get("activa_desde") or c.get("creada") or "")[:10]
    try:
        edad = (datetime.fromisoformat(dia) - datetime.fromisoformat(desde)).days
    except ValueError:
        return reels, historias
    for hasta, r, h in CALENTAMIENTO:
        if edad < hasta:
            return min(reels, r), min(historias, h)
    return reels, historias


def _libres(candidatos, ocupadas, n):
    """Los primeros n horarios que no choquen (a menos de 1 h) con lo que esa cuenta ya tiene ese día."""
    ya = [datetime.fromisoformat(o) for o in ocupadas if o]
    out = []
    for h in candidatos:
        if all(abs((h - o).total_seconds()) >= 3600 for o in ya + out):
            out.append(h)
        if len(out) >= n:
            break
    return sorted(out)


def repartir(dia, reels_por_cuenta=3, historias_por_cuenta=2):
    """Completa el cronograma de `dia`: reels aprobados DISTINTOS a cada multicuenta (nunca el mismo en dos
    cuentas) e historias del día a día a todas (main y multicuentas), sin repetir fotos y con coherencia:
    fotos de noche a la noche, de día de día. Sin conectar -> 'planificada'; conectada -> 'programada'."""
    datetime.fromisoformat(dia)
    cs = sorted(cuentas(), key=lambda c: (c.get("main") or c["id"], bool(c.get("main")), c["id"]))
    hechos = {"reels": 0, "historias": 0, "faltan": []}
    with _lock:
        lista = piezas()
        ya_repartidas = {x.get("copia") for x in lista if x.get("copia")}
        fotos_usadas = {x.get("foto") for x in lista if x.get("foto")}
        def del_dia(cid, tipo):
            return [x for x in lista if x["cuenta"] == cid and x.get("plan") and x.get("dia") == dia and x["tipo"] == tipo]
        for i, c in enumerate(cs):
            m = c.get("main") or c["id"]
            estado = "programada" if c["conectada"] else "planificada"
            # reels: solo las multicuentas (la main es la influencer: día a día)
            cr, ch = calentamiento(c, dia, reels_por_cuenta, historias_por_cuenta)
            faltan_r = (cr - len(del_dia(c["id"], "reel"))) if c.get("main") else 0
            if faltan_r > 0:
                pool = sorted([x for x in COP.listar() if x.get("resultado") and (x.get("juez") or {}).get("estado") == "aprobado"
                               and material_de(x.get("cuenta")) == m and x["id"] not in ya_repartidas],
                              key=lambda x: x.get("armado") or "")
                ocupadas = {x["cuando"] for x in lista if x["cuenta"] == c["id"]}
                for cuando in _libres(_horarios(HORAS_REELS, reels_por_cuenta, i, dia) + _horarios(HORAS_REELS, len(HORAS_REELS), i, dia), ocupadas, faltan_r):
                    if not pool:
                        hechos["faltan"].append(f"@{c['usuario']}: faltan reels aprobados de @{m}")
                        break
                    x = pool.pop(0)
                    ya_repartidas.add(x["id"])
                    dst = _libre(SUBIR / c["id"] / f"reel-{x['id']}.mp4")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(COP.resultado(x["id"]), dst)
                    hora = cuando.strftime("%H:%M")
                    actividad("programando", f"Reel para @{c['usuario']} a las {hora}", img=rel(dst), cuenta=c["id"], copia=x["id"])
                    lista.append({"id": nuevo_id(), "cuenta": c["id"], "tipo": "reel", "archivos": [rel(dst)], "estado": estado,
                                  "creado": ahora_iso(), "caption": "", "cuando": cuando.isoformat(timespec="minutes"),
                                  "jueces": None, "plan": True, "dia": dia, "copia": x["id"]})
                    hechos["reels"] += 1
            # historias: fotos del día a día de su modelo, nunca la misma en dos cuentas, de noche a la noche
            # la main es la cuenta oficial: ese contenido lo sube ella; la fábrica solo trabaja en las subcuentas
            faltan_h = (ch - len(del_dia(c["id"], "historia"))) if c.get("main") else 0
            if faltan_h > 0:
                todas = [{**x, "_sec": k} for _, et, k, x in material_en(m, CARPETAS_HISTORIAS + ("Fotos",))
                         if medios.es_foto(x["nombre"]) and f"{m}/{x['nombre']}" not in fotos_usadas
                         and (k != "Fotos" or "noche" in et)]   # de Fotos solo las de noche (para las historias nocturnas)
                ocupadas = {x["cuando"] for x in lista if x["cuenta"] == c["id"]}
                for cuando in _libres(_horarios(HORAS_HISTORIAS, historias_por_cuenta, i, dia) + _horarios(HORAS_HISTORIAS, len(HORAS_HISTORIAS), i, dia), ocupadas, faltan_h):
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
                    sec = f["_sec"]
                    dst = _libre(SUBIR / c["id"] / f"hist-{f['nombre']}")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(MAT.carpeta(m, sec) / f["nombre"], dst)
                    actividad("programando", f"Historia para @{c['usuario']} a las {hora}", img=rel(dst), cuenta=c["id"])
                    lista.append({"id": nuevo_id(), "cuenta": c["id"], "tipo": "historia", "archivos": [rel(dst)], "estado": estado,
                                  "creado": ahora_iso(), "caption": "", "cuando": cuando.isoformat(timespec="minutes"),
                                  "jueces": None, "plan": True, "dia": dia, "foto": f"{m}/{f['nombre']}"})
                    hechos["historias"] += 1
        guardar_piezas(lista)
    return hechos


def encargo(dia, reels_por_cuenta, historias_por_cuenta):
    """'Mandar a trabajar': reparte lo que ya hay y, si faltan reels aprobados, le pide a la fábrica que haga más
    con los memes guardados. Mientras esté 'trabajando', el panel sigue completando solo (bucle_rapido)."""
    reels_por_cuenta, historias_por_cuenta = max(0, min(6, int(reels_por_cuenta))), max(0, min(6, int(historias_por_cuenta)))
    cs = cuentas()
    e = {"dia": dia, "reels": reels_por_cuenta, "historias": historias_por_cuenta, "estado": "trabajando",
         "creado": ahora_iso(), "pedidos": {}}
    escribir(ENCARGO, e)
    actividad("encargo", f"Encargo para el {dia}: {reels_por_cuenta} reels por multicuenta y {historias_por_cuenta} historias por cuenta")
    hechos = repartir(dia, reels_por_cuenta, historias_por_cuenta)
    # ¿cuántos reels faltan por modelo? -> la fábrica los hace con memes guardados
    for m in [c for c in cs if not c.get("main")]:
        hijas = [c for c in cs if c.get("main") == m["id"]]
        if not hijas:
            continue
        puestos = sum(1 for x in piezas() if x.get("dia") == dia and x["tipo"] == "reel" and x["cuenta"] in {h["id"] for h in hijas})
        en_camino = sum(1 for x in COP.listar() if material_de(x.get("cuenta")) == m["id"] and x.get("estado") in ("bajando", "leyendo", "armando"))
        falta = len(hijas) * reels_por_cuenta - puestos - en_camino
        if falta > 0:
            try:
                n = len(COP.hacer_guardados(falta + 1, m["id"]))   # uno de más por si el juez tacha alguno
                e["pedidos"][m["id"]] = n
                actividad("pedido", f"Pidiendo {n} reels nuevos de @{m['usuario']} a la fábrica")
            except ValueError as ex:
                actividad("aviso", f"@{m['usuario']}: {ex}")
    escribir(ENCARGO, e)
    return hechos


def seguir_encargo():
    """Cada pocos segundos: si hay un encargo trabajando, va repartiendo los reels que el juez aprueba."""
    e = leer(ENCARGO, {}) or {}
    if e.get("estado") != "trabajando":
        return
    h = repartir(e["dia"], e["reels"], e["historias"])
    cs = cuentas()
    falta = 0
    for c in cs:
        ps = [x for x in piezas() if x["cuenta"] == c["id"] and x.get("dia") == e["dia"]]
        cr, ch = calentamiento(c, e["dia"], e["reels"], e["historias"])
        falta += max(0, (cr if c.get("main") else 0) - sum(1 for x in ps if x["tipo"] == "reel"))
        falta += max(0, (ch if c.get("main") else 0) - sum(1 for x in ps if x["tipo"] == "historia"))
    ocupadas = any(x.get("estado") in ("bajando", "leyendo", "armando") or (x.get("juez") or {}).get("estado") == "mirando"
                   for x in COP.listar())
    if not falta:
        e["estado"] = "listo"
        actividad("listo_encargo", f"Listo el cronograma del {e['dia']}: todo programado")
        escribir(ENCARGO, e)
    elif not ocupadas and not h["reels"] and not h["historias"] and time.time() - datetime.fromisoformat(e["creado"]).timestamp() > 600:
        e["estado"] = "incompleto"
        actividad("aviso", f"El encargo quedó incompleto: faltan {falta} piezas (faltan memes o fotos del día a día)")
        escribir(ENCARGO, e)


def revisar_cola():
    """Publica lo que llega a su hora. Si la compu/panel estuvo apagado y se pasó la hora: 'vencida' + aviso."""
    ahora = time.time()
    if ahora - _vivo["tick"] > 180:  # hubo un hueco (compu suspendida): lo de antes no se publica solo
        _vivo["desde"] = ahora
    _vivo["tick"] = ahora
    vencidas, listo = [], None
    with _lock:
        lista = piezas()
        for x in sorted(lista, key=lambda p: p.get("cuando") or ""):
            if x.get("estado") != "programada":
                continue
            try:
                t = datetime.fromisoformat(x["cuando"]).timestamp()
            except Exception:
                continue
            if not x.get("ahora") and ahora > t + TOLERANCIA_S and t - ANTICIPO_S < _vivo["desde"]:
                x.update(estado="vencida", etapa="")
                vencidas.append(x)
            elif (x.get("ahora") or ahora >= t - ANTICIPO_S) and listo is None:
                listo = x
        if vencidas:
            guardar_piezas(lista)
    for x in vencidas:
        chat_agregar("panel", f"NO se publicó la pieza de @{x['cuenta']} ({x['tipo']}, era para el "
                              f"{x['cuando'].replace('T', ' ')}) porque la compu o el panel estaban apagados. "
                              "Reprogramala o tocá Publicar ahora.")
    if listo and not _publicando.locked():
        threading.Thread(target=_publicar, args=(listo,), daemon=True).start()


# --- comentarios -------------------------------------------------------------------------------------
def comentarios_de(cid):
    d = leer(CUENTAS / cid / "comentarios.json", {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("items", {})
    return d


def actualizar_comentarios(cid):
    cu = cuenta_ig(cid)
    ms = (leer(CUENTAS / cid / "medios.json", {}) or {}).get("medios") or []
    d = comentarios_de(cid)
    nuevos = 0
    for m in [m for m in ms if m.get("comentarios")][:10]:
        for c in cu.comentarios(m["id"]):
            viejo = d["items"].get(c["id"])
            if viejo:
                viejo.update(likes=c["likes"], respuestas=c["respuestas"])
                if c["respondido_por_mi"] and viejo.get("estado") == "nuevo":
                    viejo["estado"] = "respondido"
                continue
            d["items"][c["id"]] = {**c, "post": m.get("url", ""), "post_texto": (m.get("caption") or "")[:120],
                                   "miniatura": m.get("miniatura", ""),
                                   "estado": "respondido" if c["respondido_por_mi"] else "nuevo"}
            nuevos += 1
    d["fecha"] = ahora_iso()
    escribir(CUENTAS / cid / "comentarios.json", d)
    return nuevos


def aplicar_sugerencias(cid, res):
    with _lock:
        d = comentarios_de(cid)
        usadas = [c.get("respuesta_enviada") or "" for c in d["items"].values() if c.get("respuesta_enviada")]
        for k, r in res.items():
            c = d["items"].get(k)
            if not c:
                continue
            alerta, resp = respuestas.revisar(c, r, usadas)
            c.update(clase=r["clase"], sugerencia=resp, alerta=alerta)
            usadas.append(resp)
        d["sugiriendo"] = ""
        escribir(CUENTAS / cid / "comentarios.json", d)


def sugerir_respuestas(cid):
    d = comentarios_de(cid)
    pend = [c for c in d["items"].values() if c.get("estado") == "nuevo" and not c.get("sugerencia")][:30]
    if not pend:
        return {"ok": True, "via": "nada", "n": 0}
    info = leer(CUENTAS / cid / "cuenta.json", {}) or {}
    d["sugiriendo"] = ahora_iso()
    escribir(CUENTAS / cid / "comentarios.json", d)
    if respuestas.buscar_claude():
        def correr():
            try:
                aplicar_sugerencias(cid, respuestas.sugerir_haiku("@" + cid, info.get("nicho"), info.get("tono"), pend,
                                                                  DATOS / "claude_tmp"))
            except Exception as ex:
                chat_agregar("panel", f"No pude sugerir respuestas directo ({str(ex)[:120]}); se lo pido a Claude.")
                _pedir_respuestas_chat(cid, info, pend)
        threading.Thread(target=correr, daemon=True).start()
        return {"ok": True, "via": "haiku", "n": len(pend)}
    _pedir_respuestas_chat(cid, info, pend)
    return {"ok": True, "via": "chat", "n": len(pend)}


def _pedir_respuestas_chat(cid, info, pend):
    p = RESP_DIR / f"{cid}_pedido.json"
    escribir(p, {"cuenta": "@" + cid, "prompt": respuestas.armar_prompt("@" + cid, info.get("nicho"), info.get("tono"), pend),
                 "escribir_en": str(RESP_DIR / f"{cid}.json")})
    pedido(f"respuestas · @{cid}: leé {p} y escribí el JSON que pide en {RESP_DIR / (cid + '.json')} "
           "(hacelo vos directo, sin agente: es corto).", {"tipo_pedido": "respuestas", "cuenta": cid})


def recoger_respuestas():
    if not RESP_DIR.exists():
        return
    for f in RESP_DIR.glob("*.json"):
        if f.name.endswith("_pedido.json"):
            continue
        cid = f.stem
        try:
            res = respuestas.leer_items(f.read_text(encoding="utf-8"))
        except Exception:
            res = {}
        if res and cuenta_existe(cid):
            aplicar_sugerencias(cid, res)
        try:
            f.unlink()
            (RESP_DIR / f"{cid}_pedido.json").unlink(missing_ok=True)
        except Exception:
            pass


def accion_comentario(d):
    cid, k, op = str(d.get("cuenta", "")), str(d.get("comentario", "")), d.get("op")
    if not cuenta_existe(cid):
        raise ValueError("Cuenta desconocida.")
    if op == "actualizar":
        return {"ok": True, "nuevos": actualizar_comentarios(cid)}
    if op == "sugerir":
        return sugerir_respuestas(cid)
    with _lock:
        c = comentarios_de(cid)["items"].get(k)
    if not c:
        raise ValueError("Comentario desconocido.")
    if op == "responder":
        texto = str(d.get("texto") or "").strip()
        cuenta_ig(cid).responder(k, texto)
        cambios = {"estado": "respondido", "respuesta_enviada": texto, "respondido": ahora_iso()}
    elif op == "ocultar":
        cuenta_ig(cid).ocultar(k)
        cambios = {"estado": "oculto"}
    elif op == "saltar":
        cambios = {"estado": "saltado"}
    else:
        raise ValueError("Acción desconocida.")
    with _lock:
        dd = comentarios_de(cid)
        dd["items"][k].update(cambios)
        escribir(CUENTAS / cid / "comentarios.json", dd)
    return {"ok": True}


# --- tendencias ---------------------------------------------------------------------------------------
def pedir_tendencias(cid, auto=False):
    info = leer(CUENTAS / cid / "cuenta.json", {}) or {}
    if not info.get("nicho"):
        raise ValueError("Primero escribí el nicho de la cuenta (Cuentas → Editar).")
    info["tendencias_pedido"] = ahora_iso()
    escribir(CUENTAS / cid / "cuenta.json", info)
    destino = CUENTAS / cid / "tendencias.json"
    pedido(f"tendencias{' (semanal automático)' if auto else ''} · @{cid} · nicho: {info['nicho']}. Lanzá UN agente "
           f"tendencias-cm para esta cuenta; escribe {destino}.", {"tipo_pedido": "tendencias", "cuenta": cid})
    return {"ok": True}


def tendencias_semanales():
    if estado_claude()["puente"] != "escuchando":
        return
    for c in cuentas():
        info = leer(CUENTAS / c["id"] / "cuenta.json", {}) or {}
        if not info.get("nicho"):
            continue
        ult = max(c.get("tendencias_fecha") or "", info.get("tendencias_pedido") or "")
        if not ult or datetime.now() - datetime.fromisoformat(ult[:16]) > timedelta(days=7):
            pedir_tendencias(c["id"], auto=True)
            return  # de a una por vuelta (ahorra uso)


# --- bucles -------------------------------------------------------------------------------------------
def bucle_rapido():
    while True:
        for f in (escanear, recoger_veredictos, recoger_respuestas, COP.recoger, BUS.recoger, seguir_encargo):
            try:
                f()
            except Exception as ex:
                print("bucle:", f.__name__, ex, file=sys.stderr)
        time.sleep(4)


def bucle_cola():
    with _lock:  # al arrancar: lo que quedó "subiendo" se cortó (se cerró el panel)
        lista, cambio = piezas(), False
        for x in lista:
            if x.get("estado") == "subiendo":
                x.update(estado="programada", etapa="", ahora=False)
                cambio = True
        if cambio:
            guardar_piezas(lista)
    while True:
        try:
            revisar_cola()
        except Exception as ex:
            print("cola:", ex, file=sys.stderr)
        time.sleep(30)


def bucle_sincronizar():
    time.sleep(15)
    while True:
        for c in cuentas():
            if c["conectada"]:
                try:
                    cuenta_ig(c["id"]).sincronizar()
                except Exception:
                    pass
        try:
            tendencias_semanales()
        except Exception:
            pass
        time.sleep(3600)


# --- estado para la página ---------------------------------------------------------------------------
def estado_completo():
    cs = cuentas()
    c = config()
    lista = piezas()
    for x in lista:
        x["vistas_previas"] = [f"/mini?p={urllib.parse.quote(a)}" for a in x.get("archivos", [])]
    return {"cuentas": cs, "piezas": lista, "chat": leer(CHAT, [])[-80:], "claude": estado_claude(),
            "app": {"configurada": bool(str(c.get("app_id") or "").strip() and str(c.get("app_secret") or "").strip()),
                    "cloudflared": bool(cloudflared()), "ffmpeg": bool(medios.ffmpeg())},
            "ahora": ahora_iso(), "carpeta": str(SUBIR)}


def ajustes_get():
    c = config()
    return {"app_id": str(c.get("app_id") or ""), "tiene_secret": bool(str(c.get("app_secret") or "").strip()),
            "redirect": redirect(), "redirect_defecto": f"https://localhost:{PUERTO}/instagram/callback",
            "cloudflared": cloudflared() or "", "ffmpeg": bool(medios.ffmpeg()), "carpeta": str(BASE),
            "version": c.get("version") or ig.VERSION}


def ajustes_set(d):
    with _lock:
        c = config()
        if "app_id" in d:
            v = re.sub(r"\D", "", str(d["app_id"] or ""))
            c["app_id"] = v
        if d.get("app_secret"):
            c["app_secret"] = str(d["app_secret"]).strip()
        if "redirect" in d:
            r = str(d["redirect"] or "").strip()
            if r and not r.startswith("https://"):
                raise ValueError("La dirección de vuelta tiene que empezar con https://")
            c["redirect"] = r
        if "cloudflared" in d:
            c["cloudflared"] = str(d["cloudflared"] or "").strip()
        escribir(CONFIG, c)
    return ajustes_get()


def stats(cid):
    cu = cuenta_ig(cid)
    ms = (leer(CUENTAS / cid / "medios.json", {}) or {}).get("medios") or []
    return {"estado": cu.estado(), "historial": cu.historial()[-24 * 60:], "medios": ms[:60],
            "horarios": mejores_horarios(cid)}


def medios_calendario(cid):
    out = []
    for c in cuentas():
        if cid not in ("", "todas", c["id"]):
            continue
        for m in ((leer(CUENTAS / c["id"] / "medios.json", {}) or {}).get("medios") or [])[:80]:
            out.append({"cuenta": c["id"], "id": m["id"], "publicado": m.get("publicado", ""), "tipo": m.get("tipo", ""),
                        "url": m.get("url", ""), "miniatura": m.get("miniatura", ""), "vistas": m.get("vistas"),
                        "caption": (m.get("caption") or "")[:100]})
    return out


# --- servidor ------------------------------------------------------------------------------------------
def html_simple(titulo, texto, ok=True):
    color = "#e8467c" if ok else "#c0392b"
    return (f"<!doctype html><meta charset=utf-8><title>Hookline Lite</title><body style='font-family:Inter,system-ui,"
            f"sans-serif;background:#fff6f9;color:#2b1a22;display:grid;place-items:center;min-height:90vh'>"
            f"<div style='max-width:480px;background:#fff;border:1px solid #f6d3e0;border-radius:18px;padding:32px'>"
            f"<h2 style='margin:0 0 8px;color:{color}'>{_esc(titulo)}</h2><p>{_esc(texto)}</p>"
            f"<a href='/#cuentas' style='color:#e8467c;font-weight:600'>Volver al panel</a></div>").encode("utf-8")


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Panel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def responder(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(datos)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def json(self):
        try:
            return json.loads(self.cuerpo() or b"{}")
        except ValueError:
            return {}

    def archivo(self, p, tipo=None):
        tipo = tipo or mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        tam = p.stat().st_size
        rango = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        ini, fin = (int(rango.group(1)), int(rango.group(2) or tam - 1)) if rango else (0, tam - 1)
        fin = min(fin, tam - 1)
        self.send_response(206 if rango else 200)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - ini + 1))
        self.send_header("Cache-Control", "max-age=60")
        if rango:
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(ini)
                restan = fin - ini + 1
                while restan > 0:
                    trozo = f.read(min(1 << 20, restan))
                    if not trozo:
                        break
                    self.wfile.write(trozo)
                    restan -= len(trozo)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        ruta, _, consulta = self.path.partition("?")
        q = urllib.parse.parse_qs(consulta)
        uno = lambda k: (q.get(k) or [""])[0]
        if ruta in ("/", "/index.html"):
            return self.responder(200, (RAIZ / "panel" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if ruta == "/api/estado":
            return self.responder(200, estado_completo())
        if ruta == "/api/ajustes":
            return self.responder(200, ajustes_get())
        if ruta == "/api/stats":
            if not cuenta_existe(uno("cuenta")):
                return self.responder(404, {"error": "cuenta"})
            return self.responder(200, stats(uno("cuenta")))
        if ruta == "/api/calendario":
            return self.responder(200, {"medios": medios_calendario(uno("cuenta"))})
        if ruta == "/api/comentarios":
            if not cuenta_existe(uno("cuenta")):
                return self.responder(404, {"error": "cuenta"})
            return self.responder(200, comentarios_de(uno("cuenta")))
        if ruta == "/api/tendencias":
            if not cuenta_existe(uno("cuenta")):
                return self.responder(404, {"error": "cuenta"})
            return self.responder(200, leer(CUENTAS / uno("cuenta") / "tendencias.json", {"items": []}))
        if ruta == "/api/actividad":
            return self.responder(200, {"items": list(reversed(leer(ACTIVIDAD, []) or []))[:60], "encargo": leer(DATOS / "encargo.json", {}) or {}})
        if ruta == "/api/copias":
            return self.responder(200, {"items": COP.listar(), "opciones": COP.opciones(), "links": COP.links(), "busqueda": BUS.estado()})
        if ruta == "/api/modelos":   # las main (cada una es una modelo) con el resumen de su Material
            out = []
            for c in cuentas():
                if c.get("main"):
                    continue
                r = MAT.resumen(c["id"])
                out.append({"id": c["id"], "usuario": c["usuario"], "videos": r["videos"], "fotos": r["fotos"],
                            "carpetas": r["carpetas"], "multicuentas": sum(1 for h in cuentas() if h.get("main") == c["id"]),
                            "ultima": datetime.fromtimestamp(r["ultima"]).isoformat(timespec="minutes") if r["ultima"] else "",
                            "foto": rel(r["foto"]) if r["foto"] else ""})
            return self.responder(200, {"modelos": out})
        if ruta == "/api/material":
            if not cuenta_existe(uno("cuenta")):
                return self.responder(404, {"error": "cuenta"})
            return self.responder(200, MAT.listar(material_de(uno("cuenta")), rel))
        if ruta in ("/mini", "/media"):
            p = absoluta(uno("p"))
            if not p or not str(p).startswith((str(SUBIR.resolve()), str(MATERIAL.resolve()))) or not p.is_file():
                return self.responder(404, {"error": "no existe"})
            if ruta == "/media":
                return self.archivo(p)
            m = medios.miniatura(p, MINIS)
            if not m:
                return self.responder(404, {"error": "sin miniatura"})
            return self.archivo(m, "image/jpeg")
        if ruta == "/jueces-cuadro":
            p = absoluta(uno("p"))
            if not p or not str(p).startswith(str(JUECES.resolve())) or not p.is_file():
                return self.responder(404, {"error": "no existe"})
            return self.archivo(p, "image/jpeg")
        if ruta == "/instagram/conectar":
            try:
                destino = url_conectar()
            except Exception as ex:
                return self.responder(400, html_simple("Falta un paso", ex, False), "text/html; charset=utf-8")
            self.send_response(302)
            self.send_header("Location", destino)
            self.end_headers()
            return
        if ruta == "/instagram/callback":
            if uno("error"):
                return self.responder(200, html_simple("No se conectó", "Cancelaste el permiso en Instagram.", False),
                                      "text/html; charset=utf-8")
            try:
                usuario, tipo = conectar_con_codigo(uno("code"), uno("state"))
                return self.responder(200, html_simple(f"@{usuario} conectada",
                                                       "Listo. Ya podés cerrar esta pestaña y volver al panel."),
                                      "text/html; charset=utf-8")
            except Exception as ex:
                return self.responder(200, html_simple("No se pudo conectar", ex, False), "text/html; charset=utf-8")
        if ruta == "/favicon.ico":
            return self.responder(204, b"")
        self.responder(404, {"error": "no encontrado"})

    def do_POST(self):
        ruta, _, consulta = self.path.partition("?")
        q = urllib.parse.parse_qs(consulta)
        try:
            if ruta == "/api/subir":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1024 * 1024 * 1024:
                    return self.responder(400, {"error": "El archivo pesa más de 1 GB."})
                nombre = guardar_subida((q.get("cuenta") or [""])[0], (q.get("nombre") or ["archivo"])[0], self.cuerpo())
                return self.responder(200, {"ok": True, "archivo": nombre})
            if ruta == "/api/material_subir":
                if int(self.headers.get("Content-Length") or 0) > 1024 * 1024 * 1024:
                    return self.responder(400, {"error": "El archivo pesa más de 1 GB."})
                uno = lambda k: (q.get(k) or [""])[0]
                if not cuenta_existe(uno("cuenta")):
                    return self.responder(400, {"error": "Elegí una cuenta primero."})
                nombre = MAT.guardar(material_de(uno("cuenta")), uno("sec"), uno("nombre") or "archivo", self.cuerpo())
                return self.responder(200, {"ok": True, "archivo": nombre})
            if ruta == "/api/copiar_subir":  # ?que=ejemplo (nueva copia) | modelo&id=...
                if int(self.headers.get("Content-Length") or 0) > 1024 * 1024 * 1024:
                    return self.responder(400, {"error": "El archivo pesa más de 1 GB."})
                que, nombre = (q.get("que") or [""])[0], (q.get("nombre") or ["archivo"])[0]
                if que == "ejemplo":
                    return self.responder(200, {"ok": True, "id": COP.desde_archivo(nombre, self.cuerpo(), (q.get("cuenta") or [""])[0])})
                if que == "audio":
                    return self.responder(200, {"ok": True, "archivo": COP.subir_audio((q.get("id") or [""])[0], nombre, self.cuerpo())})
                return self.responder(200, {"ok": True, "archivo": COP.subir_modelo((q.get("id") or [""])[0], nombre, self.cuerpo())})
            d = self.json()
            if ruta == "/api/copiar":
                op, cid = d.get("op"), str(d.get("id", ""))
                if op == "link":
                    ids = COP.desde_links(d.get("link"), str(d.get("cuenta") or ""))
                    return self.responder(200, {"ok": True, "id": ids[0], "ids": ids})
                if op == "cancelar":
                    return self.responder(200, {"ok": True, "n": COP.cancelar()})
                if op == "guardar_links":
                    return self.responder(200, {"ok": True, "n": COP.guardar_links(d.get("link"))})
                if op == "buscar":
                    BUS.buscar()
                    return self.responder(200, {"ok": True})
                if op == "abrir_navegador":
                    BUS.abrir(["https://www.instagram.com/", "https://www.tiktok.com/"], visible=True)
                    return self.responder(200, {"ok": True})
                if op == "agregar_encontrados":   # lo usa Claude para cargar ejemplos con miniatura ya bajada
                    items = [{**x, "img": absoluta(x.get("img", ""))} for x in d.get("items") or []]
                    items = [x for x in items if x["img"] and str(x["img"]).startswith(str(MATERIAL.resolve())) and x.get("link")]
                    return self.responder(200, {"ok": True, "n": COP.agregar_encontrados(items)})
                if op == "votar":
                    COP.votar(str(d.get("link") or ""), d.get("voto"), d.get("motivo"))
                    return self.responder(200, {"ok": True})
                if op == "previa":
                    try:
                        return self.responder(200, {"ok": True, "url": rel(COP.previa(str(d.get("link") or "")))})
                    except ValueError:   # Instagram pide sesión: tomo la del buscador y pruebo otra vez
                        BUS.exportar_cookies(material.COOKIES)
                        return self.responder(200, {"ok": True, "url": rel(COP.previa(str(d.get("link") or "")))})
                if op == "borrar_link":
                    COP.borrar_link(str(d.get("link") or ""))
                    return self.responder(200, {"ok": True})
                if op == "hacer_guardados":
                    ids = COP.hacer_guardados(d.get("n"), str(d.get("cuenta") or ""))
                    return self.responder(200, {"ok": True, "id": ids[0], "ids": ids})
                if op == "armar":
                    COP.armar(cid, d)
                    return self.responder(200, {"ok": True})
                if op == "borrar":
                    COP.borrar(cid)
                    return self.responder(200, {"ok": True})
                if op == "modelo_material":
                    if not cuenta_existe(str(d.get("cuenta", ""))):
                        raise ValueError("Elegí una cuenta primero.")
                    src = MAT.ruta(material_de(str(d["cuenta"])), str(d.get("sec", "")), str(d.get("nombre", "")))
                    return self.responder(200, {"ok": True, "archivo": COP.modelo_de(cid, src)})
                if op == "audio_material":
                    if not cuenta_existe(str(d.get("cuenta", ""))):
                        raise ValueError("Elegí una cuenta primero.")
                    sec = "musica" if d.get("sec") != "ejemplos" else "ejemplos"
                    src = MAT.ruta(material_de(str(d["cuenta"])), sec, str(d.get("nombre", "")))
                    return self.responder(200, {"ok": True, "archivo": COP.audio_de(cid, src, d.get("titulo") or "")})
                if op == "ubicar":
                    return self.responder(200, COP.ubicar(cid, d))
                if op == "borrar_version":
                    COP.borrar_version(cid, str(d.get("archivo", "")))
                    return self.responder(200, {"ok": True})
                if op == "a_bandeja":
                    cuenta = str(d.get("cuenta", ""))
                    if not cuenta_existe(cuenta):
                        raise ValueError("Elegí una cuenta primero.")
                    destino = _libre(SUBIR / cuenta / f"copia-{cid}.mp4")
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(COP.resultado(cid, str(d.get("archivo") or "")), destino)
                    escanear()
                    return self.responder(200, {"ok": True})
                if op == "carpeta":   # abre el Explorador con el video ya marcado (está en la compu: no hace falta descargarlo)
                    dst = COP.a_resultados(cid) or COP.resultado(cid)
                    subprocess.Popen(["explorer", "/select,", str(Path(dst).resolve())])
                    return self.responder(200, {"ok": True})
                return self.responder(400, {"error": "op"})
            if ruta == "/api/material":
                cid, op = str(d.get("cuenta", "")), d.get("op")
                if not cuenta_existe(cid):
                    return self.responder(400, {"error": "Elegí una cuenta primero."})
                if op == "nueva_carpeta":
                    return self.responder(200, {"ok": True, "nombre": MAT.nueva_carpeta(material_de(cid), d.get("nombre"))})
                if op == "bajar":
                    return self.responder(200, {"ok": True, "id": MAT.bajar(material_de(cid), d.get("link"))})
                if op == "etiquetar":
                    et = MAT.etiquetar(material_de(cid), str(d.get("sec", "")), str(d.get("nombre", "")), d.get("etiquetas"))
                    return self.responder(200, {"ok": True, "etiquetas": et})
                if op == "descartar":
                    MAT.descartar(str(d.get("id", "")))
                    return self.responder(200, {"ok": True})
                if op == "borrar":
                    MAT.borrar(material_de(cid), str(d.get("sec", "")), str(d.get("nombre", "")))
                    return self.responder(200, {"ok": True})
                if op == "a_bandeja":  # copia una foto/video del material a la bandeja de la cuenta
                    src = MAT.ruta(material_de(cid), str(d.get("sec", "")), str(d.get("nombre", "")))
                    if not (medios.es_foto(src) or medios.es_video(src)):
                        raise ValueError("A la bandeja van solo fotos o videos.")
                    destino = _libre(SUBIR / cid / src.name)
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, destino)
                    escanear()
                    return self.responder(200, {"ok": True})
                if op == "carpeta":
                    c = MAT.carpeta(material_de(cid), str(d.get("sec") or "modelo"))
                    c.mkdir(parents=True, exist_ok=True)
                    os.startfile(c)
                    return self.responder(200, {"ok": True})
                return self.responder(400, {"error": "op"})
            if ruta == "/api/chat":
                texto = str(d.get("texto") or "").strip()
                if not texto:
                    return self.responder(400, {"error": "Escribí algo."})
                return self.responder(200, chat_agregar("vos", texto))
            if ruta == "/api/repartir":
                return self.responder(200, {"ok": True, **repartir(str(d.get("dia") or ""))})
            if ruta == "/api/encargo":
                return self.responder(200, {"ok": True, **encargo(str(d.get("dia") or ""), d.get("reels", 3), d.get("historias", 2))})
            if ruta == "/api/pieza":
                if d.get("op") == "jueces":
                    return self.responder(200, pedir_jueces(str(d.get("id", ""))))
                return self.responder(200, accion_pieza(d))
            if ruta == "/api/comentario":
                return self.responder(200, accion_comentario(d))
            if ruta == "/api/tendencias":
                cid = str(d.get("cuenta", ""))
                if not cuenta_existe(cid):
                    return self.responder(404, {"error": "Cuenta desconocida."})
                return self.responder(200, pedir_tendencias(cid))
            if ruta == "/api/ajustes":
                return self.responder(200, ajustes_set(d))
            if ruta == "/api/conectar":  # pegar la dirección a la que volvió Instagram
                u = urllib.parse.urlparse(str(d.get("url") or "").strip())
                qq = urllib.parse.parse_qs(u.query)
                code = (qq.get("code") or [""])[0]
                if not code:
                    return self.responder(400, {"error": "Esa dirección no trae el código: copiala entera desde la barra de arriba."})
                usuario, tipo = conectar_con_codigo(code, (qq.get("state") or [""])[0])
                return self.responder(200, {"ok": True, "usuario": usuario, "tipo": tipo})
            if ruta == "/api/conectar_url":
                return self.responder(200, {"url": url_conectar()})
            if ruta == "/api/cuenta":
                cid, op = str(d.get("id", "")), d.get("op")
                if op == "crear_local":  # cuenta sin conectar todavía (para ir cargando contenido); se conecta después
                    cid = re.sub(r"[^\w.]", "", cid.strip().lstrip("@"))[:30]
                    if not cid:
                        raise ValueError("Escribí el usuario de Instagram de la cuenta.")
                    if cuenta_existe(cid):
                        raise ValueError("Esa cuenta ya existe.")
                    main = re.sub(r"[^\w.]", "", str(d.get("main") or "").lstrip("@"))
                    if main and (not cuenta_existe(main) or material_de(main) != main):
                        raise ValueError("La cuenta main elegida no existe (o es una multicuenta).")
                    with _lock:
                        escribir(CUENTAS / cid / "cuenta.json", {"usuario": cid, "nombre": "", "creada": ahora_iso(),
                                                                 "nicho": "", "tono": "", **({"main": main} if main else {})})
                    (SUBIR / cid).mkdir(parents=True, exist_ok=True)
                    return self.responder(200, {"ok": True, "id": cid})
                if not cuenta_existe(cid):
                    return self.responder(404, {"error": "Cuenta desconocida."})
                if op == "guardar":
                    with _lock:
                        info = leer(CUENTAS / cid / "cuenta.json", {}) or {}
                        for k in ("nicho", "tono", "apodo"):
                            if k in d:
                                info[k] = str(d[k] or "").strip()[:300]
                        if "activa_desde" in d:   # desde cuándo existe la cuenta en Instagram (para el calentamiento)
                            v = str(d["activa_desde"] or "")[:10]
                            if v:
                                datetime.fromisoformat(v)
                            info["activa_desde"] = v
                        escribir(CUENTAS / cid / "cuenta.json", info)
                    return self.responder(200, {"ok": True})
                if op == "sincronizar":
                    return self.responder(200, cuenta_ig(cid).sincronizar())
                if op == "desconectar":
                    (CUENTAS / cid / "token.json").unlink(missing_ok=True)
                    return self.responder(200, {"ok": True})
                if op == "carpeta":
                    (SUBIR / cid).mkdir(parents=True, exist_ok=True)
                    os.startfile(SUBIR / cid)
                    return self.responder(200, {"ok": True})
                return self.responder(400, {"error": "op"})
            return self.responder(404, {"error": "ruta"})
        except ig.TokenVencido:
            return self.responder(400, {"error": "El permiso de Instagram venció: reconectá la cuenta."})
        except (ValueError, RuntimeError) as ex:
            return self.responder(400, {"error": str(ex)[:300]})
        except Exception as ex:
            print("POST", ruta, repr(ex), file=sys.stderr)
            return self.responder(500, {"error": f"Error inesperado: {str(ex)[:200]}"})


class Servidor(ThreadingHTTPServer):
    """En Windows, SO_REUSEADDR deja abrir DOS paneles en el mismo puerto (y publicarían dos veces).
    Con SO_EXCLUSIVEADDRUSE el segundo falla al arrancar y se cierra solo."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def servir(abrir=True):
    for d in (DATOS, CUENTAS, SUBIR):
        d.mkdir(parents=True, exist_ok=True)
    srv = Servidor(("127.0.0.1", PUERTO), Panel)  # si el puerto está ocupado, falla acá (ya hay un panel)
    BUS.al_arrancar()
    for f in (bucle_rapido, bucle_cola, bucle_sincronizar):
        threading.Thread(target=f, daemon=True).start()
    url = f"http://localhost:{PUERTO}"
    print(f"Hookline Lite en {url}  (Ctrl+C para cerrar)")
    if abrir:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


def cmd_estado():
    cs = cuentas()
    ps = piezas()
    print(f"Cuentas: {', '.join('@' + c['id'] + ('' if c['conectada'] else ' (sin conectar)') for c in cs) or 'ninguna'}")
    for e in ("nueva", "jueces", "juzgada", "programada", "vencida", "error"):
        n = [p for p in ps if p.get("estado") == e]
        if n:
            print(f"  {e}: {len(n)}")
    print(f"Puente: {estado_claude()['puente']}")


def main():
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    a = sys.argv[1:]
    if not a:
        return servir()
    if a[0] == "fondo":  # sin consola ni navegador (lo lanza "Abrir Hookline Lite.bat")
        DATOS.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(DATOS / "panel.log", "a", encoding="utf-8", buffering=1)
        try:
            return servir(abrir=False)
        except OSError:
            return  # ya hay un panel andando en el puerto
    if a[0] == "esperar":
        return cmd_esperar()
    if a[0] == "responder" and len(a) > 1:
        chat_agregar("claude", " ".join(a[1:]))
        return print("ok")
    if a[0] == "estado":
        return cmd_estado()
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
