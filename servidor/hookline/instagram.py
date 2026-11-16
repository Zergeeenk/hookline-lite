"""Instagram API with Instagram Login (graph.instagram.com). Portado de Hookline Lite (instagram_api.py).

Cambios respecto de Lite:
  - El token vive cifrado en la tabla credenciales_ig (antes: cuentas/<id>/token.json en claro).
  - Fotos/carruseles/historias: Meta baja el archivo de una URL firmada de este servicio o de S3 (sin cloudflared).
  - Publicar es reanudable: el creation_id se guarda en la pieza antes de esperar la hora, y si la pieza ya tiene
    media_id no se vuelve a publicar.
Nunca se loguean tokens: los errores de Meta se reducen a código + mensaje.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import almacen, seguridad
from .config import config
from .db import sesion
from .modelos import CredencialIG

GRAPH = "https://graph.instagram.com"
GRAPH_FB = "https://graph.facebook.com"             # inicio de sesión unificado (apps nuevas de Meta)
DIALOGO_FB = "https://www.facebook.com"
AUTORIZAR = "https://www.instagram.com/oauth/authorize"
TOKEN_CORTO = "https://api.instagram.com/oauth/access_token"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
SCOPES = ("instagram_business_basic,instagram_business_content_publish,"
          "instagram_business_manage_comments,instagram_business_manage_insights")
P_PUBLICAR = "instagram_business_content_publish"
P_COMENTARIOS = "instagram_business_manage_comments"
P_INSIGHTS = "instagram_business_manage_insights"
LIMITE_24H = 100
METRICAS = ["views", "reach", "likes", "comments", "saved", "shares", "total_interactions"]
INSIGHTS_MAX = 40
ESPERA_PROCESO_S = 20 * 60
DURACION_TOKEN = 5184000  # 60 días


class TokenVencido(RuntimeError):
    pass


class ErrorInstagram(RuntimeError):
    def __init__(self, msg, codigo=None, sub=None):
        super().__init__(msg)
        self.codigo, self.sub = codigo, sub


def _error_http(ex: urllib.error.HTTPError) -> Exception:
    try:
        d = json.loads(ex.read())
    except Exception:
        d = {}
    err = d.get("error") if isinstance(d.get("error"), dict) else {}
    msg = err.get("error_user_msg") or err.get("message") or d.get("error_message") or d.get("error_description") or ""
    if not msg and isinstance(d.get("error"), str):
        msg = d["error"]
    codigo = err.get("code") or d.get("code")
    if codigo == 190 or (d.get("error_type") == "OAuthException" and "expired" in str(msg).lower()):
        return TokenVencido(str(msg)[:200] or "token vencido")
    return ErrorInstagram(f"Instagram respondió {ex.code}{f' ({codigo})' if codigo else ''}: {str(msg)[:240]}".strip(),
                          codigo, err.get("error_subcode"))


def pedir(metodo: str, url: str, params: dict | None = None, datos: dict | None = None, timeout: int = 60,
          headers: dict | None = None) -> dict:
    """Única salida a Meta (los tests la reemplazan)."""
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


# --- login -------------------------------------------------------------------------------------------------------
def url_login(app_id: str, redirect: str, state: str) -> str:
    q = {"client_id": str(app_id).strip(), "redirect_uri": redirect, "response_type": "code",
         "scope": SCOPES, "state": state, "force_reauth": "true"}
    return AUTORIZAR + "?" + urllib.parse.urlencode(q)


def canjear_codigo(app_id: str, app_secret: str, redirect: str, code: str) -> dict:
    """code → {access_token, user_id, permisos, obtenido, refrescado, vence} (token de 60 días)."""
    code = (code or "").split("#")[0].strip()
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
    t = time.time()
    return {"access_token": l["access_token"], "user_id": str(d.get("user_id", "")), "permisos": permisos,
            "obtenido": t, "refrescado": t, "vence": t + int(l.get("expires_in") or DURACION_TOKEN) - 3600}


# --- inicio de sesión unificado (apps de Meta creadas desde fines de 2026) ---------------------------------------------
# Meta ya no les da "Instagram API with Instagram Login" a las apps nuevas: les da "API setup with Unified login", que es
# el inicio de sesión con Facebook para empresas con una configuración (config_id). El código se canjea en
# graph.facebook.com por un token de usuario del sistema que no vence; las cuentas de Instagram que la persona eligió se
# leen de /me/assigned_instagram_accounts y todo lo demás (publicar, comentarios, estadísticas) va a graph.facebook.com.
EQUIVALENTES = {"instagram_basic": "instagram_business_basic",
                "instagram_content_publish": "instagram_business_content_publish",
                "instagram_manage_comments": "instagram_business_manage_comments",
                "instagram_manage_insights": "instagram_business_manage_insights",
                "instagram_manage_messages": "instagram_business_manage_messages"}
NUNCA = 100 * 365 * 86400


def normalizar_permisos(permisos) -> str:
    """Los permisos del inicio de sesión unificado se llaman distinto (instagram_content_publish): se suman los
    nombres de siempre (instagram_business_content_publish) para que el resto del sistema no cambie."""
    lista = [p.strip() for p in (permisos.split(",") if isinstance(permisos, str) else (permisos or [])) if str(p).strip()]
    out = []
    for p in lista + [EQUIVALENTES[p] for p in lista if p in EQUIVALENTES]:
        if p not in out:
            out.append(p)
    return ",".join(out)


def url_login_unificado(app_id: str, config_id: str, redirect: str, state: str) -> str:
    q = {"client_id": str(app_id).strip(), "config_id": str(config_id).strip(), "redirect_uri": redirect,
         "response_type": "code", "override_default_response_type": "true", "state": state}
    return f"{DIALOGO_FB}/{config().ig_version}/dialog/oauth?" + urllib.parse.urlencode(q)


def canjear_codigo_unificado(app_id: str, app_secret: str, redirect: str, code: str) -> dict:
    """code → token de usuario del sistema (no vence) con sus permisos, leídos de debug_token."""
    code = (code or "").split("#")[0].strip()
    v = config().ig_version
    d = pedir("GET", f"{GRAPH_FB}/{v}/oauth/access_token",
              {"client_id": str(app_id).strip(), "client_secret": str(app_secret).strip(), "redirect_uri": redirect,
               "code": code})
    tok = d.get("access_token")
    if not tok:
        raise RuntimeError("Meta no devolvió el permiso.")
    t = time.time()
    vence, permisos = 0, ""
    try:
        info = pedir("GET", f"{GRAPH_FB}/{v}/debug_token",
                     {"input_token": tok, "access_token": f"{str(app_id).strip()}|{str(app_secret).strip()}"})
        info = info.get("data") or {}
        permisos = info.get("scopes") or ""
        vence = int(info.get("expires_at") or 0)
    except Exception:
        pass        # sin debug_token igual se puede seguir: los permisos se descubren al usarlos
    if not vence and d.get("expires_in"):
        vence = int(t + int(d["expires_in"]))
    return {"access_token": tok, "user_id": "", "permisos": normalizar_permisos(permisos), "obtenido": t,
            "refrescado": t, "vence": (vence - 3600) if vence else t + NUNCA, "via": "unificado"}


CAMPOS_PERFIL_FB = "id,username,name,profile_picture_url,followers_count,follows_count,media_count"


def cuentas_unificado(token: str) -> list[dict]:
    """Las cuentas de Instagram que la persona eligió al autorizar, cada una con su perfil."""
    v = config().ig_version
    d = pedir("GET", f"{GRAPH_FB}/{v}/me/assigned_instagram_accounts", {"access_token": token})
    ids = []
    for x in d.get("data") or []:
        i = str((x or {}).get("id") or "").strip() if isinstance(x, dict) else ""
        if re.fullmatch(r"\d{5,30}", i) and i not in ids:
            ids.append(i)
    out = []
    for i in ids[:50]:
        p = pedir("GET", f"{GRAPH_FB}/{v}/{i}", {"fields": CAMPOS_PERFIL_FB, "access_token": token})
        if p.get("username"):
            out.append({**p, "user_id": str(p.get("id") or i), "account_type": p.get("account_type") or ""})
    return out


def perfil_con_token(token: str) -> dict:
    return pedir("GET", f"{GRAPH}/{config().ig_version}/me",
                 {"fields": "user_id,username,name,account_type,profile_picture_url,followers_count,follows_count,"
                            "media_count", "access_token": token})


def guardar_credencial(cuenta_id: str, tok: dict) -> None:
    with sesion() as s:
        c = s.get(CredencialIG, cuenta_id) or CredencialIG(cuenta_id=cuenta_id)
        c.token_cifrado = seguridad.cifrar(tok["access_token"])
        c.via = tok.get("via") or "instagram"
        c.permisos = tok.get("permisos") or ""
        c.obtenido, c.refrescado, c.vence = tok["obtenido"], tok["refrescado"], tok["vence"]
        c.vencido = False
        s.add(c)


def local_iso(iso: str) -> datetime | None:
    """Fecha de Instagram ('2026-11-01T22:10:00+0000') → datetime UTC."""
    if not iso:
        return None
    try:
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso.replace("Z", "+00:00"))
        return datetime.fromisoformat(iso).astimezone(timezone.utc)
    except ValueError:
        return None


# --- una cuenta ----------------------------------------------------------------------------------------------------
_locks: dict[str, threading.Lock] = {}


class Cliente:
    """Llamadas a la API para una cuenta. El token se lee (y se refresca) de credenciales_ig."""

    def __init__(self, cuenta_id: str):
        self.cuenta_id = cuenta_id
        self.version = config().ig_version
        self._lock = _locks.setdefault(cuenta_id, threading.Lock())
        self._ig_id = ""

    # token --------------------------------------------------------------------------------------------------
    def credencial(self) -> CredencialIG | None:
        with sesion() as s:
            return s.get(CredencialIG, self.cuenta_id)

    def permisos(self) -> str:
        c = self.credencial()
        return (c.permisos or "") if c else ""

    def unificado(self) -> bool:
        c = self.credencial()
        return bool(c and (c.via or "") == "unificado")

    def conectada(self) -> bool:
        c = self.credencial()
        return bool(c and not c.vencido and c.vence > time.time())

    def _marcar_vencido(self) -> None:
        with sesion() as s:
            c = s.get(CredencialIG, self.cuenta_id)
            if c:
                c.vencido = True
        from .comun import evento
        evento("cuenta.token_vencido", cuenta=self.cuenta_id)

    def token(self, forzar_refresco: bool = False) -> str:
        """Devuelve el token; lo refresca si tiene >24 h y (pasaron >5 días del último refresco o vence en <20 días)."""
        with self._lock:
            c = self.credencial()
            if not c:
                raise RuntimeError("Esta cuenta no está conectada.")
            if c.vencido:
                raise TokenVencido("vencido")
            t = time.time()
            if t > c.vence:
                self._marcar_vencido()
                raise TokenVencido("vencido")
            tok = seguridad.descifrar(c.token_cifrado)
            if (c.via or "") == "unificado":
                return tok          # token de usuario del sistema: no se refresca (no vence)
            viejo = t - c.refrescado > 5 * 86400
            poco = c.vence - t < 20 * 86400
            if t - c.obtenido > 86400 and (forzar_refresco or viejo or poco):
                try:
                    d = pedir("GET", f"{GRAPH}/refresh_access_token", {"grant_type": "ig_refresh_token",
                                                                      "access_token": tok})
                    if d.get("access_token"):
                        tok = d["access_token"]
                        with sesion() as s:
                            x = s.get(CredencialIG, self.cuenta_id)
                            x.token_cifrado = seguridad.cifrar(tok)
                            x.refrescado = t
                            x.vence = t + int(d.get("expires_in") or DURACION_TOKEN) - 3600
                except TokenVencido:
                    self._marcar_vencido()
                    raise
                except Exception:
                    pass  # se vuelve a intentar en la próxima llamada; el token actual sigue sirviendo
            return tok

    def _url(self, ruta: str) -> str:
        base = GRAPH_FB if self.unificado() else GRAPH
        return f"{base}/{self.version}/{ruta.lstrip('/')}"

    def get(self, ruta: str, **params) -> dict:
        return pedir("GET", self._url(ruta), {**params, "access_token": self.token()})

    def post(self, ruta: str, **datos) -> dict:
        return pedir("POST", self._url(ruta), datos={**datos, "access_token": self.token()})

    def ig_id(self) -> str:
        if not self._ig_id:
            from .modelos import Cuenta
            with sesion() as s:
                c = s.get(Cuenta, self.cuenta_id)
                self._ig_id = (c.ig_user_id if c else "") or "me"
        return self._ig_id

    # datos -------------------------------------------------------------------------------------------------------
    def perfil(self) -> dict:
        if self.unificado():                 # en graph.facebook.com /me es el usuario del sistema, no la cuenta
            if self.ig_id() == "me":
                raise RuntimeError("Falta el id de la cuenta de Instagram: reconectala desde Cuentas.")
            p = self.get(self.ig_id(), fields=CAMPOS_PERFIL_FB)
            return {**p, "user_id": str(p.get("id") or self.ig_id()), "account_type": p.get("account_type") or ""}
        return self.get("me", fields="user_id,username,name,account_type,profile_picture_url,followers_count,"
                                     "follows_count,media_count")

    def medios(self, paginas: int = 2) -> list[dict]:
        out, despues = [], None
        campos = ("id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url,"
                  "like_count,comments_count")
        for _ in range(paginas):
            p = {"fields": campos, "limit": 50}
            if despues:
                p["after"] = despues
            d = self.get(f"{self.ig_id()}/media", **p)
            out += d.get("data") or []
            despues = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            if not despues or not (d.get("paging") or {}).get("next"):
                break
        return out

    def insights(self, media_id: str, es_reel: bool, metricas_ok: dict | None) -> tuple[dict, list[str] | None]:
        """({metrica: valor}, lista de métricas aceptadas si hubo que averiguarla)."""
        tipo = "reel" if es_reel else "post"
        previas = (metricas_ok or {}).get(tipo)
        lista = previas or METRICAS

        def vals(d):
            out = {}
            for m in d.get("data") or []:
                v = (m.get("values") or [{}])[0].get("value") if m.get("values") else (m.get("total_value") or {}).get("value")
                if isinstance(v, (int, float)):
                    out[m.get("name")] = v
            return out
        try:
            return vals(self.get(f"{media_id}/insights", metric=",".join(lista))), None
        except ErrorInstagram:
            if previas:
                raise
        ok, todo = [], {}
        for m in lista:
            try:
                todo.update(vals(self.get(f"{media_id}/insights", metric=m)))
                ok.append(m)
            except ErrorInstagram:
                pass
        return todo, (ok or None)

    def insights_cuenta(self) -> dict:
        hasta = int(time.time())
        d = self.get(f"{self.ig_id()}/insights", metric="views,reach,total_interactions,accounts_engaged",
                     period="day", metric_type="total_value", since=hasta - 28 * 86400, until=hasta)
        return {m.get("name"): (m.get("total_value") or {}).get("value") for m in d.get("data") or []}

    def comentarios(self, media_id: str, mi_usuario: str) -> list[dict]:
        try:
            d = self.get(f"{media_id}/comments", fields="id,text,username,timestamp,like_count,replies{id,username}",
                         limit=50)
        except ErrorInstagram:
            d = self.get(f"{media_id}/comments", fields="id,text,username,timestamp", limit=50)
        out = []
        for c in d.get("data") or []:
            resp = ((c.get("replies") or {}).get("data")) or []
            texto = (c.get("text") or "").strip()
            if not texto:
                continue
            out.append({"id": c.get("id", ""), "media": media_id, "usuario": c.get("username") or "", "texto": texto,
                        "fecha": local_iso(c.get("timestamp") or ""), "likes": int(c.get("like_count") or 0),
                        "respuestas": len(resp),
                        "respondido_por_mi": bool(mi_usuario) and any(r.get("username") == mi_usuario for r in resp)})
        return out

    def responder(self, comentario_id: str, texto: str) -> dict:
        texto = (texto or "").strip()
        if not texto:
            raise ValueError("Escribí la respuesta.")
        if not re.fullmatch(r"\d+", str(comentario_id or "")):
            raise ValueError("Comentario desconocido.")
        if P_COMENTARIOS not in self.permisos():
            raise ValueError("Falta el permiso de comentarios: reconectá la cuenta y aceptalo.")
        d = self.post(f"{comentario_id}/replies", message=texto[:2200])
        return {"ok": True, "id": d.get("id", "")}

    def ocultar(self, comentario_id: str, ocultar: bool = True) -> dict:
        if not re.fullmatch(r"\d+", str(comentario_id or "")):
            raise ValueError("Comentario desconocido.")
        self.post(str(comentario_id), hide="true" if ocultar else "false")
        return {"ok": True}

    def limite(self) -> dict:
        d = self.get(f"{self.ig_id()}/content_publishing_limit", fields="quota_usage,config")
        it = (d.get("data") or [{}])[0]
        return {"usadas": int(it.get("quota_usage") or 0),
                "total": int((it.get("config") or {}).get("quota_total") or LIMITE_24H),
                "fecha": datetime.now(timezone.utc).isoformat(timespec="minutes")}

    # publicar ----------------------------------------------------------------------------------------------------
    def esperar_contenedor(self, cid: str, avisar: Callable[[str], None], que: str = "el archivo") -> None:
        fin = time.time() + ESPERA_PROCESO_S
        while time.time() < fin:
            d = self.get(cid, fields="status_code,status")
            sc = d.get("status_code")
            if sc in ("FINISHED", "PUBLISHED"):
                return
            if sc in ("ERROR", "EXPIRED"):
                raise ErrorInstagram(f"Instagram no pudo procesar {que} ({sc}): {str(d.get('status') or '')[:200]}")
            avisar(f"Instagram está procesando {que}")
            time.sleep(10)
        raise ErrorInstagram("Instagram tardó más de 20 minutos en procesar.")

    def crear(self, **datos) -> str:
        d = self.post(f"{self.ig_id()}/media", **datos)
        if not d.get("id"):
            raise ErrorInstagram("Instagram no devolvió el contenedor.")
        return d["id"]

    def subir_resumable(self, archivo: Path, datos: dict, avisar: Callable[[str], None]) -> str:
        d = self.post(f"{self.ig_id()}/media", **datos, upload_type="resumable")
        cid = d.get("id")
        if not cid:
            raise ErrorInstagram("Instagram no devolvió el contenedor.")
        uri = d.get("uri") or f"{RUPLOAD}/{self.version}/{cid}"
        if not uri.startswith("https://rupload.facebook.com/"):
            raise ErrorInstagram("Instagram devolvió una dirección de subida inesperada.")
        tam = Path(archivo).stat().st_size
        avisar(f"subiendo el video ({tam / 1e6:.1f} MB)")
        with open(archivo, "rb") as f:
            req = urllib.request.Request(uri, data=f, method="POST", headers={
                "Authorization": f"OAuth {self.token()}", "offset": "0", "file_size": str(tam),
                "Content-Length": str(tam), "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    res = json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as ex:
                raise _error_http(ex) from None
        if res.get("success") is False:
            raise ErrorInstagram(f"La subida a Instagram falló: {str(res.get('message') or '')[:200]}")
        return cid

    @staticmethod
    def es_video(p) -> bool:
        return Path(p).suffix.lower() in (".mp4", ".mov", ".m4v")

    def preparar(self, tipo: str, archivos: list[str], caption: str, avisar: Callable[[str], None]) -> tuple[str, str]:
        """Crea el contenedor y espera a que Instagram lo procese. archivos: rutas RELATIVAS (fotos ya en JPEG).
        Devuelve (creation_id, 'directa'|'url')."""
        for a in archivos:
            if not almacen.ruta(a).is_file():
                raise ValueError(f"No encuentro el archivo {Path(a).name} (¿lo movieron o lo borraron?).")
        if P_PUBLICAR not in self.permisos():
            raise ValueError("Falta el permiso de publicar: reconectá la cuenta y aceptalo.")
        caption = (caption or "")[:2200]
        try:
            lim = self.limite()
            if lim["usadas"] >= lim["total"]:
                raise ValueError(f"Llegaste al límite de Instagram: {lim['total']} publicaciones por API en 24 h.")
        except (TokenVencido, ValueError):
            raise
        except Exception:
            pass
        if tipo in ("reel", "historia") and self.es_video(archivos[0]):
            datos = {"media_type": "REELS" if tipo == "reel" else "STORIES"}
            if tipo == "reel":
                datos.update(caption=caption, share_to_feed="true")
            try:
                avisar("creando la publicación en Instagram")
                cid = self.subir_resumable(almacen.ruta(archivos[0]), datos, avisar)
                self.esperar_contenedor(cid, avisar, "el video")
                return cid, "directa"
            except TokenVencido:
                raise
            except Exception:
                avisar("la subida directa no anduvo, pruebo con la dirección pública")
                cid = self.crear(**datos, video_url=almacen.url_publica(archivos[0]))
                self.esperar_contenedor(cid, avisar, "el video")
                return cid, "url"
        if tipo in ("foto", "historia"):
            datos = {"image_url": almacen.url_publica(archivos[0])}
            if tipo == "historia":
                datos["media_type"] = "STORIES"
            else:
                datos["caption"] = caption
            avisar("creando la publicación en Instagram")
            cid = self.crear(**datos)
            self.esperar_contenedor(cid, avisar, "la foto")
            return cid, "url"
        if tipo == "carrusel":
            if not 2 <= len(archivos) <= 10:
                raise ValueError("Un carrusel lleva entre 2 y 10 archivos.")
            hijos = []
            for i, a in enumerate(archivos):
                avisar(f"preparando la parte {i + 1} de {len(archivos)}")
                if self.es_video(a):
                    h = self.crear(media_type="VIDEO", video_url=almacen.url_publica(a), is_carousel_item="true")
                    self.esperar_contenedor(h, avisar, f"el video {i + 1}")
                else:
                    h = self.crear(image_url=almacen.url_publica(a), is_carousel_item="true")
                hijos.append(h)
            cid = self.crear(media_type="CAROUSEL", children=",".join(hijos), caption=caption)
            self.esperar_contenedor(cid, avisar, "el carrusel")
            return cid, "url"
        raise ValueError("Tipo de publicación desconocido.")

    def publicar_contenedor(self, cid: str) -> dict:
        mid = self.post(f"{self.ig_id()}/media_publish", creation_id=cid).get("id")
        if not mid:
            raise ErrorInstagram("Instagram no confirmó la publicación.")
        info = {}
        try:
            info = self.get(mid, fields="permalink,timestamp")
        except Exception:
            pass
        return {"id": mid, "permalink": info.get("permalink", ""),
                "publicado": local_iso(info.get("timestamp") or "") or datetime.now(timezone.utc)}
