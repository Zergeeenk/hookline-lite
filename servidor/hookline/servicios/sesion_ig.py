"""Iniciar sesión en Instagram con usuario y contraseña de la cuenta «para mirar» (una secundaria, no la de las modelos),
en vez de exportar cookies con una extensión del navegador.

El servidor abre Instagram en el navegador del buscador (el del worker), completa el formulario y, si Instagram pide un
código (de doble factor o de «confirmá que sos vos»), lo pide en el panel y espera a que lo escriban. Con la sesión
iniciada guarda sus cookies donde las leen el buscador y las descargas (navegador/cookies.txt). Si la sesión vence,
se vuelve a iniciar sola (como mucho una vez cada AUTO_CADA horas, para no levantar sospechas en Instagram).

La contraseña se guarda cifrada (Fernet, como el App Secret de Meta) y nunca sale por la API ni a los logs.
"""
from __future__ import annotations

import json
import logging
import re
import time

from ..comun import ajuste, candado, guardar_ajuste
from ..trabajos import encolar, latido, manejador

log = logging.getLogger("hookline.sesion_ig")

URL_LOGIN = "https://www.instagram.com/accounts/login/"
CRED = "ig_login_cred"            # cifrado: {"usuario", "clave"}
ESTADO = "ig_login_estado"        # {"estado", "mensaje", "cuando", "auto", "desde"}
CODIGO = "ig_login_codigo"        # cifrado: el código que escribieron en el panel
ESPERA_CODIGO = 300               # segundos que se espera el código
TIEMPO_MAX = 480                  # segundos de todo el inicio de sesión
AUTO_CADA = 6 * 3600              # reintento automático como mucho cada 6 h
ACTIVOS = ("iniciando", "pide_codigo")

USUARIO = ("input[name='username'], input[name='email'], input[autocomplete='username']")
CLAVE = "input[name='password'], input[type='password']"
CAMPO_CODIGO = ("input[name='verificationCode'], input[name='security_code'], input[autocomplete='one-time-code'], "
                "input[name*='code' i], input[aria-label*='code' i], input[aria-label*='código' i]")
BOTON_ENVIAR_CODIGO = re.compile(r"send (security )?code|enviar (el )?c[oó]digo|get code|obtener c[oó]digo", re.I)
BOTON_CONFIRMAR = re.compile(r"^(confirm|confirmar|submit|enviar|next|siguiente|continue|continuar|verify|verificar)$", re.I)
ERRORES = [
    (re.compile(r"password (was|is) incorrect|incorrect password|contraseña (no es correcta|es incorrecta)", re.I),
     "La contraseña no es correcta."),
    (re.compile(r"doesn't belong to an account|no pertenece a ninguna cuenta|couldn.t find an account|"
                r"no encontramos ninguna cuenta", re.I),
     "Ese usuario no existe en Instagram. Revisá cómo está escrito."),
    (re.compile(r"please wait a few minutes|espera unos minutos|try again later|vuelve a intentarlo más tarde", re.I),
     "Instagram pide esperar unos minutos antes de volver a intentar. Probá de nuevo en un rato."),
    (re.compile(r"(code|código) (isn.t valid|no es válido|is incorrect|es incorrecto)|"
                r"invalid (security|confirmation) code|check the code", re.I),
     "El código no es correcto. Iniciá sesión de nuevo y escribí el último código que te llegó."),
    (re.compile(r"your account has been (disabled|suspended)|tu cuenta (fue|ha sido) (desactivada|suspendida)", re.I),
     "Instagram suspendió esa cuenta. Usá otra cuenta «para mirar»."),
    (re.compile(r"(there was a|we had a) problem logging you in|problem logging you into instagram|"
                r"hubo un problema al iniciar (tu )?sesión|try again soon|inténtalo de nuevo (más tarde|pronto)", re.I),
     "Instagram no dejó iniciar sesión desde el servidor en este momento. Probá de nuevo en un rato; si sigue, "
     "cargá la sesión con cookies (en «Avanzado»)."),
]


class ErrorLogin(Exception):
    """Algo que se le explica a quien opera el panel (contraseña, código, Instagram pidió esperar…)."""


# --- credenciales y estado ---------------------------------------------------------------------------------------------------
def _usuario_ok(u: str) -> str:
    u = str(u or "").strip().lstrip("@")
    if not re.fullmatch(r"[\w.@+-]{1,80}", u):
        raise ValueError("Escribí el usuario de Instagram (o el mail o el teléfono) de la cuenta «para mirar».")
    return u


def guardar_credenciales(usuario: str, clave: str) -> None:
    usuario = _usuario_ok(usuario)
    clave = str(clave or "")
    if not 6 <= len(clave) <= 200 or "\n" in clave:
        raise ValueError("Escribí la contraseña de esa cuenta.")
    guardar_ajuste(CRED, json.dumps({"usuario": usuario, "clave": clave}), cifrado=True)


