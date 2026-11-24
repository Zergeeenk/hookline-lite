"""`hookline buscador-probar`: lo que se corre en el servidor para saber si el buscador anda contra las redes reales."""
import pytest


def _correr(monkeypatch, args):
    from hookline import cli
    from hookline.servicios import buscador
    pedidas = []

    def probar(consultas=None):
        pedidas.append(consultas)
        return [{"fuente": "ig x", "red": "Instagram", "url": "u", "sesion": True, "ok": True, "videos": 3,
                 "pidio_login": False, "final": "https://www.instagram.com/x", "ejemplos": [], "tarjetas": {},
                 "api_ig": {"estado": 404, "medios": 0, "inicio": "not-logged-in"}, "sin_marca": False}]
    monkeypatch.setattr(buscador, "probar", probar)
    with pytest.raises(SystemExit) as e:
        cli.main(["buscador-probar", *args])
    return pedidas, e.value.code


def test_fuentes_elige_como_la_busqueda_de_verdad_y_suma_las_explicitas(monkeypatch, capsys):
    from hookline.db import sesion
    from hookline.modelos import FuenteBusqueda
    from hookline.servicios import buscador
    buscador.sumar_fuentes(["tt @muerta", "ig: viva", "pin: memes"])
    with sesion() as s:                                  # 60 vistos y ninguno sirvió: la búsqueda ya no la usa
        f = next(x for x in s.query(FuenteBusqueda) if x.consulta == "@muerta")
        f.vistos, f.sirven = 60, 0
    pedidas, codigo = _correr(monkeypatch, ["ig @memes", "--fuentes"])
    assert pedidas == [["ig @memes", "ig: viva", "pin: memes"]] and codigo == 0
    out = capsys.readouterr().out
    assert "API de Instagram: 404" in out and "not-logged-in" in out


def test_fuentes_sin_fuentes_avisa_y_no_prueba_otra_cosa(monkeypatch, capsys):
    pedidas, codigo = _correr(monkeypatch, ["--fuentes"])
    assert pedidas == [] and codigo == 1 and "No hay fuentes activas" in capsys.readouterr().out


def test_sin_argumentos_usa_las_de_prueba(monkeypatch):
    pedidas, codigo = _correr(monkeypatch, [])
    assert pedidas == [None] and codigo == 0
