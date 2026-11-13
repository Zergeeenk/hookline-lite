"""Base de datos: SQLAlchemy 2 (Postgres en producción, SQLite para desarrollo y tests)."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import config


class Base(DeclarativeBase):
    pass


class FechaUTC(TypeDecorator):
    """Siempre guarda y devuelve datetime con zona UTC (SQLite no guarda zona: se la volvemos a poner)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, v, dialect):
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("FechaUTC necesita datetime con zona horaria")
        return v.astimezone(timezone.utc)

    def process_result_value(self, v, dialect):
        if v is None:
            return None
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


class JSONTexto(TypeDecorator):
    """JSON portable (texto en SQLite, texto en Postgres: no hace falta indexar adentro)."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, v, dialect):
        return None if v is None else json.dumps(v, ensure_ascii=False)

    def process_result_value(self, v, dialect):
        return None if v is None else json.loads(v)


def ahora() -> datetime:
    return datetime.now(timezone.utc)


_motor: Engine | None = None
_fabrica: sessionmaker | None = None


def motor() -> Engine:
    global _motor, _fabrica
    if _motor is None:
        cfg = config()
        url = cfg.url_bd
        kw = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            cfg.datos.mkdir(parents=True, exist_ok=True)
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _motor = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_motor, "connect")
            def _pragmas(con, _):
                cur = con.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        _fabrica = sessionmaker(bind=_motor, expire_on_commit=False, future=True)
    return _motor


def reiniciar_motor() -> None:
    """Para tests: vuelve a leer la config y crea un motor nuevo."""
    global _motor, _fabrica
    if _motor is not None:
        _motor.dispose()
    _motor, _fabrica = None, None


def es_postgres() -> bool:
    return motor().dialect.name == "postgresql"


@contextmanager
def sesion() -> Iterator[Session]:
    motor()
    s = _fabrica()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def crear_tablas() -> None:
    """Desarrollo y tests. En producción se usa Alembic (`hookline db migrar`)."""
    from . import modelos  # noqa: F401  (registra las tablas)
    Base.metadata.create_all(motor())
