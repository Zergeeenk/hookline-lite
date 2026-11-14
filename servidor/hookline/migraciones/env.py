"""Alembic: usa la misma conexión que el servicio (HOOKLINE_BASE_DATOS) y las tablas de hookline.modelos."""
from alembic import context

from hookline import modelos  # noqa: F401
from hookline.db import Base, motor

objetivo = Base.metadata


def correr():
    with motor().connect() as con:
        context.configure(connection=con, target_metadata=objetivo, render_as_batch=con.dialect.name == "sqlite")
        with context.begin_transaction():
            context.run_migrations()


correr()
