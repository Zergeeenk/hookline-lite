"""Cifrado de secretos, contraseñas, sesiones, tokens de API y firmas de URL.

- Tokens de Instagram y App Secret: cifrados con Fernet (HOOKLINE_CLAVE_SECRETA). Nunca en claro en la base.
- Contraseñas: scrypt (stdlib), con sal por usuario.
- Sesión: cookie HttpOnly con un token al azar; en la base solo su sha256. CSRF: token por sesión que el panel
  manda en el header X-CSRF-Token en cada POST (el panel viejo no tenía nada de esto: ver REQUERIMIENTOS §8).
- Tokens de API (para Pyro): "hl_<al azar>", se guarda solo el sha256.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from .config import config


class ErrorSecreto(RuntimeError):
    pass


def _fernet() -> Fernet:
    clave = config().clave_secreta
    if not clave:
        raise ErrorSecreto("Falta HOOKLINE_CLAVE_SECRETA (generala con `hookline generar-clave`).")
    try:
        return Fernet(clave.encode())
    except ValueError as ex:
        raise ErrorSecreto("HOOKLINE_CLAVE_SECRETA no es una clave Fernet válida.") from ex


def generar_clave() -> str:
    return Fernet.generate_key().decode()


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode()).decode()


def descifrar(cifrado: str) -> str:
    try:
        return _fernet().decrypt(cifrado.encode()).decode()
    except InvalidToken as ex:
        raise ErrorSecreto("No se pudo descifrar (¿cambió HOOKLINE_CLAVE_SECRETA?).") from ex


# --- contraseñas ------------------------------------------------------------------------------------------
_N, _R, _P = 2 ** 14, 8, 1


def hash_clave(clave: str) -> str:
    from .config import config
    minimo = config().clave_minima
    if len(clave) < minimo:
        raise ValueError(f"La contraseña tiene que tener al menos {minimo} caracteres.")
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def verificar_clave(clave: str, guardado: str) -> bool:
    try:
        _, n, r, p, sal, h = guardado.split("$")
        calc = hashlib.scrypt(clave.encode(), salt=base64.b64decode(sal), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(h)))
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


# --- tokens -------------------------------------------------------------------------------------------------
def sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def nuevo_token(prefijo: str = "", n: int = 32) -> str:
    return prefijo + secrets.token_urlsafe(n)


def iguales(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --- firmas (URLs públicas temporales para que Meta baje los archivos) --------------------------------------
def _clave_firma() -> bytes:
    k = config().clave_sesion
    if not k:
        raise ErrorSecreto("Falta HOOKLINE_CLAVE_SESION.")
    return k.encode()


def firmar(texto: str, vence: int) -> str:
    return hmac.new(_clave_firma(), f"{texto}|{vence}".encode(), hashlib.sha256).hexdigest()[:40]


def firma_valida(texto: str, vence: int, firma: str) -> bool:
    if vence < time.time():
        return False
    return hmac.compare_digest(firmar(texto, vence), firma)
