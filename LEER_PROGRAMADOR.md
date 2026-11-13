# LEER PRIMERO (programador)

## Qué quiero

Quiero **integrar Hookline Lite dentro de otro sistema** (el repo de **Pyro**). Ese repo lo trabaja un agente de IA (Claude Code), y a ese agente le tengo que pasar **los requerimientos de integración**.

**Tu tarea:** revisar este repo y escribir el archivo **`REQUERIMIENTOS_INTEGRACION.md`** en la raíz. Ese documento se lo voy a pegar tal cual al agente de Pyro, así que tiene que poder leerlo alguien que nunca vio Hookline.

Abajo te dejo un primer borrador con lo que se ve en el código. **Verificalo, corregilo y completalo**, sobre todo lo marcado con **[A CONFIRMAR]**.

---

## Qué tiene que tener `REQUERIMIENTOS_INTEGRACION.md`

1. **Qué hace Hookline**, en 5 líneas.
2. **Módulos y qué hace cada uno.** Para cada uno: qué se puede reusar tal cual, qué se reescribe y qué depende de Windows.
3. **Dependencias:** Python, paquetes, binarios y navegador.
4. **Datos y almacenamiento:** qué archivos JSON hay, qué guarda cada uno y su esquema. Si conviene pasar a base de datos y cuáles serían las tablas.
5. **API HTTP del panel:** lista de endpoints con método, entrada y salida (está en `panel.py`).
6. **Instagram:** permisos, flujo OAuth, cómo se renuevan los tokens y límites de la API.
7. **Integración con Claude Code:** el "puente" de `panel.py esperar` / `responder`, los tipos de pedido y los 3 agentes.
8. **Seguridad:** dónde viven los secretos y cómo deberían manejarse en Pyro (variables de entorno o un vault, nunca en el repo).
9. **Qué asume Windows o una PC local** y cómo correrlo en servidor (Linux, Docker).
10. **Puntos de integración sugeridos:** qué expone Hookline para que Pyro lo llame (API, cola, webhooks) y qué necesita recibir de Pyro.
11. **Riesgos y decisiones abiertas.**
12. **Estimación:** tareas y orden.

---

## Borrador (verificar)

### Qué es
Panel web local de *community manager* para **varias cuentas de Instagram** (modelos hechas con IA). Hace cinco cosas:
- Organiza el material de cada cuenta.
- Programa y publica Reels por la API oficial.
- Lee comentarios y stats.
- Busca tendencias o "memes" para copiar el formato.
- Arma videos copiando un ejemplo (ffmpeg).

Las decisiones de criterio las toma **Claude Code** desde VS Code: jueces de calidad, tendencias, textos y respuestas.

### Archivos principales
| Archivo | Qué hace |
|---|---|
| `panel.py` | Servidor HTTP (`ThreadingHTTPServer`), puerto **8791** (`HOOKLINE_PUERTO`). Sirve `panel/index.html` y la API `/api/*`. Tiene la cola de publicación y el puente con Claude (`esperar` / `responder`). |
| `instagram_api.py` | API oficial "Instagram API with Instagram Login" (`graph.instagram.com`). Hace OAuth y publica. |
| `buscador.py` | Busca reels de referencia con **Playwright** sobre Chrome real, con perfil propio y `--remote-debugging-port=9333`. |
| `copiar.py` | Arma un video nuevo copiando el formato de un ejemplo. Usa ffmpeg y `yt-dlp` para leer el ejemplo. |
| `medios.py`, `material.py` | Manejo de fotos y videos por cuenta, y ffmpeg vía `imageio-ffmpeg`. |
| `respuestas.py`, `chat.py` | Respuestas a comentarios y chat del panel con Claude. |
| `bandeja.py` | Ícono en la bandeja de Windows que abre Chrome en modo app. **Solo Windows.** |
| `.claude/agents/` | Agentes de Claude Code: `jueces-cm`, `tendencias-cm` y `buscador-memes`. |
| `CONTEXTO.md` / `CLAUDE.md` | Las instrucciones que sigue Claude Code: tipos de pedido y formato de las respuestas JSON. |

### Dependencias
- Python 3.11+ con Pillow, imageio-ffmpeg, yt-dlp, opencv-python-headless y playwright (ver `requirements.txt`).
- Google Chrome, que usan `buscador.py` y `bandeja.py`.
- `cloudflared.exe`, para un túnel temporal en el login de Instagram (ver `instagram_api.py`). **[A CONFIRMAR]**: si en Pyro hay dominio con HTTPS, no hace falta.
- Claude Code (plan Max) en la misma máquina, para el puente.

### Instagram
- **API:** Instagram API with Instagram Login, con cuentas business o creator.
- **Permisos:** `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_insights`.
- **Redirect actual:** `https://localhost:8770/instagram/callback` **[A CONFIRMAR]**, porque el panel usa 8791.
- **Tokens:** se guardan uno por cuenta en `cuentas/<usuario>/token.json`. **Renovación [A CONFIRMAR].**
- **App ID / secret** de Meta: en `datos/config.json`. **No están en el repo.**

### Datos (todo en archivos, sin base de datos)
- `datos/`: `config.json`, `chat.json`, `puente.json`, `piezas.json`, `actividad.json`, `encargo.json` y el perfil de Chrome (`datos/navegador/`).
- `cuentas/<usuario>/`: `cuenta.json`, `token.json`, `medios.json`, `tendencias.json`.
- `Material/<modelo>/<categoría>/` y `Material/_copias/<id>/copia.json`, que guardan los medios y las copias.
- `Para subir/`, `Resultados/` y `capturas/`, que guardan las salidas.

### Puente con Claude
- El panel deja pedidos y `py panel.py esperar` los imprime. Claude los atiende y responde con `py panel.py responder "..."` o escribiendo un JSON en la ruta que indica el pedido.
- **Tipos de pedido:** `jueces`, `tendencias`, `respuestas`, `copiar`, `filtrar`, `juez` y los mensajes de chat. Están detallados en `CONTEXTO.md`.
- **[A CONFIRMAR]:** en Pyro, ¿esto sigue con Claude Code local o pasa a la API de Claude (Messages API) del lado del servidor?

### Reglas que se tienen que mantener
- Nunca se publica contenido con personas que puedan ser menores. El filtro y el juez descartan ante la duda, y eso tiene que seguir igual en Pyro.
- Ningún secreto en el repo ni en logs.
- Las publicaciones salen solo por la API oficial de Instagram (sin bots de navegador para publicar).

---

## Qué NO hay en este repo
Por privacidad no están `datos/`, los tokens, las cuentas reales, el material de las modelos ni la lista real de fuentes del buscador. En `ejemplos/` hay 5 fotos por modelo y links de videos de ejemplo. Para probar, usá tu propia app de Meta en modo desarrollo, con una cuenta de prueba.

**Entrega:** `REQUERIMIENTOS_INTEGRACION.md` en la raíz, por pull request. Si te trabás con algo, dejalo en la sección "Decisiones abiertas" y seguí.
