"""Webhooks salientes hacia Pyro (outbox con reintentos).

Cada evento se manda como POST JSON a HOOKLINE_WEBHOOK_URL:
  {"id": 12, "tipo": "pieza.publicada", "creado": "2026-11-20T23:00:05+00:00", "datos": {...}}
con headers:
  X-Hookline-Evento: <tipo>
  X-Hookline-Firma:  sha256=<HMAC-SHA256(cuerpo, HOOKLINE_WEBHOOK_SECRETO)>
  X-Hookline-Tiempo: <epoch>   (también firmado: firma = HMAC(f"{tiempo}.{cuerpo}"))
Pyro responde 2xx para confirmar. Si no, se reintenta con espera creciente (hasta 12 veces, ~1 día).

Tipos: pieza.juzgada, pieza.publicada, pieza.error, pieza.vencida, cuenta.conectada, cuenta.token_vencido,
cuenta.token_por_vencer, copia.lista, copia.rechazada, encargo.listo, encargo.incompleto.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import select, update

from .config import config
from .db import ahora, sesion
from .modelos import Evento

log = logging.getLogger("hookline.eventos")
MAX_INTENTOS = 12


def firmar(cuerpo: bytes, tiempo: str, secreto: str) -> str:
    return "sha256=" + hmac.new(secreto.encode(), f"{tiempo}.".encode() + cuerpo, hashlib.sha256).hexdigest()


def enviar_pendientes(limite: int = 20) -> int:
    cfg = config()
    if not cfg.webhook_url:
        return 0
    with sesion() as s:
        candidatos = s.scalars(select(Evento.id).where(Evento.enviado.is_(None), Evento.intentos < MAX_INTENTOS,
                                                       Evento.proximo <= ahora()).order_by(Evento.id).limit(limite)).all()
        # reclamar cada evento (con varios workers, cada evento lo manda uno solo)
        mios = [i for i in candidatos
                if s.execute(update(Evento).where(Evento.id == i, Evento.enviado.is_(None), Evento.proximo <= ahora())
                             .values(proximo=ahora() + timedelta(minutes=2))).rowcount == 1]
    with sesion() as s:
        evs = [s.get(Evento, i) for i in mios]
    n = 0
    with httpx.Client(timeout=5) as cli:
        for e in evs:
            cuerpo = json.dumps({"id": e.id, "tipo": e.tipo, "creado": e.creado.isoformat(), "datos": e.datos},
                                ensure_ascii=False).encode()
            t = str(int(time.time()))
            headers = {"Content-Type": "application/json", "X-Hookline-Evento": e.tipo, "X-Hookline-Tiempo": t}
            if cfg.webhook_secreto:
                headers["X-Hookline-Firma"] = firmar(cuerpo, t, cfg.webhook_secreto)
            try:
                r = cli.post(cfg.webhook_url, content=cuerpo, headers=headers)
                ok, err = 200 <= r.status_code < 300, f"HTTP {r.status_code}"
            except httpx.HTTPError as ex:
                ok, err = False, str(ex)[:200]
            with sesion() as s:
                x = s.get(Evento, e.id)
                x.intentos += 1
                if ok:
                    x.enviado, x.error = ahora(), ""
                    n += 1
                else:
                    x.error = err
                    x.proximo = ahora() + timedelta(seconds=min(3 * 3600, 30 * 2 ** x.intentos))
    return n