def credenciales() -> tuple[str, str] | None:
    try:
        d = json.loads(ajuste(CRED, "") or "{}")
    except (ValueError, TypeError):
        return None
    if d.get("usuario") and d.get("clave"):
        return str(d["usuario"]), str(d["clave"])
    return None


def estado(**cambios) -> dict:
    e = dict(ajuste(ESTADO, {}) or {})
    if cambios:
        e.update(cambios)
        e["cuando"] = time.time()
        guardar_ajuste(ESTADO, e)
    return e or {"estado": "parado"}


def publico() -> dict:
    """Lo que ve el panel: nunca la contraseña."""
    e = estado()
    cred = credenciales()
    est = e.get("estado") or "parado"
    if est in ACTIVOS and time.time() - float(e.get("cuando") or 0) > TIEMPO_MAX + 120:
        est = "error"                    # el trabajo se cortó (reinicio del worker): que se pueda volver a probar
        e["mensaje"] = "El inicio de sesión se cortó. Probá de nuevo."
    return {"estado": est, "mensaje": str(e.get("mensaje") or ""), "usuario": cred[0] if cred else "",
            "tiene_credenciales": bool(cred), "auto": bool(e.get("auto"))}


def olvidar() -> None:
    guardar_ajuste(CRED, None)
    guardar_ajuste(CODIGO, None)
    estado(estado="parado", mensaje="", auto=False)


def iniciar(auto: bool = False) -> dict:
    """Encola el inicio de sesión (corre en el worker, con el navegador del buscador)."""
    if not credenciales():
        raise ValueError("Primero cargá el usuario y la contraseña de la cuenta «para mirar».")
    with candado("ig_login"):
        if publico()["estado"] in ACTIVOS:
            raise ValueError("Ya está iniciando sesión: esperá un momento.")
        guardar_ajuste(CODIGO, None)
        estado(estado="iniciando", mensaje="Iniciando sesión en Instagram…", auto=bool(auto), desde=time.time())
    encolar("ig_login", {"auto": bool(auto)}, clave=f"ig_login:{time.time():.0f}", max_intentos=1)
    return publico()


def dar_codigo(codigo: str) -> dict:
    c = re.sub(r"\D", "", str(codigo or ""))
    if not 4 <= len(c) <= 8:
        raise ValueError("El código son los números que te mandó Instagram (6 u 8 dígitos).")
    if publico()["estado"] != "pide_codigo":
        raise ValueError("Instagram no está pidiendo un código ahora.")
    guardar_ajuste(CODIGO, c, cifrado=True)
    estado(estado="iniciando", mensaje="Probando el código…")
    return publico()


def reintentar_solo() -> bool:
    """Cuando la sesión vence (lo detectan el buscador o una descarga): si hay usuario y contraseña, se vuelve a
    iniciar sola, como mucho una vez cada AUTO_CADA."""
    if not credenciales():
        return False
    e = estado()
    if e.get("estado") in ACTIVOS or time.time() - float(e.get("ultimo_auto") or 0) < AUTO_CADA:
        return False
    estado(ultimo_auto=time.time())
    try:
        iniciar(auto=True)
        return True
    except ValueError:
        return False


# --- el inicio de sesión (worker) --------------------------------------------------------------------------------------------
def _tiene_sesion(ctx) -> bool:
    try:
        return any(c.get("name") == "sessionid" and c.get("value") and "instagram" in str(c.get("domain"))
                   for c in ctx.cookies())
    except Exception:
        return False


def _texto(pg) -> str:
    try:
        return " ".join((pg.inner_text("body", timeout=3000) or "").split())
    except Exception:
        return ""


def _visible(pg, sel: str):
    try:
        for el in pg.query_selector_all(sel):
            if el.is_visible():
                return el
    except Exception:
        pass
    return None


def _boton(pg, rx: re.Pattern):
    """Un botón visible cuyo texto coincide (Instagram usa <button> y también <div role=button>)."""
    try:
        for el in pg.query_selector_all("button, [role='button'], input[type='submit']"):
            t = " ".join((el.inner_text() or el.get_attribute("value") or "").split())
            if t and rx.search(t) and el.is_visible():
                return el
    except Exception:
        pass
    return None


def _aceptar_cookies(pg) -> None:
    b = _boton(pg, re.compile(r"allow (all|essential)|permitir (todas|las cookies)|aceptar|accept", re.I))
    if b:
        try:
            b.click(timeout=3000)
            pg.wait_for_timeout(800)
        except Exception:
            pass


def _esperar_codigo(hasta: float) -> str | None:
    fin = min(hasta, time.time() + ESPERA_CODIGO)
    while time.time() < fin:
        c = ajuste(CODIGO, None)
        if c:
            guardar_ajuste(CODIGO, None)
            return str(c)
        latido()
        time.sleep(1.5)
    return None


