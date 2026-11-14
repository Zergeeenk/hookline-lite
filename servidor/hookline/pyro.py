"""Cliente de la API de Pyro (Model Pro): la API de solo lectura `/api/v1` con una clave de la agencia (`pyro_...`).

Única salida a Pyro (los tests la reemplazan con `transporte`). Lo que usa Hookline:
  GET /api/v1/models                 las modelos de la agencia
  GET /api/v1/models/{id}            su ficha: nicho y `socialAccounts` (sus cuentas de Instagram)
  GET /api/v1/models/{id}/content    su biblioteca de fotos y videos (por páginas)
Ver docs/API-METRICAS.md en pyromodels/backend.

Reglas:
  - Solo https (salvo HOOKLINE_PYRO_PERMITIR_HTTP=true, para un Pyro de prueba).
  - La clave solo viaja a la dirección donde se pegó: los archivos se bajan con la clave únicamente si son del
    mismo host; si no, se bajan sin clave.
  - Plazos cortos y tamaño máximo por archivo.
"""
from __future__ import annotations

import os
import re
import time
import urllib.parse
from pathlib import Path

import httpx

CLAVE_RE = re.compile(r"pyro_[A-Za-z0-9_\-]{20,80}")
MAX_ARCHIVO = 500 * 1024 * 1024
PLAZO = httpx.Timeout(20.0, connect=5.0)
transporte: httpx.BaseTransport | None = None          # los tests ponen un httpx.MockTransport


class ErrorPyro(ValueError):
    """Un error para mostrar tal cual en el panel."""


def permitir_http() -> bool:
    return os.environ.get("HOOKLINE_PYRO_PERMITIR_HTTP", "").lower() in ("1", "true", "si", "sí")


def normalizar_url(url: str) -> str:
    """'https://app.pyro.com/' → 'https://app.pyro.com'. Solo el origen: sin ruta, usuario, consulta ni fragmento."""
    url = "".join(str(url or "").split())
    malo = ErrorPyro("Escribí la dirección de Pyro, por ejemplo https://app.pyromodels.com")
    if "://" not in url:
        if not re.fullmatch(r"[\w.\-]+(:\d{1,5})?/?", url):        # 'javascript:...', 'https:', etc.
            raise malo
        url = "https://" + url
    url = url.rstrip("/")
    try:
        u = urllib.parse.urlsplit(url)
        u.port                                                      # puerto inválido → ValueError
    except ValueError:
        raise malo from None
    if u.scheme not in ("https", "http") or not u.hostname or not re.fullmatch(r"[a-z0-9.\-]+", u.hostname):
        raise malo
    if u.scheme == "http" and not permitir_http():
        raise ErrorPyro("La dirección de Pyro tiene que ser https.")
    if u.username or u.password or u.query or u.fragment or u.path.strip("/") not in ("", "api/v1", "api"):
        raise ErrorPyro("Poné solo la dirección de Pyro (https://dominio), sin rutas ni parámetros.")
    return f"{u.scheme}://{u.netloc}"


def validar_clave(clave: str) -> str:
    clave = "".join(str(clave or "").split())
    if not CLAVE_RE.fullmatch(clave):
        raise ErrorPyro("Eso no parece una clave de la API de Pyro (empieza con pyro_). "
                        "Se crea en Pyro → Accesos → API de Pyro.")
    return clave


def _cliente() -> httpx.Client:
    return httpx.Client(timeout=PLAZO, transport=transporte, follow_redirects=False,
                        headers={"Accept": "application/json", "User-Agent": "Hookline"})


def _error(r: httpx.Response) -> ErrorPyro:
    if r.status_code == 401:
        return ErrorPyro("Pyro rechazó la clave (puede estar revocada). Creá otra en Pyro → Accesos → API de Pyro.")
    if r.status_code == 403:
        return ErrorPyro("La clave de Pyro no tiene permiso para esto.")
    if r.status_code == 404:
        return ErrorPyro("Pyro no encontró eso (¿la dirección es la de Pyro?).")
    if r.status_code == 429:
        return ErrorPyro("Pyro pidió que esperemos un poco (límite de pedidos). Probá en un minuto.")
    return ErrorPyro(f"Pyro respondió con un error ({r.status_code}).")


def _espera(r: httpx.Response) -> float | None:
    if r.status_code != 429:
        return None
    try:
        s = float(r.headers.get("retry-after") or 2)
    except ValueError:
        s = 2.0
    return s if 0 <= s <= 15 else None


