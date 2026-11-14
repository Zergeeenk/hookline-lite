"""Acceso: sesión con cookie (panel) o token Bearer (Pyro). Roles: lector < editor < admin.

Cookies: hl_sesion (HttpOnly, el token de sesión) y hl_csrf (legible por el panel). Todo POST con sesión de cookie
tiene que traer el header X-CSRF-Token igual al csrf de la sesión (protección CSRF que Lite no tenía).
Los tokens Bearer no usan cookies, así que no necesitan CSRF.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select

from . import seguridad
from .config import config
from .db import ahora, sesion
from .modelos import Sesion, TokenAPI, Usuario

COOKIE, COOKIE_CSRF = "hl_sesion", "hl_csrf"
DURACION = timedelta(days=14)
NIVEL = {"lector": 0, "editor": 1, "admin": 2}


@dataclass
class Quien:
    id: int | None
    email: str
    nombre: str
    rol: str
    via: str            # "sesion" | "token"
    csrf: str = ""
    actor: str = ""     # con token: quién es del otro lado (el puente de Pyro manda X-Hookline-Actor)

    def dict(self) -> dict:
        return {"email": self.email, "nombre": self.nombre, "rol": self.rol}

    @property
    def firma(self) -> str:
        if self.via == "sesion":
            return self.email
        return f"token:{self.nombre}" + (f" · {self.actor}" if self.actor else "")


def crear_usuario(email: str, clave: str, nombre: str = "", rol: str = "admin") -> int:
    import re
    if rol not in NIVEL:
        raise ValueError("Rol inválido.")
    email = str(email or "").strip().lower()
    if len(email) > 200 or not re.fullmatch(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+", email):
        raise ValueError("Escribí un email válido.")
    if not isinstance(clave, str) or not 10 <= len(clave) <= 200:
        raise ValueError("La contraseña tiene que tener entre 10 y 200 caracteres.")
    nombre = re.sub(r"[\x00-\x1f\x7f]", "", str(nombre or "")).strip()[:120] or email.split("@")[0][:120]
    with sesion() as s:
        if s.scalar(select(Usuario).where(Usuario.email == email)):
            raise ValueError("Ya existe un usuario con ese email.")
        u = Usuario(email=email, nombre=nombre, clave_hash=seguridad.hash_clave(clave), rol=rol)
        s.add(u)
        s.flush()
        return u.id


_fallidos: dict[str, list[float]] = {}
MAX_FALLIDOS, VENTANA = 8, 15 * 60


def _frenar(clave: str) -> None:
    import time
    t = time.time()
    lista = [x for x in _fallidos.get(clave, []) if t - x < VENTANA]
    _fallidos[clave] = lista
    if len(lista) >= MAX_FALLIDOS:
        raise HTTPException(429, "Demasiados intentos: esperá unos minutos.")


def login(email: str, clave: str, resp: Response, ip: str = "") -> Quien:
    import time
    email = email.strip().lower() if isinstance(email, str) else ""
    clave = clave if isinstance(clave, str) else ""
    for k in (f"e:{email}", f"i:{ip}"):
        _frenar(k)
    with sesion() as s:
        u = s.scalar(select(Usuario).where(Usuario.email == email)) if email else None
        if not u or not u.activo or not seguridad.verificar_clave(clave, u.clave_hash):
            for k in (f"e:{email}", f"i:{ip}"):
                _fallidos.setdefault(k, []).append(time.time())
            raise HTTPException(401, "Email o contraseña incorrectos.")
        token, csrf = seguridad.nuevo_token(), seguridad.nuevo_token(n=24)
        s.add(Sesion(id=seguridad.sha256(token), usuario_id=u.id, csrf=csrf, expira=ahora() + DURACION))
        q = Quien(u.id, u.email, u.nombre, u.rol, "sesion", csrf)
    seg = config().cookie_segura
    resp.set_cookie(COOKIE, token, max_age=int(DURACION.total_seconds()), httponly=True, secure=seg, samesite="lax", path="/")
    resp.set_cookie(COOKIE_CSRF, csrf, max_age=int(DURACION.total_seconds()), httponly=False, secure=seg, samesite="lax", path="/")
    return q


def logout(req: Request, resp: Response) -> None:
    tok = req.cookies.get(COOKIE)
    if tok:
        with sesion() as s:
            x = s.get(Sesion, seguridad.sha256(tok))
            if x:
                s.delete(x)
    resp.delete_cookie(COOKIE, path="/")
    resp.delete_cookie(COOKIE_CSRF, path="/")


def crear_token(nombre: str, rol: str = "editor") -> str:
    """Devuelve el token UNA sola vez (en la base queda el hash)."""
    if rol not in NIVEL:
        raise ValueError("Rol inválido.")
    tok = seguridad.nuevo_token("hl_")
    with sesion() as s:
        s.add(TokenAPI(nombre=nombre[:120], hash=seguridad.sha256(tok), prefijo=tok[:10], rol=rol))
    return tok


def _actor(texto: str | None) -> str:
    """Solo para el registro (quién aprobó desde Pyro): texto plano corto, sin caracteres de control."""
    import re
    return re.sub(r"[^\w .:@()#\-áéíóúñÁÉÍÓÚÑ]", "", str(texto or ""))[:80].strip()


def identificar(req: Request) -> Quien | None:
    h = req.headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        tok = h[7:].strip()
        with sesion() as s:
            t = s.scalar(select(TokenAPI).where(TokenAPI.hash == seguridad.sha256(tok), TokenAPI.revocado.is_(False)))
            if not t:
                return None
            t.ultimo_uso = ahora()
            return Quien(None, "", t.nombre, t.rol, "token", actor=_actor(req.headers.get("x-hookline-actor")))
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    with sesion() as s:
        x = s.get(Sesion, seguridad.sha256(tok))
        if not x or x.expira < ahora():
            return None
        u = s.get(Usuario, x.usuario_id)
        if not u or not u.activo:
            return None
        return Quien(u.id, u.email, u.nombre, u.rol, "sesion", x.csrf)


def requerir(rol: str = "lector"):
    def dep(req: Request) -> Quien:
        q = identificar(req)
        if not q:
            raise HTTPException(401, "Iniciá sesión.")
        if req.method not in ("GET", "HEAD", "OPTIONS"):
            if q.via == "sesion" and not seguridad.iguales(req.headers.get("x-csrf-token") or "", q.csrf):
                raise HTTPException(403, "Falta el token CSRF: recargá la página.")
            if NIVEL[q.rol] < max(NIVEL[rol], NIVEL["editor"]):
                raise HTTPException(403, "Tu usuario no puede hacer cambios.")
        elif NIVEL[q.rol] < NIVEL[rol]:
            raise HTTPException(403, "Tu usuario no tiene permiso para esto.")
        return q
    return Depends(dep)
