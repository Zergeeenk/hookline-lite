"""Conexión con Instagram para Hookline Lite (API oficial "Instagram API with Instagram Login", graph.instagram.com).

Varias cuentas profesionales (Creador o Empresa) con UNA sola app de Meta. Cada cuenta guarda lo suyo en
cuentas/<usuario>/ (token.json, estado.json, medios.json, historial.json). Nada sale de la compu salvo hacia Meta.

La app de Meta (App ID / App Secret) se carga en Ajustes del panel y queda en datos/config.json (local, no se comparte).
Nunca se imprimen ni se loguean claves ni tokens.

Publica: Reel (video), Foto (IMAGE), Carrusel (CAROUSEL, 2-10) e Historia (STORIES, foto o video).
Documentación (v26.0):
  OAuth:      https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
  Publicar:   https://developers.facebook.com/docs/instagram-platform/content-publishing
  Insights:   https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights

URL PÚBLICA: Instagram baja las fotos (y los videos de carrusel/historia si no anda la subida directa) desde una
dirección pública. Como no hay servidor, el panel abre un mini servidor SOLO con esos archivos (ruta con token al azar)
y lo expone unos minutos con un túnel rápido de Cloudflare (cloudflared.exe: gratis, sin cuenta; Instalar.bat lo baja).
Para Reels se prueba primero la subida directa ("resumable") y, si Meta la rechaza, el túnel.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "v26.0"
GRAPH = "https://graph.instagram.com"
AUTORIZAR = "https://www.instagram.com/oauth/authorize"
TOKEN_CORTO = "https://api.instagram.com/oauth/access_token"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
SCOPES = ("instagram_business_basic,instagram_business_content_publish,"
          "instagram_business_manage_comments,instagram_business_manage_insights")
PUBLICAR = "instagram_business_content_publish"
LIMITE_24H = 100
METRICAS_REEL = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]
METRICAS_POST = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]
INSIGHTS_MAX = 40
ESPERA_PROCESO_S = 20 * 60


class TokenVencido(RuntimeError):
    pass


class ErrorInstagram(RuntimeError):
    def __init__(self, msg, codigo=None, sub=None):
        super().__init__(msg)
        self.codigo, self.sub = codigo, sub


def _error_http(ex):
    try:
        d = json.loads(ex.read())
    except Exception:
        d = {}
    err = d.get("error") if isinstance(d.get("error"), dict) else {}
    msg = err.get("error_user_msg") or err.get("message") or d.get("error_message") or d.get("error_description") or ""
    if not msg and isinstance(d.get("error"), str):
        msg = d["error"]
    codigo = err.get("code") or d.get("code")
    if codigo == 190 or d.get("error_type") == "OAuthException" and "expired" in str(msg).lower():
        return TokenVencido(str(msg)[:200] or "token vencido")
    # solo el código y el mensaje de Meta: nunca el cuerpo completo ni la URL (podrían traer el token)
    return ErrorInstagram(f"Instagram respondió {ex.code}{f' ({codigo})' if codigo else ''}: {str(msg)[:240]}".strip(),
                          codigo, err.get("error_subcode"))


def pedir(metodo, url, params=None, datos=None, timeout=60, headers=None):
    params = params or {}
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    if metodo == "GET":
        req = urllib.request.Request(url + q, headers={"Accept": "application/json", **(headers or {})})
    else:
        req = urllib.request.Request(url + q, data=urllib.parse.urlencode(datos or {}).encode(), method=metodo,
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as ex:
        raise _error_http(ex) from None
    except urllib.error.URLError as ex:
        raise RuntimeError(f"Sin conexión con Instagram: {getattr(ex, 'reason', ex)}") from None


# --- login (compartido por todas las cuentas: una sola app de Meta) ------------------------------
def url_login(app_id, redirect, state):
    q = {"client_id": str(app_id).strip(), "redirect_uri": redirect, "response_type": "code",
         "scope": SCOPES, "state": state, "force_reauth": "true"}
    return AUTORIZAR + "?" + urllib.parse.urlencode(q)


def canjear_codigo(app_id, app_secret, redirect, code):
    """code -> {"access_token", "user_id", "permisos", "obtenido", "refrescado", "vence"} (token de 60 días)."""
    code = (code or "").split("#")[0].strip()  # Instagram puede agregar "#_" al final del código
    d = pedir("POST", TOKEN_CORTO, datos={"client_id": str(app_id).strip(), "client_secret": str(app_secret).strip(),
                                          "grant_type": "authorization_code", "redirect_uri": redirect, "code": code})
    if isinstance(d.get("data"), list) and d["data"]:
        d = d["data"][0]
    corto = d.get("access_token")
    if not corto:
        raise RuntimeError("Instagram no devolvió el permiso.")
    permisos = d.get("permissions") or ""
    if isinstance(permisos, list):
        permisos = ",".join(permisos)
    l = pedir("GET", f"{GRAPH}/access_token", {"grant_type": "ig_exchange_token",
                                               "client_secret": str(app_secret).strip(), "access_token": corto})
    if not l.get("access_token"):
        raise RuntimeError("Instagram no devolvió el token de larga duración.")
    ahora = time.time()
    return {"access_token": l["access_token"], "user_id": str(d.get("user_id", "")), "permisos": permisos,
            "obtenido": ahora, "refrescado": ahora, "vence": ahora + int(l.get("expires_in") or 5184000) - 3600}


def perfil_con_token(token, version=VERSION):
    return pedir("GET", f"{GRAPH}/{version}/me", {"fields": "user_id,username,name,account_type,profile_picture_url,"
                                                             "followers_count,follows_count,media_count",
                                                  "access_token": token})


# --- túnel temporal (URL pública para que Meta baje los archivos) ---------------------------------
class _Archivo(http.server.BaseHTTPRequestHandler):
    archivos = {}  # "/<token>/<nombre>" -> (Path, tipo)

    def log_message(self, *a):
        pass

    def _servir(self, cuerpo):
        it = self.archivos.get(self.path.split("?")[0])
        if not it or not it[0].exists():
            self.send_response(404)
            self.end_headers()
            return
        p, tipo = it
        tam = p.stat().st_size
        rango = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        ini, fin = (int(rango.group(1)), int(rango.group(2) or tam - 1)) if rango else (0, tam - 1)
        fin = min(fin, tam - 1)
        self.send_response(206 if rango else 200)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - ini + 1))
        if rango:
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        self.end_headers()
        if not cuerpo:
            return
        with open(p, "rb") as f:
            f.seek(ini)
            restan = fin - ini + 1
            while restan > 0:
                trozo = f.read(min(1 << 20, restan))
                if not trozo:
                    break
                try:
                    self.wfile.write(trozo)
                except (ConnectionResetError, BrokenPipeError):
                    return
                restan -= len(trozo)

    def do_GET(self):
        self._servir(True)

    def do_HEAD(self):
        self._servir(False)


class Tunel:
    """Mini servidor (solo los archivos pedidos, ruta con token al azar) + túnel rápido de Cloudflare."""

    def __init__(self, exe):
        self.exe, self.proc, self.srv, self.base = exe, None, None, ""
        self.token = secrets.token_urlsafe(24)

    def abrir(self, archivos, espera=60):
        rutas = {f"/{self.token}/{n}": (Path(p), t) for n, (p, t) in archivos.items()}
        manejador = type("ArchivoTunel", (_Archivo,), {"archivos": rutas})
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), manejador)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        puerto = self.srv.server_address[1]
        self.proc = subprocess.Popen([self.exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{puerto}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                     errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        hallado = {}

        def leer_salida():
            for linea in self.proc.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", linea)
                if m and "url" not in hallado:
                    hallado["url"] = m.group(0)
        threading.Thread(target=leer_salida, daemon=True).start()
        fin = time.time() + espera
        while time.time() < fin and "url" not in hallado and self.proc.poll() is None:
            time.sleep(0.5)
        if "url" not in hallado:
            self.cerrar()
            raise RuntimeError("No se pudo abrir el túnel de Cloudflare (cloudflared no dio una dirección).")
        self.base = hallado["url"]
        prueba = self.url(next(iter(archivos)))
        while time.time() < fin + 60:  # el nombre nuevo tarda unos segundos en existir en internet
            try:
                with urllib.request.urlopen(urllib.request.Request(prueba, method="HEAD"), timeout=10) as r:
                    if r.status in (200, 206):
                        return self
            except Exception:
                time.sleep(3)
        self.cerrar()
        raise RuntimeError("El túnel de Cloudflare abrió pero no respondió desde internet.")

    def url(self, nombre):
        return f"{self.base}/{self.token}/{urllib.parse.quote(nombre)}"

    def cerrar(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        try:
            if self.srv:
                self.srv.shutdown()
                self.srv.server_close()
        except Exception:
            pass


def buscar_cloudflared(raiz, configurado=None):
    if configurado and Path(configurado).exists():
        return str(configurado)
    for c in (Path(raiz) / "herramientas" / "cloudflared.exe", Path(raiz) / "cloudflared.exe"):
        if c.exists():
            return str(c)
    p = shutil.which("cloudflared")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    for c in (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "cloudflared" / "cloudflared.exe",
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "cloudflared" / "cloudflared.exe",
              Path(local) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe" if local else None):
        if c and c.exists():
            return str(c)
    return None


# --- una cuenta -------------------------------------------------------------------------------------
class Cuenta:
    def __init__(self, carpeta, leer, escribir, version=VERSION, cloudflared=None):
        self.dir = Path(carpeta)
        self.leer, self.escribir = leer, escribir
        self.version = version or VERSION
        self.cloudflared = cloudflared  # función que devuelve la ruta del exe (o None)
        self.token_p = self.dir / "token.json"
        self.estado_p = self.dir / "estado.json"
        self.medios_p = self.dir / "medios.json"
        self.historial_p = self.dir / "historial.json"
        self._tok_lock = threading.Lock()

    def _anotar(self, **kw):
        e = self.leer(self.estado_p, {})
        e.update(kw)
        self.escribir(self.estado_p, e)

    def estado(self):
        e = self.leer(self.estado_p, {})
        tok = self.leer(self.token_p, {})
        conectado = bool(tok.get("access_token")) and not tok.get("vencido")
        permisos = tok.get("permisos") or ""
        return {"conectado": conectado, "vencido": bool(tok.get("vencido")),
                "dias_token": round((tok["vence"] - time.time()) / 86400, 1) if tok.get("vence") else None,
                "usuario": e.get("usuario", ""), "nombre": e.get("nombre", ""), "tipo_cuenta": e.get("tipo_cuenta", ""),
                "foto": e.get("foto", ""), "seguidores": e.get("seguidores"), "siguiendo": e.get("siguiendo"),
                "publicaciones": e.get("publicaciones"), "ultima": e.get("ultima", ""), "error": e.get("error", ""),
                "aviso": e.get("aviso", ""), "cuenta_28d": e.get("cuenta_28d") or {}, "limite": e.get("limite") or {},
                "puede_publicar": PUBLICAR in permisos, "puede_comentar": "instagram_business_manage_comments" in permisos,
                "puede_insights": "instagram_business_manage_insights" in permisos}

    def _access(self):
        with self._tok_lock:
            tok = self.leer(self.token_p, {})
            if tok.get("vencido"):
                raise TokenVencido("vencido")
            if not tok.get("access_token"):
                raise RuntimeError("Esta cuenta no está conectada.")
            ahora = time.time()
            if ahora > tok.get("vence", 0):
                tok["vencido"] = True
                tok.pop("access_token", None)
                self.escribir(self.token_p, tok)
                raise TokenVencido("vencido")
            # refresco: el token tiene que tener al menos 24 h; cada refresco lo lleva a 60 días de nuevo
            viejo = ahora - tok.get("refrescado", tok.get("obtenido", ahora)) > 5 * 86400
            poco = tok.get("vence", 0) - ahora < 20 * 86400
            if ahora - tok.get("obtenido", ahora) > 86400 and (viejo or poco):
                try:
                    d = pedir("GET", f"{GRAPH}/refresh_access_token",
                              {"grant_type": "ig_refresh_token", "access_token": tok["access_token"]})
                    if d.get("access_token"):
                        tok.update(access_token=d["access_token"], refrescado=ahora,
                                   vence=ahora + int(d.get("expires_in") or 5184000) - 3600)
                        self.escribir(self.token_p, tok)
                except TokenVencido:
                    tok["vencido"] = True
                    tok.pop("access_token", None)
                    self.escribir(self.token_p, tok)
                    raise
                except Exception:
                    pass
            return tok["access_token"]

    def _url(self, ruta):
        return f"{GRAPH}/{self.version}/{ruta.lstrip('/')}"

    def _get(self, ruta, **params):
        return pedir("GET", self._url(ruta), {**params, "access_token": self._access()})

    def _post(self, ruta, **datos):
        return pedir("POST", self._url(ruta), datos={**datos, "access_token": self._access()})

    def ig_id(self):
        tok = self.leer(self.token_p, {})
        return tok.get("ig_id") or tok.get("user_id") or "me"

    # --- datos ------------------------------------------------------------------------------------
    def perfil(self):
        u = self._get("me", fields="user_id,username,name,account_type,profile_picture_url,"
                                   "followers_count,follows_count,media_count")
        tok = self.leer(self.token_p, {})
        if u.get("user_id") and tok.get("ig_id") != str(u["user_id"]):
            tok["ig_id"] = str(u["user_id"])
            self.escribir(self.token_p, tok)
        return u

    def medios(self, paginas=2):
        out, despues = [], None
        campos = ("id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url,"
                  "like_count,comments_count")
        for _ in range(paginas):
            p = {"fields": campos, "limit": 50}
            if despues:
                p["after"] = despues
            d = self._get(f"{self.ig_id()}/media", **p)
            out += d.get("data") or []
            despues = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            if not despues or not (d.get("paging") or {}).get("next"):
                break
        return out

    def insights(self, media_id, es_reel):
        """{metrica: valor}. Si la API rechaza alguna métrica, se averigua cuáles acepta y se recuerda."""
        tipo = "reel" if es_reel else "post"
        ok_prev = (self.leer(self.estado_p, {}).get("metricas_ok") or {}).get(tipo)
        lista = ok_prev or (METRICAS_REEL if es_reel else METRICAS_POST)

        def leer_vals(d):
            out = {}
            for m in d.get("data") or []:
                v = (m.get("values") or [{}])[0].get("value") if m.get("values") else (m.get("total_value") or {}).get("value")
                if isinstance(v, (int, float)):
                    out[m.get("name")] = v
            return out
        try:
            return leer_vals(self._get(f"{media_id}/insights", metric=",".join(lista)))
        except TokenVencido:
            raise
        except ErrorInstagram:
            if ok_prev:
                raise
        ok, vals = [], {}
        for m in lista:
            try:
                vals.update(leer_vals(self._get(f"{media_id}/insights", metric=m)))
                ok.append(m)
            except TokenVencido:
                raise
            except ErrorInstagram:
                pass
        if ok:
            e = self.leer(self.estado_p, {})
            e.setdefault("metricas_ok", {})[tipo] = ok
            self.escribir(self.estado_p, e)
        return vals

    def insights_cuenta(self):
        hasta = int(time.time())
        d = self._get(f"{self.ig_id()}/insights", metric="views,reach,total_interactions,accounts_engaged",
                      period="day", metric_type="total_value", since=hasta - 28 * 86400, until=hasta)
        return {m.get("name"): (m.get("total_value") or {}).get("value") for m in d.get("data") or []}

    def comentarios(self, media_id):
        try:
            d = self._get(f"{media_id}/comments", fields="id,text,username,timestamp,like_count,replies{id,username}",
                          limit=50)
        except ErrorInstagram:
            d = self._get(f"{media_id}/comments", fields="id,text,username,timestamp", limit=50)
        out = []
        for c in d.get("data") or []:
            resp = ((c.get("replies") or {}).get("data")) or []
            out.append({"id": c.get("id", ""), "media": media_id, "usuario": c.get("username") or "",
                        "texto": (c.get("text") or "").strip(), "fecha": self._local(c.get("timestamp") or ""),
                        "likes": int(c.get("like_count") or 0), "respuestas": len(resp),
                        "respondido_por_mi": any(r.get("username") == self.leer(self.estado_p, {}).get("usuario")
                                                 for r in resp)})
        return [c for c in out if c["texto"]]

    def responder(self, comentario_id, texto):
        texto = (texto or "").strip()
        if not texto:
            raise RuntimeError("Escribí la respuesta.")
        if not re.fullmatch(r"\d+", str(comentario_id or "")):
            raise RuntimeError("Comentario desconocido.")
        if "instagram_business_manage_comments" not in (self.leer(self.token_p, {}).get("permisos") or ""):
            raise RuntimeError("Falta el permiso de comentarios: reconectá la cuenta y aceptalo.")
        d = self._post(f"{comentario_id}/replies", message=texto[:2200])
        return {"ok": True, "id": d.get("id", "")}

    def ocultar(self, comentario_id, ocultar=True):
        if not re.fullmatch(r"\d+", str(comentario_id or "")):
            raise RuntimeError("Comentario desconocido.")
        self._post(str(comentario_id), hide="true" if ocultar else "false")
        return {"ok": True}

    def limite(self):
        d = self._get(f"{self.ig_id()}/content_publishing_limit", fields="quota_usage,config")
        it = (d.get("data") or [{}])[0]
        lim = {"usadas": int(it.get("quota_usage") or 0),
               "total": int((it.get("config") or {}).get("quota_total") or LIMITE_24H),
               "fecha": datetime.now().isoformat(timespec="minutes")}
        self._anotar(limite=lim)
        return lim

    # --- publicar ---------------------------------------------------------------------------------
    def _esperar(self, contenedor, avisar, que="el archivo"):
        fin = time.time() + ESPERA_PROCESO_S
        while time.time() < fin:
            d = self._get(contenedor, fields="status_code,status")
            sc = d.get("status_code")
            if sc in ("FINISHED", "PUBLISHED"):
                return
            if sc in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram no pudo procesar {que} ({sc}): {str(d.get('status') or '')[:200]}")
            avisar(f"Instagram está procesando {que}")
            time.sleep(10)
        raise RuntimeError("Instagram tardó más de 20 minutos en procesar.")

    def _crear(self, **datos):
        d = self._post(f"{self.ig_id()}/media", **datos)
        if not d.get("id"):
            raise ErrorInstagram("Instagram no devolvió el contenedor.")
        return d

    def _subir_resumable(self, ruta, datos, avisar):
        """Contenedor con upload_type=resumable + el archivo LOCAL a rupload.facebook.com. Devuelve el id."""
        d = self._crear(**datos, upload_type="resumable")
        cid = d["id"]
        uri = d.get("uri") or f"{RUPLOAD}/{self.version}/{cid}"
        if not uri.startswith("https://rupload.facebook.com/"):
            raise ErrorInstagram("Instagram devolvió una dirección de subida inesperada.")
        tam = Path(ruta).stat().st_size
        avisar(f"subiendo el video ({tam / 1e6:.1f} MB)")
        with open(ruta, "rb") as f:
            req = urllib.request.Request(uri, data=f, method="POST", headers={
                "Authorization": f"OAuth {self._access()}", "offset": "0", "file_size": str(tam),
                "Content-Length": str(tam), "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    res = json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as ex:
                raise _error_http(ex) from None
        if res.get("success") is False:
            raise ErrorInstagram(f"La subida a Instagram falló: {str(res.get('message') or '')[:200]}")
        return cid

    def _tunel(self, archivos, avisar):
        exe = self.cloudflared() if self.cloudflared else None
        if not exe:
            raise RuntimeError("Instagram pide las fotos en una dirección pública y falta cloudflared.exe "
                               "(volvé a correr Instalar.bat o mirá CONECTAR-INSTAGRAM.md).")
        avisar("abriendo el túnel temporal para que Instagram baje los archivos")
        return Tunel(exe).abrir(archivos)

    @staticmethod
    def _es_video(p):
        return Path(p).suffix.lower() in (".mp4", ".mov", ".m4v")

    def publicar(self, tipo, archivos, caption="", avisar=None, publicar_en=None):
        """tipo: reel | foto | carrusel | historia. archivos: rutas locales (fotos ya en JPEG).
        Devuelve {ok, id, permalink, publicado, subida}."""
        avisar = avisar or (lambda _t: None)
        archivos = [Path(a) for a in archivos]
        for a in archivos:
            if not a.exists():
                raise RuntimeError(f"No encuentro el archivo {a.name} (¿lo moviste o lo borraste?).")
        if PUBLICAR not in (self.leer(self.token_p, {}).get("permisos") or ""):
            raise RuntimeError("Falta el permiso de publicar: reconectá la cuenta y aceptalo.")
        caption = (caption or "")[:2200]
        try:
            lim = self.limite()
            if lim["usadas"] >= lim["total"]:
                raise RuntimeError(f"Llegaste al límite de Instagram: {lim['total']} publicaciones por API en 24 h.")
        except TokenVencido:
            raise
        except RuntimeError as ex:
            if "límite" in str(ex):
                raise
        except Exception:
            pass
        tunel, usado, cid = None, "", None
        try:
            if tipo in ("reel", "historia") and self._es_video(archivos[0]):
                datos = {"media_type": "REELS" if tipo == "reel" else "STORIES"}
                if tipo == "reel":
                    datos.update(caption=caption, share_to_feed="true")
                try:
                    avisar("creando la publicación en Instagram")
                    cid = self._subir_resumable(archivos[0], datos, avisar)
                    self._esperar(cid, avisar, "el video")
                    usado = "directa"
                except TokenVencido:
                    raise
                except Exception as ex:
                    avisar("la subida directa no anduvo, pruebo con el túnel")
                    self._anotar(aviso_resumable=str(ex)[:200])
                    tunel = self._tunel({"video.mp4": (archivos[0], "video/mp4")}, avisar)
                    cid = self._crear(**datos, video_url=tunel.url("video.mp4"))["id"]
                    self._esperar(cid, avisar, "el video")
                    usado = "tunel"
            elif tipo in ("foto", "historia"):
                tunel = self._tunel({"foto.jpg": (archivos[0], "image/jpeg")}, avisar)
                datos = {"image_url": tunel.url("foto.jpg")}
                if tipo == "historia":
                    datos["media_type"] = "STORIES"
                else:
                    datos["caption"] = caption
                avisar("creando la publicación en Instagram")
                cid = self._crear(**datos)["id"]
                self._esperar(cid, avisar, "la foto")
                usado = "tunel"
            elif tipo == "carrusel":
                if not 2 <= len(archivos) <= 10:
                    raise RuntimeError("Un carrusel lleva entre 2 y 10 archivos.")
                nombres = {}
                for i, a in enumerate(archivos):
                    v = self._es_video(a)
                    nombres[f"{i + 1}.{'mp4' if v else 'jpg'}"] = (a, "video/mp4" if v else "image/jpeg")
                tunel = self._tunel(nombres, avisar)
                hijos = []
                for i, (n, (a, t)) in enumerate(nombres.items()):
                    avisar(f"preparando la parte {i + 1} de {len(nombres)}")
                    if t == "video/mp4":
                        h = self._crear(media_type="VIDEO", video_url=tunel.url(n), is_carousel_item="true")["id"]
                        self._esperar(h, avisar, f"el video {i + 1}")
                    else:
                        h = self._crear(image_url=tunel.url(n), is_carousel_item="true")["id"]
                    hijos.append(h)
                cid = self._crear(media_type="CAROUSEL", children=",".join(hijos), caption=caption)["id"]
                self._esperar(cid, avisar, "el carrusel")
                usado = "tunel"
            else:
                raise RuntimeError("Tipo de publicación desconocido.")
        finally:
            if tunel:
                tunel.cerrar()
        if publicar_en and publicar_en > time.time():
            avisar("listo en Instagram, esperando la hora programada")
            while time.time() < publicar_en:
                time.sleep(min(20, max(1, publicar_en - time.time())))
        avisar("publicando")
        mid = self._post(f"{self.ig_id()}/media_publish", creation_id=cid).get("id")
        if not mid:
            raise ErrorInstagram("Instagram no confirmó la publicación.")
        info = {}
        try:
            info = self._get(mid, fields="permalink,timestamp")
        except Exception:
            pass
        return {"ok": True, "id": mid, "permalink": info.get("permalink", ""),
                "publicado": self._local(info.get("timestamp") or "") or datetime.now().isoformat(timespec="minutes"),
                "subida": usado}

    # --- sincronización -----------------------------------------------------------------------------
    @staticmethod
    def _local(iso):
        if not iso:
            return ""
        try:
            iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso.replace("Z", "+00:00"))
            return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
        except Exception:
            return iso[:16]

    def historial(self):
        h = self.leer(self.historial_p, [])
        return h if isinstance(h, list) else []

    def sincronizar(self):
        try:
            u = self.perfil()
            crudos = self.medios()
            previos = {x["id"]: x for x in (self.leer(self.medios_p, {}).get("medios") or [])}
            out, avisos = [], []
            puede_ins = "instagram_business_manage_insights" in (self.leer(self.token_p, {}).get("permisos") or "")
            for i, m in enumerate(crudos):
                es_reel = m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO"
                v = {"id": m["id"], "caption": m.get("caption") or "", "tipo": m.get("media_product_type") or m.get("media_type") or "",
                     "media_type": m.get("media_type") or "", "reel": es_reel, "url": m.get("permalink", ""),
                     "publicado": self._local(m.get("timestamp") or ""),
                     "miniatura": m.get("thumbnail_url") or (m.get("media_url") if m.get("media_type") != "VIDEO" else "") or "",
                     "likes": int(m.get("like_count") or 0), "comentarios": int(m.get("comments_count") or 0)}
                ant = previos.get(m["id"]) or {}
                for k in ("vistas", "alcance", "guardados", "compartidos", "interacciones"):
                    v[k] = ant.get(k)
                if puede_ins and i < INSIGHTS_MAX:
                    try:
                        ins = self.insights(m["id"], es_reel)
                        v.update(vistas=ins.get("views", v["vistas"]), alcance=ins.get("reach", v["alcance"]),
                                 guardados=ins.get("saved", v["guardados"]), compartidos=ins.get("shares", v["compartidos"]),
                                 interacciones=ins.get("total_interactions", v["interacciones"]))
                    except TokenVencido:
                        raise
                    except Exception as ex:
                        if not avisos:
                            avisos.append(f"Estadísticas: {str(ex)[:150]}")
                out.append(v)
            ahora = datetime.now().isoformat(timespec="minutes")
            cuenta = {}
            if puede_ins:
                try:
                    cuenta = self.insights_cuenta()
                except TokenVencido:
                    raise
                except Exception:
                    cuenta = self.leer(self.estado_p, {}).get("cuenta_28d") or {}
            self.escribir(self.medios_p, {"fecha": ahora, "medios": out})
            h = self.historial()
            foto = {"fecha": ahora, "seguidores": u.get("followers_count"),
                    "vistas": sum(x.get("vistas") or 0 for x in out), "publicaciones": u.get("media_count")}
            if h and h[-1].get("fecha", "")[:13] == ahora[:13]:
                h.pop()
            h.append(foto)
            self.escribir(self.historial_p, h[-24 * 120:])
            try:
                self.limite()
            except TokenVencido:
                raise
            except Exception:
                pass
            if not puede_ins:
                avisos.append("Sin permiso de estadísticas: reconectá la cuenta y aceptá 'insights'.")
            self._anotar(ultima=ahora, error="", aviso=" · ".join(avisos), usuario=u.get("username", ""),
                         nombre=u.get("name", ""), tipo_cuenta=u.get("account_type", ""),
                         foto=u.get("profile_picture_url", ""), seguidores=u.get("followers_count"),
                         siguiendo=u.get("follows_count"), publicaciones=u.get("media_count"), cuenta_28d=cuenta)
            return {"ok": True, "medios": len(out), "aviso": " · ".join(avisos)}
        except TokenVencido:
            msg = "El permiso de Instagram venció o se revocó: reconectá la cuenta."
            self._anotar(error=msg)
            return {"ok": False, "vencido": True, "error": msg}
        except Exception as ex:  # el panel tiene que seguir andando aunque Instagram falle
            self._anotar(error=str(ex)[:300])
            return {"ok": False, "error": str(ex)[:300]}
