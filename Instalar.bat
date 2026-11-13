@echo off
rem Instala lo que necesita Hookline Lite y crea el acceso directo en el Escritorio.
cd /d "%~dp0"
title Instalar Hookline Lite
echo.
echo  Instalando Hookline Lite...
echo.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo  ============================================================
  echo   Falta Python.
  echo.
  echo   1. Entra a https://www.python.org/downloads/ y baja Python 3.12
  echo   2. Al instalar, MARCA la casilla "Add python.exe to PATH"
  echo   3. Volve a hacer doble clic en Instalar.bat
  echo  ============================================================
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
echo  [1/3] Instalando las piezas de Python (Pillow, imageio-ffmpeg)...
%PY% -m pip install --upgrade --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo  No se pudieron instalar las piezas de Python. Revisa que haya internet y volve a probar.
  pause
  exit /b 1
)
%PY% -m pip install --disable-pip-version-check pillow-heif >nul 2>nul
echo.
echo  [2/3] Bajando cloudflared (para publicar fotos, carruseles e historias)...
if not exist "herramientas" mkdir "herramientas"
if exist "herramientas\cloudflared.exe" (
  echo  Ya estaba.
) else (
  curl -L --fail -o "herramientas\cloudflared.exe" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
  if errorlevel 1 echo  No se pudo bajar cloudflared. Las fotos no se van a poder publicar hasta que se instale ^(ver CONECTAR-INSTAGRAM.md^).
)
echo.
echo  [3/3] Creando el acceso directo en el Escritorio...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Hookline Lite.lnk')); $s.TargetPath=(Join-Path '%~dp0' 'Abrir Hookline Lite.bat'); $s.WorkingDirectory='%~dp0'; $s.IconLocation=(Join-Path '%~dp0' 'herramientas\hookline.ico'); $s.WindowStyle=7; $s.Save()"
echo.
echo  ============================================================
echo   Listo. Ahora abri Hookline Lite con el acceso directo
echo   "Hookline Lite" del Escritorio.
echo  ============================================================
echo.
pause
