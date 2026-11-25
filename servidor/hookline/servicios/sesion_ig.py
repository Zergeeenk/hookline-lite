"""Iniciar sesión en Instagram con usuario y contraseña de la cuenta «para mirar» (una secundaria, no la de las modelos),
en vez de exportar cookies con una extensión del navegador.

El servidor abre Instagram en el navegador del buscador (el del worker), completa el formulario y, si Instagram pide un
código (de doble factor o de «confirmá que sos vos»), lo pide en el panel y espera a que lo escriban. Con la sesión
iniciada guarda sus cookies donde las leen el buscador y las descargas (navegador/cookies.txt). Si la sesión vence,
se vuelve a iniciar sola (como mucho una vez cada AUTO_CADA horas, para no levantar sospechas en Instagram), salvo que
la última vez Instagram haya rechazado la contraseña o la cuenta, o haya pedido un código que nadie escribió: eso
necesita a una persona y no se reintenta solo hasta que alguien inicie sesión a mano.

El estado («iniciando», «pide_codigo»…) vale mientras haya un trabajo ig_login en la cola que lo respalde: si el worker
se reinicia en el medio, pasa solo a error y se puede volver a probar.

La contraseña se guarda cifrada (Fernet, como el App Secret de Meta) y nunca sale por la API ni a los logs (tampoco el
código: los errores de Playwright traen lo que se tipeó, por eso nunca se loguean enteros).
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta

from ..comun import ajuste, candado, guardar_ajuste
from ..trabajos import encolar, latido, manejador

log = logging.getLogger("hookline.sesion_ig")

URL_LOGIN = "https://www.instagram.com/accounts/login/"
CRED = "ig_login_cred"            # cifrado: {"usuario", "clave"}
ESTADO = "ig_login_estado"        # {estado, mensaje, cuando, auto, desde, ultimo_auto, bloqueado, espera_hasta}
CODIGO = "ig_login_codigo"        # cifrado: el código que escribieron en el panel
ESPERA_CODIGO = 300               # segundos que se espera el código
ESPERA_RESULTADO = 25             # segundos que se le dan a Instagram para contestar un código (valida por detrás)
SIN_RESPUESTA = 45                # segundos con el formulario quieto después de mandarlo: Instagram no contestó
TIEMPO_MAX = 480                  # segundos de todo el inicio de sesión
AUTO_CADA = 6 * 3600              # reintento automático como mucho cada 6 h
PAUSA_SI_FRENA = 180              # si Instagram pide esperar, no se vuelve a probar antes de esto
GRACIA = 60                       # un estado «iniciando» sin trabajo en la cola vale esto (lo que tarda en encolarse)
ACTIVOS = ("iniciando", "pide_codigo")
SESION_IG = re.compile(r"^(sessionid|ds_user_id|rur|shbid|shbts)$")   # la sesión; mid, ig_did, datr son el «dispositivo»

USUARIO = "input[name='username'], input[name='email'], input[autocomplete='username']"
CLAVE = "input[name='password'], input[type='password']"
CAMPO_CODIGO = ("input[name='verificationCode'], input[name='security_code'], input[autocomplete='one-time-code'], "
                "input[name*='code' i], input[aria-label*='code' i], input[aria-label*='código' i]")
BOTON_ENTRAR = re.compile(r"^(log in|iniciar sesión|entrar)$", re.I)
BOTON_ENVIAR_CODIGO = re.compile(r"send (security )?code|enviar (el )?c[oó]digo|get code|obtener c[oó]digo", re.I)
BOTON_CONFIRMAR = re.compile(r"^(confirm|confirmar|submit|enviar|next|siguiente|continue|continuar|verify|verificar)$", re.I)
BOTON_COOKIES = re.compile(r"^(allow all cookies|allow essential and optional cookies|accept all( cookies)?|accept|"
                           r"permitir todas las cookies|permitir cookies esenciales y opcionales|aceptar todas"
                           r"( las cookies)?|aceptar)$", re.I)
PIDE_APROBAR = re.compile(r"check your notifications|approve (this|the) login|was this you|"
                          r"revis[aá] tus notificaciones|aprob[aá] (el|este) inicio|¿fuiste tú\?", re.I)

M_CLAVE = "La contraseña no es correcta."
M_USUARIO = "Ese usuario no existe en Instagram. Revisá cómo está escrito."
M_ESPERAR = "Instagram pide esperar unos minutos antes de volver a intentar. Probá de nuevo en un rato."
M_CODIGO = "El código no es correcto. Iniciá sesión de nuevo y escribí el último código que te llegó."
M_SUSPENDIDA = "Instagram suspendió esa cuenta. Usá otra cuenta «para mirar»."
M_NO_DEJA = ("Instagram no dejó iniciar sesión desde el servidor en este momento. Probá de nuevo en un rato; si sigue, "
             "cargá la sesión con cookies (en «Avanzado»).")
M_SIN_CODIGO = "No llegó el código a tiempo. Iniciá sesión de nuevo cuando lo tengas a mano."
M_MUCHOS_CODIGOS = "Instagram no aceptó los códigos. Iniciá sesión de nuevo y escribí el último que te llegó."
M_OTRO_CODIGO = "El código no es correcto: escribí el último que te llegó."
M_APROBAR = ("Instagram pide aprobar el inicio de sesión desde la app (en el celular de la cuenta para mirar). "
             "Aprobalo y esperá acá.")
M_CORTADO = "El inicio de sesión se cortó (se reinició el servidor). Probá de nuevo."
M_EN_ESPERA = "En espera: arranca apenas termine la búsqueda que está corriendo."
M_CANCELADO = "Se canceló el inicio de sesión."
ERRORES = [
    (re.compile(r"password (was|is) incorrect|incorrect password|contraseña (no es correcta|es incorrecta)", re.I), M_CLAVE),
    (re.compile(r"doesn't belong to an account|no pertenece a ninguna cuenta|couldn.t find an account|"
                r"no encontramos ninguna cuenta", re.I), M_USUARIO),
    (re.compile(r"please wait a few minutes|espera unos minutos|try again later|vuelve a intentarlo más tarde", re.I),
     M_ESPERAR),
    (re.compile(r"(code|código)( de seguridad)? (isn.t valid|is not valid|no es válido|is incorrect|es incorrecto)|"
                r"invalid (security |confirmation |login )?code|check the (security |confirmation )?code|"
                r"(revisa|comprueba|verifica) el código", re.I), M_CODIGO),
    (re.compile(r"your account has been (disabled|suspended)|tu cuenta (fue|ha sido) (desactivada|suspendida)|"
                r"we suspended your account|suspendimos tu cuenta", re.I), M_SUSPENDIDA),
    (re.compile(r"(there was a|we had a) problem logging you in|problem logging you into instagram|"
                r"hubo un problema al iniciar (tu )?sesión|try again soon|inténtalo de nuevo (más tarde|pronto)", re.I),
     M_NO_DEJA),
]

_secretos: list[str] = []         # la contraseña y los códigos del intento en curso: se tachan de cualquier log
_avisado = {"cred": False}


class ErrorLogin(Exception):
    """Algo que se le explica a quien opera el panel (contraseña, código, Instagram pidió esperar…).

    tipo: «definitivo» (contraseña, usuario o cuenta: no se reintenta solo), «codigo» (hace falta una persona que lo
    escriba), «freno» (Instagram pidió esperar), «cancelado» (lo cancelaron desde el panel) o «pasajero»."""

    def __init__(self, mensaje: str, tipo: str = "pasajero"):
        super().__init__(mensaje)
        self.tipo = tipo


def _tipo(msg: str) -> str:
    if msg in (M_CLAVE, M_USUARIO, M_SUSPENDIDA):
        return "definitivo"
    if msg == M_CODIGO:
        return "codigo"
    if msg in (M_ESPERAR, M_NO_DEJA):
        return "freno"
    return "pasajero"


# --- credenciales y estado ---------------------------------------------------------------------------------------------------
def _usuario_ok(u: str) -> str:
    u = str(u or "").strip().lstrip("@")
    if not re.fullmatch(r"[\w.@+-]{1,80}", u):
        raise ValueError("Escribí el usuario de Instagram (o el mail o el teléfono) de la cuenta «para mirar».")
    return u


def guardar_credenciales(usuario: str, clave: str = "") -> None:
    """Usuario y contraseña. Con la contraseña vacía, el usuario (corregido) con la contraseña que ya estaba guardada."""
    usuario = _usuario_ok(usuario)
    clave = str(clave or "")
    if not clave:
        cred = credenciales()
        if not cred:
            raise ValueError("Escribí la contraseña de esa cuenta.")
        clave = cred[1]
    if not 6 <= len(clave) <= 200 or "\n" in clave:
        raise ValueError("Escribí la contraseña de esa cuenta.")
    with candado("ig_login"):
        guardar_ajuste(CRED, json.dumps({"usuario": usuario, "clave": clave}), cifrado=True)
        estado(bloqueado=False)          # credenciales nuevas: el reintento automático vuelve a valer


def preparar(usuario, clave) -> None:
    """Lo que manda el panel al tocar «Iniciar sesión»: usuario y contraseña nuevos, solo el usuario corregido (con la
    contraseña guardada) o nada (lo guardado)."""
    if not isinstance(usuario, (str, type(None))) or not isinstance(clave, (str, type(None))):
        raise ValueError("Escribí el usuario y la contraseña de la cuenta «para mirar».")
    usuario, clave = (usuario or "").strip(), clave or ""
    cred = credenciales()
    if clave or (usuario and (not cred or _usuario_ok(usuario) != cred[0])):
        guardar_credenciales(usuario, clave)


def credenciales() -> tuple[str, str] | None:
    try:
        d = json.loads(ajuste(CRED, "") or "{}")
    except Exception as ex:              # también si cambió HOOKLINE_CLAVE_SECRETA: que las vuelvan a cargar
        if not _avisado["cred"]:
            _avisado["cred"] = True
            log.warning("no pude leer el usuario y la contraseña guardados de Instagram (%s): hay que cargarlos de "
                        "nuevo", type(ex).__name__)
        return None
    if isinstance(d, dict) and d.get("usuario") and d.get("clave"):
        return str(d["usuario"]), str(d["clave"])
    return None


def estado(**cambios) -> dict:
    e = dict(ajuste(ESTADO, {}) or {})
    if cambios:
        e.update(cambios)
        e["cuando"] = time.time()
        guardar_ajuste(ESTADO, e)
    return e or {"estado": "parado"}


def _trabajo_activo() -> str:
    """«corriendo», «pendiente» o «» según los ig_login de la cola. Uno «corriendo» cuya toma venció es de un worker
    que se murió (un deploy, un reinicio): ya no cuenta."""
    from sqlalchemy import func, select

    from ..db import ahora, sesion
    from ..modelos import Trabajo
    with sesion() as s:
        base = select(func.count()).select_from(Trabajo).where(Trabajo.tipo == "ig_login")
        if s.scalar(base.where(Trabajo.estado == "corriendo", Trabajo.tomado_hasta >= ahora())):
            return "corriendo"
        if s.scalar(base.where(Trabajo.estado == "pendiente")):
            return "pendiente"
    return ""


def _cancelar_pendientes() -> None:
    from sqlalchemy import update

    from ..db import ahora, sesion
    from ..modelos import Trabajo
    with sesion() as s:
        s.execute(update(Trabajo).where(Trabajo.tipo == "ig_login", Trabajo.estado == "pendiente")
                  .values(estado="hecho", terminado=ahora(), error="Cancelado desde el panel."))


def _vigente() -> dict:
    """El estado guardado, salvo que diga «iniciando» o «pide_codigo» sin un trabajo que lo respalde (el worker se
    reinició en el medio): eso pasa a error y se guarda, así lo ven igual el panel, iniciar() y el reintento solo.
    Agrega «_trabajo» (corriendo/pendiente) mientras hay uno."""
    e = estado()
    if e.get("estado") not in ACTIVOS:
        return e
    t = _trabajo_activo()
    if t:
        return {**e, "_trabajo": t}
    if time.time() - float(e.get("cuando") or 0) <= GRACIA:
        return e
    with candado("ig_login"):
        e = estado()
        if e.get("estado") in ACTIVOS and not _trabajo_activo() and time.time() - float(e.get("cuando") or 0) > GRACIA:
            e = estado(estado="error", mensaje=M_CORTADO)
    return e


def publico() -> dict:
    """Lo que ve el panel: nunca la contraseña."""
    e = _vigente()
    cred = credenciales()
    est = e.get("estado") or "parado"
    msg = str(e.get("mensaje") or "")
    if est == "iniciando" and e.get("_trabajo") == "pendiente":
        msg = M_EN_ESPERA
    return {"estado": est, "mensaje": msg, "usuario": cred[0] if cred else "", "tiene_credenciales": bool(cred),
            "auto": bool(e.get("auto"))}


def olvidar() -> None:
    with candado("ig_login"):
        guardar_ajuste(CRED, None)
        guardar_ajuste(CODIGO, None)
        _cancelar_pendientes()
        estado(estado="parado", mensaje="", auto=False, bloqueado=False, espera_hasta=0)


def cancelar() -> dict:
    """Desde el panel (por ejemplo, el código no llega y quieren pedir otro): el intento en curso termina en cuanto lo
    nota y se puede volver a iniciar."""
    with candado("ig_login"):
        guardar_ajuste(CODIGO, None)
        _cancelar_pendientes()
        if estado().get("estado") in ACTIVOS:
            estado(estado="parado", mensaje=M_CANCELADO)
    return publico()


def iniciar(auto: bool = False) -> dict:
    """Encola el inicio de sesión (corre en el worker, con el navegador del buscador; pasa adelante de las búsquedas
    en cola, aunque espera a que termine la que está corriendo)."""
    from ..db import ahora
    if not credenciales():
        raise ValueError("Primero cargá el usuario y la contraseña de la cuenta «para mirar».")
    with candado("ig_login"):
        e = _vigente()
        if e.get("estado") in ACTIVOS or _trabajo_activo() == "corriendo":
            raise ValueError("Ya está iniciando sesión: esperá un momento.")
        falta = float(e.get("espera_hasta") or 0) - time.time()
        if falta > 0:
            minutos = max(1, round(falta / 60))
            raise ValueError(f"Instagram pidió esperar: probá de nuevo en {minutos} minuto{'s' if minutos > 1 else ''}.")
        guardar_ajuste(CODIGO, None)
        cambios = {"estado": "iniciando", "mensaje": "Iniciando sesión en Instagram…", "auto": bool(auto),
                   "desde": time.time()}
        if not auto:
            cambios["bloqueado"] = False     # alguien lo pidió a mano: el reintento automático vuelve a valer
        estado(**cambios)
        if _trabajo_activo() == "pendiente":
            return publico()                 # ya hay uno en la cola (lee lo guardado al arrancar): ese alcanza
        tid = encolar("ig_login", {"auto": bool(auto)}, clave=f"ig_login:{time.time_ns()}", max_intentos=1,
                      cuando=ahora() - timedelta(minutes=30))
        if not tid:
            estado(estado="error", mensaje="No pude poner en cola el inicio de sesión. Probá de nuevo.")
    return publico()


def dar_codigo(codigo: str) -> dict:
    c = re.sub(r"\D", "", str(codigo or ""))
    if not 4 <= len(c) <= 8:
        raise ValueError("El código son los números que te mandó Instagram (6 u 8 dígitos).")
    with candado("ig_login"):
        if _vigente().get("estado") != "pide_codigo":
            raise ValueError("Instagram no está pidiendo un código ahora.")
        guardar_ajuste(CODIGO, c, cifrado=True)
        estado(estado="iniciando", mensaje="Probando el código…")
    return publico()


def reintentar_solo() -> bool:
    """Cuando la sesión vence (lo detectan el buscador o una descarga): si hay usuario y contraseña, se vuelve a
    iniciar sola, como mucho una vez cada AUTO_CADA. No si la última vez Instagram rechazó la contraseña o la cuenta,
    o pidió un código que nadie escribió (eso lo destraba alguien iniciando sesión a mano)."""
    if not credenciales():
        return False
    with candado("ig_login"):
        e = _vigente()
        if (e.get("estado") in ACTIVOS or e.get("bloqueado") or _trabajo_activo()
                or time.time() - float(e.get("ultimo_auto") or 0) < AUTO_CADA
                or float(e.get("espera_hasta") or 0) > time.time()):
            return False
        estado(ultimo_auto=time.time())
        try:
            iniciar(auto=True)
            return True
        except ValueError:
            return False


# --- el inicio de sesión (worker) --------------------------------------------------------------------------------------------
def _base() -> str:
    return URL_LOGIN.split("/accounts/")[0] + "/"


def _en_paso(url: str) -> bool:
    """Una pantalla de seguridad de Instagram (código, «confirmá que sos vos»): todavía no terminó."""
    return bool(re.search(r"/challenge|two_factor|/auth_platform", str(url or "")))


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
    """El cartel de cookies de Instagram (UE y Reino Unido) aparece cuando quiere y tapa el formulario."""
    b = _boton(pg, BOTON_COOKIES)
    if b:
        try:
            b.click(timeout=3000)
            pg.wait_for_timeout(800)
        except Exception:
            pass


def _escribir(el, valor: str, que: str) -> None:
    """fill() sin que lo tipeado termine en un log: el error de Playwright trae el valor tal cual."""
    try:
        if el is None:
            raise LookupError
        el.fill(valor, timeout=10000)
    except Exception:
        raise ErrorLogin(f"No pude escribir {que} en el formulario de Instagram. Probá de nuevo.") from None


def _apretar(pg, boton, campo) -> None:
    """Clic en el botón; si algo lo tapa (un cartel), sacarlo y probar de nuevo; si no, Enter en el campo (el
    teclado no necesita que el botón se vea)."""
    for _ in range(2):
        if boton is None:
            break
        try:
            boton.click(timeout=8000)
            return
        except Exception:
            _aceptar_cookies(pg)
    try:
        campo.press("Enter", timeout=5000)
    except Exception:
        try:
            pg.keyboard.press("Enter")
        except Exception:
            pass


def _avance(**cambios) -> None:
    """Un paso del intento en curso, salvo que lo hayan cancelado desde el panel (el estado ya no está activo)."""
    with candado("ig_login"):
        if estado().get("estado") not in ACTIVOS:
            raise ErrorLogin(M_CANCELADO, "cancelado")
        estado(**cambios)


def _leer_codigo() -> str | None:
    try:
        c = ajuste(CODIGO, None)
    except Exception:
        return None
    if c:
        guardar_ajuste(CODIGO, None)
        return str(c)
    return None


def _esperar_codigo(pg, ctx, hasta: float) -> str | None:
    """El código que escriban en el panel. None si no llegó a tiempo o si la sesión se inició igual (por ejemplo,
    aprobaron el inicio desde la app de Instagram). ErrorLogin «cancelado» si lo cancelaron desde el panel."""
    fin = min(hasta, time.time() + ESPERA_CODIGO)
    while time.time() < fin:
        c = _leer_codigo()
        if c:
            return c
        if estado().get("estado") != "pide_codigo":
            c = _leer_codigo()             # dar_codigo() guarda el código antes de cambiar el estado
            if c:
                return c
            raise ErrorLogin(M_CANCELADO, "cancelado")
        if _tiene_sesion(ctx) and not _en_paso(pg.url):
            return None
        latido()
        pg.wait_for_timeout(1500)
    return None


def _esperar_resultado(pg, ctx, navs: list, antes: int, error_antes: str | None) -> None:
    """Después de mandar el código, hasta que Instagram conteste: navega, inicia la sesión, saca el campo o muestra un
    error nuevo. Como mucho ESPERA_RESULTADO: Instagram valida por detrás y desde un servidor a veces tarda."""
    fin = time.time() + ESPERA_RESULTADO
    pg.wait_for_timeout(800)
    while time.time() < fin:
        latido()
        if navs[0] != antes or not _visible(pg, CAMPO_CODIGO) or (_tiene_sesion(ctx) and not _en_paso(pg.url)):
            break
        if error_antes is None and _error(_texto(pg)):
            break
        pg.wait_for_timeout(700)
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass


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


def _limpio(texto: str) -> str:
    """Para un log: la primera línea (el «call log» de Playwright, que trae lo tipeado, viene después) y sin la
    contraseña ni los códigos."""
    t = (str(texto or "").splitlines() or [""])[0][:300]
    for s in _secretos:
        if s:
            t = t.replace(s, "•••")
    return t


def _borrar_sesion_vieja(ctx) -> None:
    """Saca la sesión vieja de Instagram del navegador pero deja las cookies que lo identifican como el mismo
    dispositivo (mid, ig_did, datr, csrftoken) y las de las otras redes: un dispositivo nuevo en cada inicio de sesión
    es lo que más hace que Instagram pida códigos."""
    try:
        ctx.clear_cookies(name=SESION_IG, domain=re.compile("instagram"))
    except Exception as ex:
        log.info("no pude sacar la sesión vieja de Instagram del navegador: %s", type(ex).__name__)


def _guardar_cookies(ctx) -> int:
    from .buscador import _netscape, importar_cookies
    cs = [{"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path") or "/",
           "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
           "expires": max(0, int(c.get("expires") or 0))}
          for c in ctx.cookies() if "instagram" in str(c.get("domain"))]
    return importar_cookies(_netscape(cs))


def _verificar(pg, ctx) -> int | None:
    """Con la sesión iniciada, abrir el inicio de Instagram y ver adónde manda. A «cuenta suspendida» (Instagram deja
    entrar a esas cuentas) no sirve para mirar; si pide confirmar algo, que siga el bucle (None). «¿Guardar tu
    información?» (/accounts/onetap) es lo normal y está bien."""
    pg.wait_for_timeout(1500)                    # que termine de guardar las cookies
    try:
        pg.goto(_base(), wait_until="domcontentloaded", timeout=40000)
        pg.wait_for_timeout(1500)
    except Exception:
        pass
    url = pg.url
    if re.search(r"/accounts/(suspended|disabled)", url):
        raise ErrorLogin(M_SUSPENDIDA, "definitivo")
    if _en_paso(url):
        return None
    if "/accounts/login" in url or not _tiene_sesion(ctx):
        raise ErrorLogin(M_NO_DEJA, "freno")
    return _guardar_cookies(ctx)


def hacer_login(pg, ctx, usuario: str, clave: str) -> int:
    """Completa el inicio de sesión en la página `pg`. Devuelve cuántas cookies guardó; ErrorLogin si no se pudo."""
    hasta = time.time() + TIEMPO_MAX
    navs = [0]                                   # navegaciones de la página (también las de una app de una página)
    pg.on("framenavigated", lambda fr: fr == pg.main_frame and navs.__setitem__(0, navs[0] + 1))
    try:
        pg.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=40000)
    except Exception as ex:
        raise ErrorLogin(f"No pude abrir Instagram desde el servidor ({type(ex).__name__}). Probá de nuevo en un "
                         "rato.") from None
    fin = time.time() + 20                       # el primer campo VISIBLE (puede haber otro escondido antes)
    while not (campo_usuario := _visible(pg, USUARIO)):
        if time.time() > fin or (_tiene_sesion(ctx) and not _en_paso(pg.url)):
            break
        latido()
        _aceptar_cookies(pg)
        pg.wait_for_timeout(400)
    if not campo_usuario:
        if _tiene_sesion(ctx) and not _en_paso(pg.url):
            n = _verificar(pg, ctx)
            if n is not None:
                return n
        texto = _texto(pg)
        raise ErrorLogin("No apareció el formulario de Instagram. " + (_error(texto) or texto[:160]))
    _aceptar_cookies(pg)
    _escribir(campo_usuario, usuario, "el usuario")
    campo_clave = _visible(pg, CLAVE)
    _escribir(campo_clave, clave, "la contraseña")
    _aceptar_cookies(pg)
    _apretar(pg, _visible(pg, "button[type='submit']") or _boton(pg, BOTON_ENTRAR), campo_clave)
    enviado, codigos, sesion_desde, aprobar = time.time(), 0, 0.0, False
    while time.time() < hasta:
        latido()
        pg.wait_for_timeout(1500)
        _aceptar_cookies(pg)
        if _tiene_sesion(ctx):
            if not _en_paso(pg.url):
                n = _verificar(pg, ctx)
                if n is not None:
                    return n
                continue
            sesion_desde = sesion_desde or time.time()
            if (time.time() - sesion_desde > ESPERA_RESULTADO and not _visible(pg, CAMPO_CODIGO)
                    and not _boton(pg, BOTON_ENVIAR_CODIGO)):
                sesion_desde = time.time()       # hay sesión pero la página quedó en el paso de seguridad: al inicio
                n = _verificar(pg, ctx)
                if n is not None:
                    return n
                continue
        else:
            sesion_desde = 0.0
        texto = _texto(pg)
        err = _error(texto)
        campo = _visible(pg, CAMPO_CODIGO)
        if campo:
            if err and err != M_CODIGO:              # p. ej. «esperá unos minutos» con el campo a la vista
                raise ErrorLogin(err, _tipo(err))
            if codigos >= 3:
                raise ErrorLogin(M_MUCHOS_CODIGOS, "codigo")
            otra_vez = bool(err) and codigos > 0
            codigos += 1
            _avance(estado="pide_codigo",
                    mensaje=M_OTRO_CODIGO if otra_vez else _pista(texto) or "Instagram pide el código que te mandó.")
            codigo = _esperar_codigo(pg, ctx, hasta)
            if not codigo:
                if _tiene_sesion(ctx) and not _en_paso(pg.url):
                    continue                     # la aprobaron desde la app: no hacía falta el código
                raise ErrorLogin(M_SIN_CODIGO, "codigo")
            _secretos.append(codigo)
            _avance(estado="iniciando", mensaje="Probando el código…")
            campo = _visible(pg, CAMPO_CODIGO)   # nunca el de antes: la página pudo cambiar mientras esperaba
            if not campo:
                if _tiene_sesion(ctx):
                    continue
                raise ErrorLogin("La página de Instagram cambió mientras esperaba el código. Iniciá sesión de nuevo.")
            antes = navs[0]
            _escribir(campo, codigo, "el código")
            _apretar(pg, _visible(pg, "button[type='submit']") or _boton(pg, BOTON_CONFIRMAR), campo)
            _esperar_resultado(pg, ctx, navs, antes, err)
            continue
        if err:
            raise ErrorLogin(err, _tipo(err))
        enviar = _boton(pg, BOTON_ENVIAR_CODIGO)
        if enviar:                                  # «confirmá que sos vos»: que mande el código (al mail o al teléfono)
            try:
                enviar.click(timeout=8000)
            except Exception:
                _aceptar_cookies(pg)
            pg.wait_for_timeout(2000)
            continue
        if not aprobar and PIDE_APROBAR.search(texto):   # «aprobalo desde la app»: no hay nada que escribir, solo esperar
            aprobar = True
            _avance(estado="iniciando", mensaje=M_APROBAR)
            continue
        if (not codigos and "/accounts/login" in pg.url and _visible(pg, CLAVE)
                and time.time() - enviado > SIN_RESPUESTA):
            raise ErrorLogin("Instagram no contestó al iniciar sesión. Probá de nuevo en un rato.")
    raise ErrorLogin("Instagram no terminó el inicio de sesión. " + (_texto(pg)[:160] or ""))


def _final(**cambios) -> None:
    """El resultado del intento, salvo que lo hayan cancelado desde el panel (el estado ya no está activo)."""
    with candado("ig_login"):
        if estado().get("estado") in ACTIVOS:
            estado(**cambios)


@manejador("ig_login")
def _login(datos: dict) -> None:
    e = estado()
    auto = bool(e.get("auto")) if e.get("estado") in ACTIVOS else bool((datos or {}).get("auto"))
    cred = credenciales()
    if not cred:
        _final(estado="error", mensaje="Faltan el usuario y la contraseña de la cuenta «para mirar».")
        return
    if e.get("estado") not in ACTIVOS and e.get("mensaje") == M_CANCELADO:
        return                                   # lo cancelaron mientras esperaba en la cola
    _secretos[:] = [cred[1]]
    estado(estado="iniciando", mensaje="Iniciando sesión en Instagram…", auto=auto, desde=time.time())
    try:
        from .buscador import _contexto, _playwright, abrir
        sync_playwright = _playwright()
        abrir()
        with sync_playwright() as p:
            _, ctx = _contexto(p)
            _borrar_sesion_vieja(ctx)
            pg = ctx.new_page()
            try:
                n = hacer_login(pg, ctx, *cred)
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
        _final(estado="listo", mensaje=f"Sesión iniciada como @{cred[0]} ({n} cookies).", bloqueado=False,
               espera_hasta=0)
        log.info("sesión de Instagram iniciada (%s cookies)", n)
    except ErrorLogin as ex:
        if ex.tipo == "cancelado":
            return
        cambios = {"estado": "error", "mensaje": str(ex)}
        if ex.tipo == "definitivo" or (auto and ex.tipo == "codigo"):
            cambios["bloqueado"] = True      # hace falta una persona: no se reintenta solo
        if ex.tipo == "freno":
            cambios["espera_hasta"] = time.time() + PAUSA_SI_FRENA
        _final(**cambios)
        log.info("inicio de sesión de Instagram: %s", _limpio(str(ex)))
    except ValueError as ex:
        _final(estado="error", mensaje=_limpio(str(ex)))
    except Exception as ex:
        log.warning("inicio de sesión de Instagram: %s: %s", type(ex).__name__, _limpio(str(ex)))
        _final(estado="error", mensaje=f"No se pudo iniciar sesión ({type(ex).__name__}). Probá de nuevo.")
    finally:
        _secretos.clear()
