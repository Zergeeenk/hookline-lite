# Hookline (servicio)

Hookline Lite convertido en un **servicio**: API HTTP + worker + Postgres. Corre en un servidor Linux o en Docker, sin
la PC de nadie prendida, y queda listo para conectarse con Pyro por API y webhooks.

Hace lo mismo que Lite (bandeja → jueces → aprobar → publicar, estadísticas, comentarios, tendencias, chat, material
por modelo, fábrica de reels, buscador de memes, repartir/"Mandar a trabajar") con el mismo panel. Lo que cambió, y por
qué, está en [Diferencias con Lite](#diferencias-con-lite).

```
servidor/
  hookline/
    api.py            API HTTP (FastAPI) + panel        worker.py      cola de trabajos y tareas periódicas
    config.py         variables de entorno              cli.py         `hookline ...`
    modelos.py, db.py tablas (SQLAlchemy) y Alembic     migrar_lite.py importa una carpeta de Hookline Lite
    auth.py, seguridad.py  login, CSRF, roles, tokens, cifrado
    almacen.py        archivos y URLs públicas firmadas para Meta
    instagram.py      Instagram API with Instagram Login (portado de Lite)
    ia/               pedidos a Claude: tareas.py (motor api | claude_code), prompts.py, esquemas.py, motor_api.py
    servicios/        cuentas, piezas (bandeja y cola), jueces (+tendencias y chat), comentarios,
                      material, fabrica (copiar video), buscador, repartir (encargo)
    panel/index.html  el panel de Lite adaptado (login, motor de IA, sin cosas de Windows)
  tests/              pytest (incluye la fábrica de punta a punta con ffmpeg)
  Dockerfile, docker-compose.yml, .env.ejemplo
```

## Arrancar

### Con Docker (producción)
```bash
cp .env.ejemplo .env      # completalo: URL pública https, claves, app de Meta, Claude
docker compose up -d --build
docker compose run --rm api db migrar
docker compose run --rm api crear-usuario duena@ejemplo.com
```
Instagram necesita una **URL pública con HTTPS** (`HOOKLINE_URL_PUBLICA`): para la vuelta del login y para bajar las
fotos. Poné el servicio detrás de un proxy con certificado (Caddy, Nginx, el balanceador de la nube) o, para probar, de
un túnel (`cloudflared tunnel --url http://localhost:8000`).

### Sin Docker (desarrollo)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,buscador]"
export HOOKLINE_CLAVE_SECRETA=$(hookline generar-clave) HOOKLINE_CLAVE_SESION=algo-largo-al-azar
hookline crear-usuario vos@ejemplo.com
hookline api            # http://localhost:8000  (panel) · /docs (API)
hookline worker         # en otra terminal
python -m pytest        # tests (~115, 1-2 min)
HOOKLINE_TEST_BD=postgresql+psycopg://usuario:clave@localhost/hookline_test python -m pytest   # los mismos, contra Postgres
```
Los tests incluyen una batería para intentar romperlo (`tests/test_romper.py`): miles de pedidos con datos basura
generados al azar (hypothesis) contra cada endpoint, ataques de rutas y permisos, publicaciones concurrentes, caídas
de Meta y de Claude, mil archivos en la bandeja e importaciones de Lite corruptas; y `tests/test_regresiones.py`
fija cada bug que encontraron las revisiones. GitHub Actions (`.github/workflows/servidor.yml`) corre todo en SQLite
y en Postgres y construye la imagen de Docker en cada PR.
Sin `HOOKLINE_BASE_DATOS` usa SQLite en `./datos-hookline/hookline.db`. Hace falta `ffmpeg` (o el que trae
`imageio-ffmpeg`) y fuentes TTF (en Debian/Ubuntu: `fonts-inter fonts-dejavu-core`).

### Pasar los datos de Hookline Lite
```bash
hookline migrar-lite "/ruta/a/Hookline Lite"
```
Importa cuentas, **tokens (quedan cifrados)**, App ID/Secret, bandeja, estadísticas, comentarios, tendencias, chat,
material, copias y banco de memes. Se puede correr más de una vez. Las horas de Lite (hora local) pasan a UTC.

## Claude: dos motores (Ajustes → "Cómo trabaja Claude")

| | **API de Claude** (`api`) | **Claude Code** (`claude_code`) |
|---|---|---|
| Cómo funciona | El worker manda cada pedido a la API de Anthropic | Los pedidos quedan en `trabajo/ia/<id>/pedido.json`; Claude Code los atiende con `hookline puente esperar` |
| Necesita | `ANTHROPIC_API_KEY` | Una máquina con Claude Code abierto en `servidor/` (ver `CLAUDE.md`), con acceso a la misma base y carpeta de datos |
| Costo | Por uso (aprox. USD 0,05 por jueces o lectura de meme; respuestas a comentarios con Haiku, casi gratis) | El plan de Claude de esa persona |
| Si se cae | Reintenta solo (3 veces) | Los pedidos esperan hasta 3 h; después quedan en error |

Los dos usan **las mismas instrucciones** (`ia/prompts.py`, salidas de `CONTEXTO.md` y `.claude/agents/` de Lite) y
**los mismos esquemas de respuesta** (`ia/esquemas.py`): con la API van como *structured outputs* (el JSON viene
garantizado); con Claude Code se valida lo que escribe. Cambiar de motor no cambia nada más.
Modelos por defecto: `claude-opus-5-5` (jueces, lectura de memes, juez, filtro, tendencias, chat) y `claude-haiku-5-5`
(respuestas). Tendencias usa la búsqueda web de Anthropic. Si Claude se niega a un pedido, queda en error con el motivo
y no se reintenta. El costo por pedido queda en `/api/tareas_ia` (tokens).

## Integración con Pyro

El plan completo (qué tiene Pyro, cómo se conectan los dos y cómo encenderlo) está en
[`INTEGRACION_PYRO.md`](../INTEGRACION_PYRO.md). En corto, son tres caminos:

1. **Hookline lee de Pyro** (Ajustes → Pyro): con una clave de la API de solo lectura de Pyro (`pyro_...`) ata cada
   cuenta a su modelo de Pyro (por el usuario de Instagram; las multicuentas heredan la de su main) y trae la
   biblioteca de fotos y videos de cada modelo a su Material, respetando las carpetas. Se repite sola (modelos cada
   6 horas, material una vez por día). Nunca escribe en Pyro. Código: `hookline/pyro.py` y `servicios/pyro.py`.
2. **Pyro llama a Hookline** (el puente `/api/hookline/*` del lado de Pyro, igual al de Contenido IA): con un token
   `hl_...`. Cada cuenta trae `pyro_modelo_id`, así Pyro muestra a cada persona solo las cuentas de sus modelos.
   Si manda `X-Hookline-Actor: Nombre (pyro:ID)`, Hookline anota quién aprobó.
3. **Hookline le avisa a Pyro** (webhooks firmados, abajo). Cada evento con `cuenta` lleva su `pyro_modelo_id`.

### Llamar a Hookline
Cualquier ruta de `/api/*` acepta `Authorization: Bearer hl_...` (sin cookies ni CSRF). Crear un token:
`hookline token pyro --rol editor` (se muestra una sola vez). Documentación OpenAPI en `/docs`.

Las más útiles para Pyro:
| Método y ruta | Para qué |
|---|---|
| `GET /api/estado` | cuentas, bandeja, chat y estado de Claude (lo que usa el panel) |
| `GET /api/piezas?cuenta=` | la bandeja / cola |
| `POST /api/subir?cuenta=&nombre=` (bytes) | subir una foto o video a la bandeja |
| `POST /api/pieza` `{op:"jueces"\|"programar"\|"cancelar"\|"caption"\|"tipo"\|"juntar"\|..., id}` | jueces, aprobar y programar (`cuando: "AAAA-MM-DDTHH:MM"` hora local; vacío = ahora) |
| `GET /api/stats?cuenta=` · `GET /api/calendario?cuenta=` | estadísticas y publicados |
| `GET /api/comentarios?cuenta=` · `POST /api/comentario` | comentarios, sugerencias, responder/ocultar |
| `POST /api/cuenta` `{op:"crear_local"\|"guardar"\|"sincronizar"\|"desconectar", id}` | cuentas |
| `POST /api/encargo` `{dia, reels, historias}` | "Mandar a trabajar" |
| `GET /api/salud` | sin login: para monitoreo |
El detalle de cada ruta y sus `op` está en `REQUERIMIENTOS_INTEGRACION.md` §5 (son las mismas de Lite) y en `/docs`.

### Que Hookline le avise a Pyro (webhooks)
Con `HOOKLINE_WEBHOOK_URL` cada evento se manda como `POST` JSON `{id, tipo, creado, datos}`, firmado con
`X-Hookline-Firma: sha256=HMAC(secreto, "<X-Hookline-Tiempo>.<cuerpo>")`. Si Pyro no responde 2xx se reintenta con
espera creciente (hasta 12 veces). Eventos: `pieza.juzgada`, `pieza.publicada`, `pieza.error`, `pieza.vencida`,
`cuenta.conectada`, `cuenta.token_vencido`, `cuenta.token_por_vencer`. Los que tienen `cuenta` traen también
`pyro_modelo_id` (la modelo de Pyro de esa cuenta, o `null`).

## Seguridad
- Login con usuario y contraseña (scrypt), cookie HttpOnly, **CSRF** en todo POST, roles `admin`/`editor`/`lector`.
  Los POST solo aceptan `application/json` (en Lite cualquier página web podía publicar mandando `text/plain`).
- **Tokens de Instagram y App Secret cifrados** (Fernet, `HOOKLINE_CLAVE_SECRETA`). Nunca salen por la API ni a logs.
- `state` de OAuth obligatorio, de un solo uso y guardado en la base (en Lite era opcional).
- Los archivos para Meta se exponen en `/publico/...` con **firma que vence** (o URL prefirmada de S3); el resto pide
  login y no puede salir del volumen de datos.
- Secretos por variables de entorno o gestor de secretos; `.env` está en `.gitignore`.

## Diferencias con Lite
- **Nada de Windows**: sin `.bat`, `os.startfile`, Explorador, `C:\Windows\Fonts` (fuentes Linux en `fuentes.py`),
  ni `cloudflared` (URL pública firmada propia o S3).
- **Base de datos** en vez de JSON; **cola de trabajos** con reintentos en vez de hilos; varios workers posibles.
- **Publicación idempotente**: un trabajo por pieza y hora (clave única), y la pieza guarda el contenedor y el
  `media_id`: un reinicio a mitad de camino no publica dos veces. Se mantienen el anticipo de 8 min y la regla de
  "vencida" (si se pasó la hora por más de 15 min sin poder arrancar, no se publica sola).
- **Token**: además del refresco automático de Lite, un trabajo diario lo refresca aunque nadie use la cuenta.
- **Aprobación de "Mandar a trabajar"**: por defecto lo que reparte queda **"Para aprobar"** con la hora sugerida
  (en Lite se programaba solo, contra la regla de "nunca publicar sin aprobación"). Se puede volver al comportamiento
  de Lite en Ajustes. Las piezas planificadas de cuentas sin conectar pasan a "Para aprobar" al conectar la cuenta
  (en Lite quedaban trabadas).
- **Marcas de agua**: la fábrica **no borra** marcas de agua de videos de otras cuentas. Si el remate tiene una, la
  copia no se arma sola: la dueña sube un remate propio o la descarta.
- **Buscador**: Chrome/Chromium headless en el servidor. Como no hay ventana para iniciar sesión, la sesión de la
  cuenta "para mirar" se carga pegando un `cookies.txt` (Memes → Cargar sesión). Se apaga con `HOOKLINE_BUSCADOR=0`.

## Pendiente / a decidir
- El build de Docker lo prueba GitHub Actions en cada PR (en el entorno donde se desarrolló no había acceso a Debian).
- Prueba real contra Meta con una app en modo desarrollo y una cuenta de prueba (los tests simulan a Meta).
- Si Pyro va a manejar cuentas de terceros: App Review de Meta y verificación del negocio.
- Riesgos de términos de uso (multicuentas, scraping, reuso de videos ajenos): ver `REQUERIMIENTOS_INTEGRACION.md` §11.