def _pista(texto: str) -> str:
    """La frase de Instagram que dice adónde mandó el código (para mostrarla en el panel)."""
    for frase in re.split(r"(?<=[.!?])\s+", texto):          # un mail (f***@gmail.com) no corta la frase
        if re.search(r"sent|enviamos|enviado|code|código", frase, re.I):
            return frase.strip()[:240]
    return ""


def _error(texto: str) -> str | None:
    for rx, msg in ERRORES:
        if rx.search(texto):
            return msg
    return None


def _guardar_cookies(ctx) -> int:
    from .buscador import _netscape, importar_cookies
    cs = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path") or "/",
           "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
           "expires": max(0, int(c.get("expires") or 0))}
          for c in ctx.cookies() if "instagram" in str(c.get("domain"))]
    return importar_cookies(_netscape(cs))


def hacer_login(pg, ctx, usuario: str, clave: str) -> int:
    """Completa el inicio de sesión en la página `pg`. Devuelve cuántas cookies guardó; ErrorLogin si no se pudo."""
    hasta = time.time() + TIEMPO_MAX
    pg.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=40000)
    _aceptar_cookies(pg)
    try:
        pg.wait_for_selector(USUARIO, timeout=20000)
    except Exception:
        if _tiene_sesion(ctx):
            return _guardar_cookies(ctx)
        raise ErrorLogin("No apareció el formulario de Instagram. " + (_error(_texto(pg)) or _texto(pg)[:160]))
    _visible(pg, USUARIO).fill(usuario)
    _visible(pg, CLAVE).fill(clave)
    boton = _visible(pg, "button[type='submit']") or _boton(pg, re.compile(r"^(log in|iniciar sesión|entrar)$", re.I))
    if boton:
        boton.click()
    else:
        pg.keyboard.press("Enter")
    codigos = 0
    while time.time() < hasta:
        latido()
        pg.wait_for_timeout(1500)
        if _tiene_sesion(ctx) and "/challenge" not in pg.url and "two_factor" not in pg.url:
            pg.wait_for_timeout(1500)              # que termine de guardar las cookies
            return _guardar_cookies(ctx)
        texto = _texto(pg)
        err = _error(texto)
        campo = _visible(pg, CAMPO_CODIGO)
        if err and not (campo and codigos == 0):
            raise ErrorLogin(err)
        if campo:
            codigos += 1
            if codigos > 3:
                raise ErrorLogin("Instagram siguió pidiendo códigos. Probá de nuevo más tarde.")
            estado(estado="pide_codigo", mensaje=_pista(texto) or "Instagram pide el código que te mandó.")
            codigo = _esperar_codigo(hasta)
            if not codigo:
                raise ErrorLogin("No llegó el código a tiempo. Iniciá sesión de nuevo cuando lo tengas a mano.")
            estado(estado="iniciando", mensaje="Probando el código…")
            campo = _visible(pg, CAMPO_CODIGO) or campo
            campo.fill(codigo)
            ok = _visible(pg, "button[type='submit']") or _boton(pg, BOTON_CONFIRMAR)
            if ok:
                ok.click()
            else:
                campo.press("Enter")
            pg.wait_for_timeout(2500)
            continue
        enviar = _boton(pg, BOTON_ENVIAR_CODIGO)
        if enviar:                                  # «confirmá que sos vos»: que mande el código (al mail o al teléfono)
            enviar.click()
            pg.wait_for_timeout(2000)
            continue
    raise ErrorLogin("Instagram no terminó el inicio de sesión. " + (_texto(pg)[:160] or ""))


@manejador("ig_login")
def _login(datos: dict) -> None:
    cred = credenciales()
    if not cred:
        estado(estado="error", mensaje="Faltan el usuario y la contraseña.")
        return
    try:
        from .buscador import _contexto, _playwright, abrir
        sync_playwright = _playwright()
        abrir()
        with sync_playwright() as p:
            _, ctx = _contexto(p)
            try:
                ctx.clear_cookies()              # nada de una sesión vieja o vencida
            except Exception:
                pass
            pg = ctx.new_page()
            try:
                n = hacer_login(pg, ctx, *cred)
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
        estado(estado="listo", mensaje=f"Sesión iniciada como @{cred[0]} ({n} cookies).")
        log.info("sesión de Instagram iniciada (%s cookies)", n)
    except ErrorLogin as ex:
        estado(estado="error", mensaje=str(ex))
    except ValueError as ex:
        estado(estado="error", mensaje=str(ex))
    except Exception as ex:
        log.warning("inicio de sesión de Instagram: %s", ex)
        estado(estado="error", mensaje=f"No se pudo iniciar sesión ({type(ex).__name__}). Probá de nuevo.")
