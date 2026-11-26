"""Si el worker se reinicia justo mientras baja el audio de un reel (#31), su carpeta .temporal-* queda en el disco (hasta
~60 MB) y no la borra nadie. La próxima descarga en esa carpeta barre las de más de una hora; las recientes pueden ser
de otra descarga en curso y no se tocan."""
import os
import time


def _vieja(p, horas):
    t = time.time() - horas * 3600
    for x in [p, *p.rglob("*")]:
        os.utime(x, (t, t))


def test_barre_las_temporales_de_mas_de_una_hora(tmp_path):
    from hookline.servicios import material
    vieja = tmp_path / ".temporal-viejaaaaaa"
    (vieja / "sub").mkdir(parents=True)
    (vieja / "musica-0").write_bytes(b"x" * 1000)
    (vieja / "sub" / "con-musica-0.mp4").write_bytes(b"x")
    _vieja(vieja, 2)
    reciente = tmp_path / ".temporal-recienteee"
    reciente.mkdir()
    (reciente / "musica-0").write_bytes(b"x")
    _vieja(reciente, 0.5)
    otra = tmp_path / ".otra-cosa"            # solo las .temporal-*: lo demás no se toca aunque sea viejo
    otra.mkdir()
    _vieja(otra, 5)
    ejemplo = tmp_path / ".temporal-archivo"  # un archivo con ese nombre no es una carpeta nuestra
    ejemplo.write_bytes(b"x")
    _vieja(ejemplo, 5)
    video = tmp_path / "ejemplo.mp4"
    video.write_bytes(b"x")

    nueva = material._temporales(video)

    assert nueva.is_dir() and nueva.parent == tmp_path and nueva.name.startswith(".temporal-")
    assert not vieja.exists()
    assert reciente.is_dir() and otra.is_dir() and ejemplo.is_file() and video.is_file()


def test_si_no_puede_barrer_igual_da_la_carpeta(tmp_path, monkeypatch):
    from hookline.servicios import material
    vieja = tmp_path / ".temporal-viejaaaaaa"
    vieja.mkdir()
    _vieja(vieja, 2)
    monkeypatch.setattr(material.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("ocupado")))
    nueva = material._temporales(tmp_path / "ejemplo.mp4")
    assert nueva.is_dir()
