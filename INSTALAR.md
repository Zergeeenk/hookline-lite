# Instalar Hookline Lite (paso a paso)

No hace falta saber programar. Son cuatro cosas: Python, Visual Studio Code, Claude Code y Hookline Lite.

---

## 1. Instalar Python 3.12

1. Entrá a **https://www.python.org/downloads/**.
   *Vas a ver una página amarilla y azul con un botón grande "Download Python 3.x".* Si ofrece 3.13 o más nueva,
   también sirve; si querés exactamente la 3.12, bajá la página hasta "Looking for a specific release?" y elegí 3.12.
2. Abrí el archivo que se bajó (`python-3.12.x-amd64.exe`).
3. **MUY IMPORTANTE:** en la primera pantalla del instalador, abajo, hay una casilla que dice
   **"Add python.exe to PATH"**. **Marcala** (tiene que quedar con el tilde).
   *La pantalla tiene el logo de Python a la izquierda y dos opciones: "Install Now" y "Customize installation".*
4. Tocá **Install Now** y esperá. Cuando diga **"Setup was successful"**, tocá **Close**.

## 2. Instalar Visual Studio Code

1. Entrá a **https://code.visualstudio.com** y tocá **Download for Windows**.
2. Abrí el instalador, aceptá todo con **Siguiente** y al final **Instalar**.
   *Si aparece la opción "Agregar la acción Abrir con Code al menú contextual", marcala: te ahorra pasos.*

## 3. Instalar Claude Code (dentro de Visual Studio Code)

1. Abrí **Visual Studio Code**.
2. A la izquierda hay una barra con íconos. Tocá el de **Extensiones** (cuatro cuadraditos, uno separado).
3. En el buscador de arriba escribí **Claude Code**. Tiene que aparecer la de **Anthropic** (con el logo naranja de Claude).
4. Tocá **Install** (Instalar).
5. Aparece el ícono de Claude (arriba a la derecha o en la barra izquierda). Tocalo y **iniciá sesión** con tu cuenta de
   Claude (la del plan Pro).

## 4. Instalar Hookline Lite

1. Poné la carpeta **Hookline Lite** en el **Escritorio** (si te la pasaron en un .zip: clic derecho → **Extraer todo**).
2. Entrá a la carpeta y hacé **doble clic en `Instalar.bat`**.
   *Se abre una ventana negra que va mostrando "[1/3]", "[2/3]", "[3/3]". Tarda uno o dos minutos.*
   Si Windows muestra un cartel azul "Windows protegió su PC": tocá **Más información → Ejecutar de todas formas**.
3. Cuando diga **"Listo"**, apretá cualquier tecla. En el Escritorio vas a tener el acceso directo **Hookline Lite**
   (ícono rosa).
4. Hacé doble clic en **Hookline Lite**: se abre el panel en el navegador (`http://localhost:8791`).

## 5. Conectar Claude (cada vez que prendés la compu)

1. Abrí **Visual Studio Code**.
2. Menú **Archivo → Abrir carpeta…** y elegí **Escritorio → Hookline Lite**. Si pregunta "¿Confiás en los autores?",
   tocá **Sí, confío**.
3. Abrí Claude (el ícono de Claude) y escribile: **arrancá**.
4. La primera vez puede pedirte permiso para usar comandos: tocá **Permitir** (o "Yes, don't ask again").
5. Listo: en el panel, abajo a la izquierda, dice **Claude conectado** (punto verde). Podés minimizar VS Code.

## 6. Conectar tus Instagram

Seguí **CONECTAR-INSTAGRAM.md**.

---

### Si algo falla

- **"Falta Python"** al abrir: repetí el paso 1 y no te olvides de marcar **"Add python.exe to PATH"**. Después
  volvé a hacer doble clic en `Instalar.bat`.
- **El panel no abre**: cerrá el navegador, esperá 10 segundos y volvé a hacer doble clic en el acceso directo.
- **El cartel "Claude no está conectado" no se va**: revisá que VS Code esté abierto **en la carpeta Hookline Lite**
  y que le hayas escrito **arrancá** a Claude.
- **Los videos no tienen vista previa**: volvé a correr `Instalar.bat`.
