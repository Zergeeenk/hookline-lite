"""Línea de comandos: `hookline <comando>`.

  hookline api [--puerto 8000]          servidor HTTP (panel + API)
  hookline worker                       cola de trabajos y tareas periódicas
  hookline db crear | migrar            tablas (crear = desarrollo; migrar = Alembic, producción)
  hookline generar-clave                una HOOKLINE_CLAVE_SECRETA nueva
  hookline crear-usuario EMAIL [--rol admin|editor|lector] [--nombre N]   (pide la contraseña)
  hookline token NOMBRE [--rol editor]  token Bearer para integraciones (Pyro); se muestra una sola vez
  hookline migrar-lite CARPETA          importa una carpeta de Hookline Lite (datos/, cuentas/, Material/, Para subir/)
  hookline puente esperar               (motor claude_code) espera un pedido para Claude Code y lo imprime
  hookline puente responder "texto"     (motor claude_code) contesta el último mensaje del chat
  hookline puente estado                resumen corto
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import time


def _base():
    from .db import crear_tablas, es_postgres
    if not es_postgres():
        crear_tablas()   # en SQLite (desarrollo) las tablas se crean solas


def cmd_api(a):
    import uvicorn
    _base()
    uvicorn.run("hookline.api:app", host=a.host, port=a.puerto, proxy_headers=True, forwarded_allow_ips="*")


def cmd_worker(a):
    _base()
    from .worker import main
    main()


def cmd_db(a):
    if a.accion == "crear":
        from .db import crear_tablas
        crear_tablas()
        print("Tablas creadas.")
    else:
        from pathlib import Path
        from alembic import command
        from alembic.config import Config as AC
        ini = Path(__file__).parent / "alembic.ini"
        c = AC(str(ini))
        c.set_main_option("script_location", str(Path(__file__).parent / "migraciones"))
        command.upgrade(c, "head")
        print("Base de datos al día.")


def cmd_clave(a):
    from .seguridad import generar_clave
    print(generar_clave())


def cmd_usuario(a):
    _base()
    from .auth import crear_usuario
    clave = a.clave or getpass.getpass("Contraseña (10+ caracteres): ")
    uid = crear_usuario(a.email, clave, a.nombre or "", a.rol)
    print(f"Usuario {a.email} creado (id {uid}, rol {a.rol}).")


def cmd_token(a):
    _base()
    from .auth import crear_token
    print(crear_token(a.nombre, a.rol))
    print("Guardalo ahora: no se vuelve a mostrar.", file=sys.stderr)


def cmd_migrar_lite(a):
    _base()
    from .migrar_lite import migrar
    print(json.dumps(migrar(a.carpeta, copiar_archivos=not a.sin_archivos), ensure_ascii=False, indent=1))


# --- puente para Claude Code -------------------------------------------------------------------------------------------------
def _latido(**kw):
    from .comun import ajuste, guardar_ajuste
    p = ajuste("puente", {}) or {}
    p.update(kw)
    guardar_ajuste("puente", p)


def cmd_puente(a):
    _base()
    from .ia import tareas
    if a.accion == "estado":
        from .servicios import cuentas, piezas
        cs = cuentas.listar()
        print("Cuentas:", ", ".join("@" + c["id"] + ("" if c["conectada"] else " (sin conectar)") for c in cs) or "ninguna")
        ps = piezas.listar()
        for e in ("nueva", "jueces", "juzgada", "programada", "vencida", "error"):
            n = sum(1 for p in ps if p["estado"] == e)
            if n:
                print(f"  {e}: {n}")
        print("Motor:", tareas.motor_actual(), "· puente:", tareas.estado_puente()["puente"])
        print("Pedidos abiertos para Claude Code:", len(tareas.pendientes_claude_code()))
        return
    if a.accion == "responder":
        texto = " ".join(a.texto).strip()
        if not texto:
            sys.exit("Falta el texto.")
        from sqlalchemy import select
        from .db import sesion
        from .modelos import TareaIA
        with sesion() as s:
            t = s.scalar(select(TareaIA).where(TareaIA.tipo == "chat", TareaIA.motor == "claude_code",
                                               TareaIA.estado.in_(("pendiente", "enviada"))).order_by(TareaIA.creado))
        if t:
            tareas.completar(t.id, {"respuesta": texto})
        else:
            from .comun import chat_agregar
            chat_agregar("claude", texto)
        print("ok")
        return
    # esperar
    if tareas.motor_actual() != "claude_code":
        print("El motor de IA es 'api': Claude Code no hace falta. (Se cambia en Ajustes.)")
        return
    ult = 0.0
    while True:
        if time.time() - ult > 15:
            _latido(escuchando=time.time())
            ult = time.time()
        nuevas = tareas.pendientes_claude_code()
        if nuevas:
            tareas.marcar_enviadas([t.id for t in nuevas])
            _latido(entregado=time.time(), escuchando=0)
            from .ia.tareas import dir_tarea
            for t in nuevas:
                if t.tipo == "chat":
                    print(f"La dueña dice: {t.entrada['datos'].get('texto', '')}  "
                          f"(contestá con `hookline puente responder \"...\"`)")
                else:
                    print(f"PEDIDO: {t.tipo} · {t.id}. Leé {dir_tarea(t.id) / 'pedido.json'}: seguí 'instrucciones', "
                          f"mirá cada imagen de 'imagenes'{' y buscá en internet' if t.entrada.get('web') else ''}, y "
                          f"escribí en 'escribir_en' un JSON válido con la forma de 'esquema'. Sin contestar en el chat.")
            return
        time.sleep(2)


def main(argv=None):
    p = argparse.ArgumentParser(prog="hookline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("api")
    x.add_argument("--host", default="0.0.0.0")
    x.add_argument("--puerto", type=int, default=8000)
    x.set_defaults(f=cmd_api)
    sub.add_parser("worker").set_defaults(f=cmd_worker)
    x = sub.add_parser("db")
    x.add_argument("accion", choices=["crear", "migrar"])
    x.set_defaults(f=cmd_db)
    sub.add_parser("generar-clave").set_defaults(f=cmd_clave)
    x = sub.add_parser("crear-usuario")
    x.add_argument("email")
    x.add_argument("--rol", default="admin", choices=["admin", "editor", "lector"])
    x.add_argument("--nombre", default="")
    x.add_argument("--clave", default="", help="(para scripts; si no, se pide)")
    x.set_defaults(f=cmd_usuario)
    x = sub.add_parser("token")
    x.add_argument("nombre")
    x.add_argument("--rol", default="editor", choices=["admin", "editor", "lector"])
    x.set_defaults(f=cmd_token)
    x = sub.add_parser("migrar-lite")
    x.add_argument("carpeta")
    x.add_argument("--sin-archivos", action="store_true", help="solo datos, sin copiar fotos y videos")
    x.set_defaults(f=cmd_migrar_lite)
    x = sub.add_parser("puente")
    x.add_argument("accion", choices=["esperar", "responder", "estado"])
    x.add_argument("texto", nargs="*")
    x.set_defaults(f=cmd_puente)
    a = p.parse_args(argv)
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    a.f(a)


if __name__ == "__main__":
    main()
