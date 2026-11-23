"""Descargas con yt-dlp de verdad (sin internet: servidas acá). Lo que dio el servidor el 23/11: los pines de Pinterest
vienen solo en HLS con el video y el audio en pistas separadas, y el formato pedido («un archivo con los dos») no
existía → «No se pudo bajar»."""
import functools
import http.server
import subprocess
import threading

import pytest

from hookline import medios

pytestmark = pytest.mark.skipif(not medios.ffmpeg(), reason="hace falta ffmpeg")


@pytest.fixture
def hls(tmp_path):
    ff = medios.ffmpeg()
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:size=360x640:rate=30:duration=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    str(tmp_path / "v.m3u8")], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "aac",
                    "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", str(tmp_path / "a.m3u8")], check=True)
    (tmp_path / "master.m3u8").write_text(
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="a.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=360x640,CODECS="avc1.64001e,mp4a.40.2",AUDIO="aud"\nv.m3u8\n')
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        type("H", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/master.m3u8"
    srv.shutdown()


def _pistas(p) -> str:
    r = subprocess.run([medios.ffmpeg(), "-hide_banner", "-i", str(p)], capture_output=True, text=True)
    return r.stderr


def test_hls_con_video_y_audio_separados_se_baja_y_queda_un_mp4_con_los_dos(hls, tmp_path):
    from hookline.servicios import material
    video, info = material.bajar_video(hls, tmp_path / "salida", "pin")
    assert video.is_file() and video.suffix == ".mp4" and video.stat().st_size > 1000
    pistas = _pistas(video)
    assert "Video: h264" in pistas and "Audio: aac" in pistas, pistas


def test_un_pin_del_banco_se_baja_por_su_hls(hls, tmp_path, monkeypatch):
    from hookline.servicios import fabrica, material
    monkeypatch.setattr(material, "ESQUEMAS_VIDEO", ("https://", hls.rsplit("/", 1)[0] + "/"))
    link = "https://www.pinterest.com/pin/e1_AfqPShzuSG4ScyIabfQ-WkP/"
    fabrica.agregar_encontrados([{"link": link, "titulo": "pov", "img": "", "video": hls, "dur": 4.0}])
    video, _ = material.bajar_video(link, tmp_path / "salida", "pin")
    assert "Audio: aac" in _pistas(video)
