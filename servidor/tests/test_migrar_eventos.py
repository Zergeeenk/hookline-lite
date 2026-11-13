"""Importar una carpeta de Hookline Lite y mandar webhooks a Pyro."""
import json
import time

import httpx


def test_migrar_lite(tmp_path, foto):
    from hookline import almacen
    from hookline.db import sesion
    from hookline.migrar_lite import migrar
    from hookline.modelos import CredencialIG, Cuenta, Pieza
    lite = tmp_path / "lite"
    (lite / "datos").mkdir(parents=True)
    (lite / "datos" / "config.json").write_text(json.dumps({"app_id": "555", "app_secret": "SECRETO-META"}))
    for cid, extra in (("modelo1", {}), ("multi1", {"main": "modelo1"})):
        d = lite / "cuentas" / cid
        d.mkdir(parents=True)
        (d / "cuenta.json").write_text(json.dumps({"usuario": cid, "nicho": "humor", "creada": "2026-10-01T10:00", **extra}))
    t = time.time()
    (lite / "cuentas" / "modelo1" / "token.json").write_text(json.dumps(
        {"access_token": "TOKEN-LITE", "permisos": "a,b", "obtenido": t, "refrescado": t, "vence": t + 9e5, "ig_id": "77"}))
    (lite / "Para subir" / "modelo1").mkdir(parents=True)
    (lite / "Para subir" / "modelo1" / "x.jpg").write_bytes(foto.read_bytes())
    (lite / "datos" / "piezas.json").write_text(json.dumps([{
        "id": "p1", "cuenta": "modelo1", "tipo": "foto", "archivos": ["Para subir/modelo1/x.jpg"],
        "estado": "programada", "cuando": "2026-12-01T20:00", "caption": "hola"}]))
    r = migrar(str(lite))
    assert r["cuentas"] == 2 and r["tokens"] == 1 and r["piezas"] == 1 and r["archivos"] == 1
    with sesion() as s:
        assert s.get(Cuenta, "multi1").main_id == "modelo1"
        assert "TOKEN-LITE" not in s.get(CredencialIG, "modelo1").token_cifrado
        p = s.get(Pieza, "p1")
        assert p.archivos == ["para-subir/modelo1/x.jpg"] and almacen.ruta(p.archivos[0]).is_file()
        assert p.cuando.hour == 23   # 20:00 de Argentina = 23:00 UTC
    assert migrar(str(lite))["cuentas"] == 2   # se puede repetir sin duplicar


def test_webhook_firmado_con_reintento(monkeypatch):
    from hookline import config, eventos
    from hookline.comun import evento
    from hookline.db import sesion
    from hookline.modelos import Evento
    monkeypatch.setenv("HOOKLINE_WEBHOOK_URL", "https://pyro.test/hooks/hookline")
    monkeypatch.setenv("HOOKLINE_WEBHOOK_SECRETO", "compartido")
    config.recargar()
    recibidos, respuestas = [], [500, 200]

    def manejar(req: httpx.Request):
        recibidos.append(req)
        return httpx.Response(respuestas.pop(0))

    real = httpx.Client
    monkeypatch.setattr(eventos.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(manejar), **kw))
    evento("pieza.publicada", pieza="p1", cuenta="modelo1")
    assert eventos.enviar_pendientes() == 0           # 500: queda para reintentar
    with sesion() as s:
        e = s.query(Evento).one()
        assert e.intentos == 1 and e.enviado is None
        e.proximo = e.creado
    assert eventos.enviar_pendientes() == 1
    req = recibidos[-1]
    cuerpo = req.content
    assert json.loads(cuerpo)["tipo"] == "pieza.publicada"
    assert req.headers["x-hookline-firma"] == eventos.firmar(cuerpo, req.headers["x-hookline-tiempo"], "compartido")
