"""Hookline Lite en la bandeja de Windows (el iconito al lado del reloj).
Arranca con la compu, mantiene el panel siempre andando (si se cae, lo vuelve a levantar) y con un clic lo abre."""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PUERTO = 8791
CANDADO = 8792   # para que haya un solo iconito aunque se abra dos veces
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NAVEGADORES = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
               Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
               Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")]


def panel_andando():
    try:
        with socket.create_connection(("127.0.0.1", PUERTO), timeout=1):
            return True
    except OSError:
        return False


def levantar_panel():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else sys.executable), str(RAIZ / "panel.py"), "fondo"], cwd=RAIZ,
                     creationflags=SIN_VENTANA)
    for _ in range(40):
        if panel_andando():
            return True
        time.sleep(0.25)
    return False


def abrir(*_):
    if not panel_andando():
        levantar_panel()
    nav = next((n for n in NAVEGADORES if n.exists()), None)
    url = f"http://localhost:{PUERTO}"
    if nav:
        subprocess.Popen([str(nav), f"--app={url}", "--window-size=1400,900"])
    else:
        import webbrowser
        webbrowser.open(url)


def cuidar():
    """Cada 15 s: si el panel se cayó, lo vuelve a levantar (así no hay que abrirlo a mano)."""
    while True:
        if not panel_andando():
            levantar_panel()
        time.sleep(15)


def icono():
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.rounded_rectangle((2, 2, 62, 62), 16, fill=(244, 84, 150))
    dr.line([(22, 16), (22, 40), (30, 48), (42, 48), (42, 16)], fill="white", width=7, joint="curve")
    return im


def main():
    try:
        candado = socket.socket()
        candado.bind(("127.0.0.1", CANDADO))
    except OSError:
        return abrir()   # ya hay un iconito: solo abro la ventana
    import pystray
    if not panel_andando():
        levantar_panel()
    threading.Thread(target=cuidar, daemon=True).start()
    if "--abrir" in sys.argv:   # el ícono del escritorio abre la ventana; al prender la compu queda solo el iconito
        abrir()

    def reiniciar(ic, _):
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$p=(Get-NetTCPConnection -LocalPort {PUERTO} -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
                        "if ($p) { Stop-Process -Id $p -Force }"], creationflags=SIN_VENTANA)
        time.sleep(1)
        levantar_panel()

    def cerrar(ic, _):
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$p=(Get-NetTCPConnection -LocalPort {PUERTO} -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
                        "if ($p) { Stop-Process -Id $p -Force }"], creationflags=SIN_VENTANA)
        ic.stop()

    menu = pystray.Menu(pystray.MenuItem("Abrir Hookline Lite", abrir, default=True),
                        pystray.MenuItem("Reiniciar el panel", reiniciar),
                        pystray.MenuItem("Cerrar Hookline Lite", cerrar))
    pystray.Icon("hookline", icono(), "Hookline Lite", menu).run()


if __name__ == "__main__":
    main()
