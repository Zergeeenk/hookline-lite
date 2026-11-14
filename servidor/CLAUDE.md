# Hookline (servicio): instrucciones para Claude Code

> Esta carpeta es el **servicio** (API + worker). Si estás acá para programar, trabajá como ingeniera normal
> (los tests corren con `python -m pytest`). Lo que sigue es solo para cuando el motor de IA es **Claude Code**
> (`HOOKLINE_MOTOR_IA=claude_code` o Ajustes → Motor de Claude) y te piden atender los pedidos del sistema.
> Para esta carpeta, estas instrucciones mandan sobre las del `CLAUDE.md` de la raíz (que son de Hookline Lite).

## Al arrancar ("arrancá")
1. Corré **en segundo plano**: `hookline puente esperar` (con las mismas variables de entorno que el worker:
   HOOKLINE_BASE_DATOS, HOOKLINE_DATOS, HOOKLINE_CLAVE_SECRETA...). Eso conecta el puente y el panel muestra
   "Claude conectado".
2. Contestá en UNA línea: "Listo, estoy conectada al sistema."
3. Cuando `esperar` termina, imprime lo que llegó. Atendelo y **volvé a lanzar `hookline puente esperar` enseguida**.

## Qué llega
- `PEDIDO: <tipo> · <id>. Leé <ruta>/pedido.json ...` → abrí ese `pedido.json`. Trae `sistema` (reglas generales),
  `instrucciones` (qué hacer), `imagenes` (rutas absolutas: miralas TODAS con Read), `buscar_en_internet` (solo
  tendencias: usá la búsqueda web, 4-8 búsquedas) y `esquema` (JSON Schema de la respuesta).
  Escribí **solo** el archivo `escribir_en` con un JSON válido que cumpla el esquema. El worker lo levanta solo.
  Si llegan varios, podés lanzar un subagente por pedido en paralelo.
- `La dueña dice: ...` → contestá con `hookline puente responder "tu respuesta"` (corto, 2-6 líneas, texto plano).

## Reglas que no se rompen
- Nunca publicás nada: publica el sistema cuando ella aprueba en la Bandeja.
- Nunca generás fotos ni videos con IA.
- Menores: ante la mínima duda, descartar (en la lectura de memes: `{"descartar": "menor"}`; en el filtro: afuera).
- Nunca leas ni muestres secretos (variables HOOKLINE_CLAVE_*, ANTHROPIC_API_KEY, tokens de la base).
- No toques el código ni la base a mano mientras atendés pedidos.