def pedir(url: str, clave: str, ruta: str, params: dict | None = None) -> dict:
    """GET {url}/api/v1{ruta} → JSON (dict)."""
    try:
        with _cliente() as c:
            for intento in range(2):
                r = c.get(f"{url}/api/v1{ruta}", params=params or None, headers={"Authorization": f"Bearer {clave}"})
                espera = _espera(r) if intento == 0 else None
                if espera is None:
                    break
                time.sleep(espera)                 # 429: Pyro dice cuánto esperar (120 pedidos por minuto)
    except httpx.TimeoutException:
        raise ErrorPyro("Pyro tardó demasiado en responder.") from None
    except httpx.HTTPError as ex:
        raise ErrorPyro(f"Sin conexión con Pyro ({type(ex).__name__}).") from None
    if r.status_code != 200:
        raise _error(r)
    try:
        d = r.json()
    except ValueError:
        raise ErrorPyro("Pyro respondió algo que no es JSON (¿la dirección es la de Pyro?).") from None
    if not isinstance(d, dict):
        raise ErrorPyro("Pyro respondió algo inesperado.")
    return d


# --- lo que usa Hookline ------------------------------------------------------------------------------------------
def modelos(url: str, clave: str) -> list[dict]:
    d = pedir(url, clave, "/models")
    return [m for m in d.get("models") or [] if isinstance(m, dict) and isinstance(m.get("id"), int)]


def modelo(url: str, clave: str, mid: int) -> dict:
    return pedir(url, clave, f"/models/{int(mid)}")


def handles_instagram(ficha: dict) -> list[str]:
    """Los usuarios de Instagram de la modelo según su ficha en Pyro (socialAccounts con network=instagram)."""
    out = []
    for a in ficha.get("socialAccounts") or []:
        if isinstance(a, dict) and str(a.get("network", "")).lower() == "instagram":
            h = limpiar_handle(a.get("handle") or a.get("url") or "")
            if h and h not in out:
                out.append(h)
    return out


def limpiar_handle(texto: str) -> str:
    t = str(texto or "").strip()
    m = re.search(r"instagram\.com/([\w.]+)", t)
    if m:
        t = m.group(1)
    return re.sub(r"[^\w.]", "", t.lstrip("@")).lower()[:30]


def biblioteca(url: str, clave: str, mid: int, maximo: int = 2000):
    """Recorre la biblioteca de la modelo, página por página. Devuelve (carpetas por id, archivos)."""
    carpetas: dict[int, dict] = {}
    archivos: list[dict] = []
    cursor = None
    for _ in range(200):                                 # tope de páginas
        params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
        d = pedir(url, clave, f"/models/{int(mid)}/content", params)
        lib = d.get("library") or {}
        for f in lib.get("folders") or []:
            if isinstance(f, dict) and isinstance(f.get("id"), int):
                carpetas[f["id"]] = f
        for a in lib.get("files") or []:
            if isinstance(a, dict) and isinstance(a.get("id"), int):
                archivos.append(a)
                if len(archivos) >= maximo:
                    return carpetas, archivos
        cursor = lib.get("nextCursor")
        if not cursor:
            break
    return carpetas, archivos


def bajar(url_pyro: str, clave: str, url_archivo: str, destino: Path) -> Path:
    """Baja un archivo de la biblioteca. La clave va solo si el archivo es del mismo host que Pyro."""
    u = urllib.parse.urlsplit(str(url_archivo or ""))
    if u.scheme not in ("https", "http") or not u.hostname or (u.scheme == "http" and not permitir_http()):
        raise ErrorPyro("Dirección de archivo inválida.")
    mismo = f"{u.scheme}://{u.netloc}" == url_pyro
    headers = {"Authorization": f"Bearer {clave}"} if mismo else {}
    tmp = destino.with_name(destino.name + ".bajando")
    try:
        with _cliente() as c, c.stream("GET", url_archivo, headers=headers,
                                       timeout=httpx.Timeout(120.0, connect=5.0)) as r:
            if r.status_code != 200:
                raise _error(r)
            if int(r.headers.get("content-length") or 0) > MAX_ARCHIVO:
                raise ErrorPyro("El archivo es demasiado grande.")
            total = 0
            with open(tmp, "wb") as fh:
                for parte in r.iter_bytes(1 << 16):
                    total += len(parte)
                    if total > MAX_ARCHIVO:
                        raise ErrorPyro("El archivo es demasiado grande.")
                    fh.write(parte)
        if total == 0:
            raise ErrorPyro("El archivo vino vacío.")
        tmp.replace(destino)
        return destino
    except httpx.HTTPError as ex:
        raise ErrorPyro(f"No se pudo bajar el archivo de Pyro ({type(ex).__name__}).") from None
    finally:
        tmp.unlink(missing_ok=True)
