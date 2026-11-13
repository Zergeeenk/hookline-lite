@echo off
rem Abre Hookline Lite: arranca el panel en segundo plano (sin ventana) y abre el navegador.
cd /d "%~dp0"
set "PYW="
where pyw >nul 2>nul && set "PYW=pyw -3"
if not defined PYW (
  where pythonw >nul 2>nul && set "PYW=pythonw"
)
if not defined PYW (
  echo.
  echo  ============================================================
  echo   Falta Python.
  echo.
  echo   1. Entra a https://www.python.org/downloads/ y baja Python 3.12
  echo   2. Al instalar, MARCA la casilla "Add python.exe to PATH"
  echo   3. Despues hace doble clic en Instalar.bat
  echo.
  echo   Esta todo explicado en INSTALAR.md
  echo  ============================================================
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
rem si ya estaba abierto, este segundo panel se cierra solo (el puerto 8791 esta ocupado)
start "" %PYW% panel.py fondo
rem esperar a que el panel responda (hasta ~15 s) y abrir el navegador
for /l %%i in (1,1,15) do (
  curl -s -o nul -m 1 http://localhost:8791/api/ajustes >nul 2>nul && goto abrir
  ping -n 2 127.0.0.1 >nul
)
:abrir
rem en su propia ventana (como una app), con Edge o Chrome; si no hay, en el navegador de siempre
set "APPNAV="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "APPNAV=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined APPNAV if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "APPNAV=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined APPNAV if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "APPNAV=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if defined APPNAV (
  start "" "%APPNAV%" --app=http://localhost:8791 --window-size=1400,900
) else (
  start "" "http://localhost:8791"
)
exit /b 0
