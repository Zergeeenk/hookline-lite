# Traspaso técnico de Hookline (noviembre 2026)

Documento de entrega del proyecto para quien lo sigue (Sebas) y para su asistente de IA. Resume **todo** lo que se
construyó, cómo está desplegado, cómo se opera, qué decisiones se tomaron y por qué, qué se rompió y cómo se
arregló, y qué queda abierto. Cada dato está verificado contra el código de `main` (commit `b0b901e`, PR #19).

> **Para la IA que lea esto:** el producto que corre en producción es **`servidor/`** (Python, FastAPI + worker +
> Postgres, en Docker). Los archivos sueltos de la raíz (`panel.py`, `copiar.py`, `*.bat`, `CONTEXTO.md`…) son
> **Hookline Lite**, la versión original de Windows: quedan como referencia y como fuente de `hookline migrar-lite`,
> pero ya no se desarrollan. El `CLAUDE.md` de la raíz describe un rol de "community manager" para Lite: si estás
> programando, ignorá ese rol y seguí `servidor/CLAUDE.md` + este documento + `servidor/README.md`.

---

## 1. Qué es

**Hookline** es la community manager de las cuentas de Instagram de las modelos de la agencia, más una fábrica de
contenido:

- **Bandeja → jueces → aprobar → publicar**: se sube una foto/video/carrusel, Claude (los "jueces") lo puntúa y
  propone caption, hashtags y horario, una persona aprueba y el sistema publica a la hora en Instagram (Graph API).
- **Copiar video** (la función más usada por el equipo): se pega el link de un meme/reel de ejemplo, el sistema lo
  baja, Claude lo "lee" (texto, posición, estilo, chats/notificaciones, remate), se reemplaza a la chica del ejemplo
  por material de la modelo, se arma con ffmpeg y un juez lo revisa. Hay un editor manual completo.
- **Banco de memes + buscador**: links de ejemplo guardados, con miniatura, y un buscador (Chromium headless) que
  encuentra más.
- **Comentarios, estadísticas, tendencias, chat con Claude, material por modelo, "Mandar a trabajar"** (reparte
  material en reels e historias para varios días).
- **Espacio Noticias** (cuentas tipo @loqueborraron): radar de noticias, guiones de reels hablados con jurado de 10
  personas, costos por reel.
- **Integración con Pyro (Model Pro)**, el CRM de la agencia: Hookline lee modelos y material de Pyro; Pyro puede
  llamar a Hookline por un puente (propuesta, ver §9).

Reglas de producto que **no se negocian** (están en prompts y en código):
- **Nunca publica solo**: publica el sistema cuando una persona aprueba en la Bandeja.
- **Nunca genera fotos ni videos con IA.** El video lo arma ffmpeg con material real.
- **Menores: ante la mínima duda, descartar** (en la lectura de memes, `{"descartar": "menor"}`). El equipo pidió
  aflojarlo porque a veces descarta chicas adultas: **se decidió no tocarlo**.
- **Marcas de agua**: la fábrica no borra marcas de agua ajenas. Si el ejemplo tiene una, la copia queda frenada y
  una persona elige «Usarlo igual» (`usar_con_marca`, guarda `marca_ok`), sube un remate propio o la descarta.
- No se hacen cuentas falsas masivas, engagement falso, ni se automatiza con suscripciones de consumo de Claude.

---

## 2. Dónde vive

| Qué | Dónde |
|---|---|
| Repo | `Zergeeenk/hookline-lite` (GitHub). Rama `main` protegida por CI. |
| Servidor | Droplet de DigitalOcean (nyc1, 2 vCPU / 4 GB, Ubuntu). Checkout en `~/hookline-lite`, se opera desde `~/hookline-lite/servidor` como `root`. |
| Contenedores | `docker compose` con `db` (postgres:16-alpine), `api`, `worker` (misma imagen) y `caddy` (HTTPS, perfil `https`). |
| Datos | Volumen `datos` → `/datos` en api y worker (medios, copias, cuadros, fuentes, cookies). Volumen `pg` → la base. |
| URL | HTTPS con Caddy + Let's Encrypt sobre un dominio `sslip.io` de la IP del droplet (`HOOKLINE_DOMINIO`, `HOOKLINE_URL_PUBLICA` en `.env`). |
| Secretos | `servidor/.env` en el droplet (nunca en git) y, cifrados con Fernet en la tabla `ajustes`, lo que se carga desde el panel (App ID/Secret de Meta, API key de Anthropic, clave de Pyro). |

**Accesos que hay que pedir/transferir** (no están en este documento a propósito): SSH al droplet, cuenta de
DigitalOcean, usuario admin del panel (se crea con `docker compose run --rm api crear-usuario email`), la app de
Meta (developers.facebook.com), la API key de Anthropic (console.anthropic.com), y la cuenta de Instagram
"para mirar" (la que da las cookies al buscador y a las descargas).

---

## 3. Deploy y operación

### Deploy (lo que se hizo en cada entrega)
```bash
cd ~/hookline-lite && git pull
cd servidor && docker compose up -d --build
docker compose run --rm api db migrar        # SOLO si el PR trae una migración nueva en hookline/migraciones/versions
```
El build tarda ~3 min (instala fuentes, Chromium, ffmpeg y `pip install ".[buscador,heic]"`, que trae el `yt-dlp`
más nuevo en cada build: es bueno, Instagram cambia seguido).

### Migraciones (Alembic, en `servidor/hookline/migraciones/versions/`)
`0001_tablas_iniciales` · `0002_pyro` · `0003_ig_unificado` · `0004_noticias`. Una nueva va como `0005_...` y
se aplica con `db migrar`. Nunca editar una ya aplicada.

### Comandos útiles en el servidor
```bash
docker compose ps                                     # estado
docker compose logs -f --tail=200 worker              # el worker (descargas, render, IA, publicación)
docker compose logs -f --tail=200 api
docker compose exec -T db psql -U hookline -d hookline   # la base (cuidado: producción)
docker compose run --rm api crear-usuario alguien@ejemplo.com
docker compose run --rm api token pyro --rol editor   # token hl_... para que Pyro llame a Hookline (se ve una vez)
```

### Diagnosticar una descarga de Instagram/TikTok
**Siempre con una copia de las cookies**: yt-dlp reescribe el `cookiefile` al terminar y, si Instagram dice que la
sesión no vale, le borra el `sessionid` (eso causó el incidente de §8.1).
```bash
L="https://www.instagram.com/reel/XXXX/"
docker compose exec -T worker sh -c 'cp /datos/navegador/cookies.txt /tmp/ck.txt && yt-dlp -v --skip-download --cookies /tmp/ck.txt --print "OK %(duration)ss" "$0" 2>&1 | grep -E "Found Instagram|no longer valid|^OK|ERROR"; rm -f /tmp/ck.txt' "$L"
```
`Found Instagram account cookies` = la sesión se cargó. `no longer valid` = venció: cargar cookies nuevas en el
panel (Memes → «Cargar sesión de Instagram/TikTok»). Al 23/11 los `/reel/` bajan sin sesión; los `/p/` necesitan
sesión (o son de fotos).

### Variables de entorno (`servidor/hookline/config.py`, ejemplo en `servidor/.env.ejemplo`)
General: `HOOKLINE_ENTORNO`, `HOOKLINE_URL_PUBLICA`, `HOOKLINE_DOMINIO`, `HOOKLINE_ZONA_HORARIA`,
`HOOKLINE_COOKIE_SEGURA`, `HOOKLINE_CLAVE_MINIMA`, `HOOKLINE_BASE_DATOS`, `HOOKLINE_DATOS`, `HOOKLINE_DIR_FUENTES`,
`HOOKLINE_ORIGENES_PERMITIDOS`, `POSTGRES_PASSWORD`, `COMPOSE_PROFILES`, `HOOKLINE_PUERTO`.
Secretos: `HOOKLINE_CLAVE_SECRETA` (Fernet; **si se pierde, no se descifran los tokens guardados**),
`HOOKLINE_CLAVE_SESION`.
Meta: `HOOKLINE_META_APP_ID`, `HOOKLINE_META_APP_SECRET`, `HOOKLINE_META_CONFIG_ID` (login unificado),
`HOOKLINE_IG_VERSION`.
Claude: `HOOKLINE_MOTOR_IA` (`api` | `claude_code`), `ANTHROPIC_API_KEY`, `HOOKLINE_MODELO`
(def. `claude-opus-5-5`), `HOOKLINE_MODELO_RAPIDO` (def. `claude-haiku-5-5`), `HOOKLINE_ESFUERZO`.
Archivos: `HOOKLINE_ALMACEN` (local | s3), `HOOKLINE_S3_*`.
Worker: `HOOKLINE_MAX_RENDER` (2), `HOOKLINE_MAX_DESCARGAS` (3), `HOOKLINE_MAX_PUBLICANDO` (2),
`HOOKLINE_WORKER_HILOS`, `HOOKLINE_BUSCADOR` (0 lo apaga), `HOOKLINE_CHROME`.
Pyro: `HOOKLINE_PYRO_URL`, `HOOKLINE_PYRO_CLAVE` (mejor en Ajustes → Pyro), `HOOKLINE_WEBHOOK_URL`,
`HOOKLINE_WEBHOOK_SECRETO`.

### Backups
**No hay backup automático configurado.** Recomendado antes de cualquier cosa grande:
```bash
docker compose exec -T db pg_dump -U hookline hookline | gzip > ~/backup-hookline-$(date +%F).sql.gz
```
y una copia del volumen `datos` (o snapshot del droplet en DigitalOcean).

---

## 4. Arquitectura (`servidor/hookline/`)

```
api.py           FastAPI: el panel (GET /), /api/* (≈65 rutas), /media, /mini, /emoji/{k}.webp, /publico/* firmado
worker.py        cola de trabajos + reloj (tareas periódicas)          cli.py   `hookline api|worker|db|crear-usuario|token|migrar-lite|puente|generar-clave`
config.py        variables de entorno                                    db.py, modelos.py   SQLAlchemy 2 + Alembic (SQLite o Postgres)
auth.py, seguridad.py   login scrypt, cookie HttpOnly, CSRF, roles admin/editor/lector, tokens hl_, cifrado Fernet
trabajos.py      cola en la tabla `trabajos` (clave única = idempotencia; reintentos con espera)
comun.py         ajustes, actividad, candados (locks en DB), avisos, eventos, app de Meta
almacen.py       carpetas bajo /datos ("secciones": copias, stickers, chat, material…) y URLs públicas firmadas
instagram.py     Instagram API with Instagram Login (publicar, insights, comentarios, tokens)
eventos.py       webhooks firmados hacia Pyro                            pyro.py  cliente de la API de solo lectura de Pyro
medios.py        ffmpeg/ffprobe                                          fuentes.py  catálogo de letras, `dibujable()`
chat_img.py      dibuja chats de WhatsApp/iMessage y notificaciones como PNG para los memes
emojis/          pack Fluent 3D de Microsoft (MIT): 3d/<código>.webp (1595, 192 px), indice.json, licencia
ia/              tareas.py (crear/recoger pedidos, motores), prompts.py, esquemas.py (Pydantic), motor_api.py
servicios/       cuentas, piezas (bandeja y cola), jueces (+chat, tendencias), comentarios, material, fabrica
                 (copiar video + banco), buscador, repartir (encargo), noticias, pyro
panel/index.html el panel entero (≈2700 líneas, JS vanilla, sin build)
```

### Worker
- Grupos con su propio cupo de concurrencia: `publicar`, `render` (`render`, `preparar_copia`), `descargas`
  (`descargar_ejemplo`, `bajar_material`, `fichas`, `pyro_material`), `buscador` (`buscar`) y general.
- Cada 4 s (`_rapido`): escanear bandeja, recoger resultados de Claude Code, mandar webhooks, `fabrica.recoger`,
  `buscador.recoger`, `repartir.seguir_encargo`.
- Cada 30 s (`periodicas`): por cuenta conectada `sincronizar` (por hora) y `refrescar_token` (por día),
  `tendencias_semanales`, `mantenimiento` (diario), `noticias.periodicas()` (radar diario), `pyro.periodicas()`.
  Las claves por ventana de tiempo evitan duplicados aunque haya varios workers.
- Manejadores registrados con `@manejador(...)`: `sincronizar`, `refrescar_token`, `tendencias_semanales`,
  `mantenimiento`, `ia`, `pyro_sincronizar`, `pyro_material`, `fichas`, `descargar_ejemplo`, `preparar_copia`,
  `render`, `preparar_jueces`, `buscar`, `radar_diario`, `publicar`, `bajar_material`.

### IA (`ia/`)
- `tareas.crear(tipo, datos, referencia, imagenes)` crea una `TareaIA`; al terminar llama al callback del tipo
  (`al_terminar` / `al_fallar`). Tipos (`esquemas.py`): `jueces`, `respuestas`, `tendencias`, `lectura` (leer un
  meme), `juez` (revisar el video armado), `filtrar` (buscador), `chat`, `radar`, `guion`, `jurado`.
- **Motor `api`**: structured outputs. Límites de Anthropic: 24 parámetros opcionales y 16 uniones por pedido, más
  un límite interno ("Schema is too complex for compilation"). Por eso existe **`esquemas.PARA_API` / `para_api(tipo)`**:
  versiones con todos los campos requeridos y valores centinela (ej. `LecturaAPI` con `hay: bool` en chat/noti), que
  `_lectura_desde_api` convierte de vuelta. `motor_api._bucle_pausa` sigue los `pause_turn` (búsqueda web) y suma el
  uso de todas las vueltas. BadRequest/Authentication/PermissionDenied → `ErrorIA` (no se reintenta).
- **Motor `claude_code`**: los pedidos quedan en `trabajo/ia/<id>/pedido.json`; un Claude Code abierto en
  `servidor/` corre `hookline puente esperar` y escribe `respuesta.json` (ver `servidor/CLAUDE.md`).
- Uso y costo por pedido en `TareaIA.uso` (tokens, cache, búsquedas) → página **Costos**.

### Tablas (`modelos.py`)
`usuarios`, `sesiones`, `tokens_api`, `ajustes`, `oauth_estados`, `cuentas`, `credenciales_ig`, `medios_ig`,
`historial_cuenta`, `comentarios`, `tendencias`, `piezas`, `tareas_ia`, `trabajos`, `chat`, `actividad`, `encargos`,
`eventos`, `material_meta`, `copias`, `banco_links`, `fuentes_busqueda`, `noticias`, `guiones`.

---

## 5. Funcionalidades, con lo que hay que saber de cada una

### Cuentas e Instagram
- Login de Instagram con "Instagram API with Instagram Login". Las apps de Meta nuevas usan el **login unificado**
  (`config_id`, `HOOKLINE_META_CONFIG_ID` o Ajustes) — PR #11.
- `state` de OAuth en la base, de un solo uso. Tokens cifrados; refresco diario.
- Publicación idempotente (un trabajo por pieza y hora; guarda contenedor y `media_id`): un reinicio no publica dos
  veces. Si se pasó la hora por más de 15 min sin arrancar, queda "vencida" y no se publica sola.

### Copiar video (`servicios/fabrica.py`, ≈1600 líneas)
Flujo: `desde_links`/`hacer_guardados` → trabajo `descargar_ejemplo` (`material.bajar_video`, yt-dlp) →
`preparar_copia` (cuadros + pedido `lectura` a Claude con las imágenes) → `_leido` (normaliza con `_limpiar_campos`,
elige foto/video de la modelo con `_candidatas`/`_usadas` para no repetir) → `_auto` → `armar` → trabajo `render`
(`_render_ya`, ffmpeg) → pedido `juez` → `_juzgado_ya`. Cada copia vive en `/datos/copias/<id>/` con `copia.json`
(`_datos`), guarda hasta 15 versiones y avisa en el chat («🎬 Video listo…»). Estados: `bajando`, `leyendo`, `armando`
(activos), `lista` (leída: se puede armar o ya está armada) y `error`.

**Texto (lo más sensible, lo que más pidió el equipo):**
- `drawtext` de ffmpeg; tamaño `tam = int(h * texto_tam * ESCALA_TEXTO)` con `ESCALA_TEXTO = 1.2`; renglones al 72 %
  del ancho (`ANCHO_MAX`), interlínea 1.25.
- **La vista previa del panel usa exactamente el mismo cálculo** (`Math.floor(texto_tam*altoV*escala)*H/altoV`): si
  se toca uno, hay que tocar el otro. Verificado con un render real dentro del 1 %.
- Estilos `sombra | caja | contorno | nada`, colores en `COLORES`. `normalizar` valida (no fuerza) estilo y color.
- Edición a mano: mover/achicar con esquina, slider o A± pone modo manual (si no, el servidor re-ubicaba el texto).
- **Emojis dentro del texto** (PR #18): `emojis_del_texto` cambia cada emoji del pack (incluye secuencias ZWJ, VS16 y
  tonos de piel) por un carácter privado U+E000+; `_renglones` mide cada uno como n espacios; `_texto_filtros`
  escribe espacios y guarda `c["_emojis"]=[(clave,x,y,lado)]`; `_render_ya` los superpone como PNG. En el panel:
  `emoCargarMapa`/`emoPartir`/`.cp-emo`.

**Capas** (PR #15): capturas pegadas (`pegar` con `a_x0/a_y0/a_ancho`), chat y notificación (PNG de `chat_img`) y
stickers se mueven y achican en la vista previa (`cpCapas`, `cpCapaSet`, `cpCapaSacar`). `capa(cid, que, k)` sirve
los PNG. Stickers: `<nombre>.png` subido (sección `stickers` del almacén, máx. 8 por video, 512 px) o
`emoji:<código>` del pack. Los WebP del pack se pasan a PNG antes de ffmpeg (canal alfa).

**Audio**: `ejemplo | modelo | musica | ninguno`.

### Banco de memes y buscador (`fabrica.py` + `servicios/buscador.py`)
- `banco_links` con ficha (duración, miniatura, usuario) que arma el trabajo `fichas` (con candado, `socket_timeout`).
- **Sesión "para mirar"**: se pega en Memes → «Cargar sesión». `buscador.leer_cookies` acepta Netscape (tabs o
  espacios), la línea `Cookie: a=1; b=2` y el JSON de Cookie-Editor; exige `sessionid`. Se guarda en
  `/datos/navegador/cookies.txt` (0600). La API no abre navegador para esto (antes daba 500).
- **yt-dlp siempre recibe una copia** (`material.cookies_ytdlp()`) y los errores salen en criollo con
  `material.motivo_ytdlp()` (sesión vencida / sin sesión / fotos / privado). PR #19.
- Buscador: Chromium headless, `_cargar_sesion(ctx)`. Pantalla «Dónde busca» (fuentes con id y texto) y semillas
  desde el propio banco (`sembrar_desde_banco`).

### Chat con Claude
En el menú lateral («Chat con Claude»). Acepta hasta 4 fotos (compu, portapapeles o arrastrar), guardadas como JPG
de 1568 px máx. en la sección `chat` (`jueces.foto_chat`, `mensaje(texto, autor, fotos)`); el pedido `chat` se crea
con `imagenes=fotos`. PR #17.

### Noticias, Costos, Fuentes, Material, Mandar a trabajar
Ver `servidor/README.md` (secciones Espacios, Fuentes, Integración). Noticias: PR #12 y #13 (Bandeja en el menú de
Noticias para publicar los guiones aprobados). Voz y render de Noticias **no están hechos** (el guion aprobado queda
listo para esas etapas).

### API para terceros
Cualquier `/api/*` acepta `Authorization: Bearer hl_...`. OpenAPI en `/docs`. Webhooks firmados
(`X-Hookline-Firma: sha256=HMAC(secreto, "<X-Hookline-Tiempo>.<cuerpo>")`), eventos `pieza.*` y `cuenta.*`.

---

## 6. Tests y CI

- `cd servidor && python -m pytest -q` → **358 tests** (SQLite). Contra Postgres:
  `HOOKLINE_TEST_BD="postgresql+psycopg://usuario@host/base" python -m pytest -q`. Tarda 3-4 min.
- Incluye `test_romper.py` (hypothesis: datos basura contra cada endpoint, rutas, permisos, concurrencia, caídas de
  Meta y Claude), `test_regresiones.py`, la fábrica de punta a punta con ffmpeg real, y un archivo por cada ronda de
  feedback (`test_feedback_zergen.py`, `test_editor_manual.py`, `test_emojis_y_marca.py`, `test_chat_fotos.py`,
  `test_sesion_ytdlp.py`, `test_noticias.py`…).
- Necesita `ffmpeg` y fuentes (`fonts-inter fonts-dejavu-core`). Borrar `.hypothesis/` después.
- CI (`.github/workflows/servidor.yml`): tests en SQLite y Postgres + migraciones Alembic + build de Docker, en cada
  PR que toca `servidor/`.
- **Forma de trabajo que se usó y conviene mantener**: cada arreglo con un test que **falla con el código viejo**
  (`git stash -- hookline`, correr, `git stash pop`) y pasa con el nuevo; prueba en navegador real (Playwright) del
  flujo completo cuando hay UI; PR → CI verde → squash merge → deploy.

---

## 7. Historial (PRs mergeados en `main`)

| PR | Qué |
|---|---|
| #1 | Hookline como servicio (API + worker) + requerimientos de integración con Pyro |
| #2 | Ajustes: pegar la API key de Claude desde el panel |
| #3 | Login nuevo + botón «Probar conexión» con Claude |
| #4 | Conexión con Pyro: cuentas atadas a modelos, material desde Pyro, propuesta del puente |
| #5, #6 | Sección Fuentes (ver, subir, ocultar; catálogo grande; Claude elige la letra) |
| #7 | HTTPS con Caddy (link para cualquier celular) |
| #8 | Mensajes claros cuando Meta está mal configurado |
| #9 | Pyro: «En Pyro pero no en Hookline» al día |
| #10 | Largo mínimo de contraseña configurable |
| #11 | Login unificado de Meta (apps nuevas, `config_id`) |
| #12, #13 | Noticias: radar, guiones, jurado de 10, costos; Bandeja en Noticias |
| #14 | Feedback: cookies en 3 formatos, miniaturas del banco, error 400 de Copiar video (esquema demasiado complejo) |
| #15 | Editor manual: texto exacto, estilos y colores, capas movibles, stickers, «Dónde busca», chat en el menú |
| #16 | Pack de emojis 3D y «Usarlo igual» con marca de agua |
| #17 | Fotos en el chat con Claude |
| #18 | Emojis dentro del texto + vista previa que corta igual que el video |
| #19 | yt-dlp no borra más la sesión guardada; errores de descarga con el motivo real |

---

## 8. Incidentes y lecciones

1. **Todos los links daban «No se pudo bajar» (20/11).** Se cerró la sesión de Instagram "para mirar" (sus cookies
   se habían compartido por un grupo). yt-dlp detectó la sesión vencida, le sacó el `sessionid` y **reescribió**
   nuestro `cookies.txt`. Sin sesión, Instagram no da los `/p/` a la IP del droplet ("There is no video in this
   post") y el mensaje genérico escondía todo. Arreglo en #19. Lección: las cookies son un secreto; nunca por chat
   ni capturas; usar una cuenta secundaria.
2. **Error 400 en Copiar video** («Schema is too complex»): el esquema de `lectura` superaba los límites de structured
   outputs → `PARA_API`. Efecto colateral que hubo que arreglar: la nota de Claude salía dibujada como notificación
   (por eso los flags `hay` y la comparación `_parece`).
3. **El texto no quedaba donde se ponía a mano**: faltaba `ESCALA_TEXTO` en la vista previa, el redondeo de píxeles
   era distinto, y achicar no pasaba a modo manual. Hoy coinciden.
4. **Fichas trabadas en «cargando»**: trabajos concurrentes sin candado ni timeout.
5. **Fuzz encontró `color: []`** (TypeError unhashable) → validación con `isinstance` en `_limpiar_campos`.

---

## 9. Pendiente / abierto

- **Reportado por el equipo, sin datos todavía**: un video exportado **sin audio** y un **chat de WhatsApp de un meme
  copiado mal**. Se pidió el link del meme y captura; no llegaron. Empezar por ahí si vuelven a aparecer
  (`copia.json` de esa copia + `docker compose logs worker`).
- **Sesión "para mirar"**: hay que cargar cookies nuevas (las actuales quedaron sin `sessionid`). Sin sesión, los
  `/reel/` bajan pero los `/p/` no.
- **Puente de Pyro**: los patches de `propuesta-pyro/` (permiso `hookline`, migración 136, `hooklinebus`,
  `hooklineapp`, `docs/HOOKLINE.md`) **no están subidos a Pyro**: los revisa y sube el equipo de Pyro. El lado de
  Hookline (token `hl_`, webhooks, `pyro_modelo_id`) está hecho. Detalle en `INTEGRACION_PYRO.md`.
- **Secreto de la app de Meta**: rotarlo lo decide el devops de Pyro (Andre).
- **Meta**: App Review y verificación del negocio si se van a manejar cuentas de terceros.
- **Costo de IA**: idea no hecha: elegir modelo por etapa (Sonnet/Haiku para lo simple) para bajar el gasto.
- **Noticias**: etapas de voz y render.
- **Worker en una PC de la casa** (para que el buscador navegue con IP residencial): idea, no hecha.
- **Backups automáticos**: no hay (ver §3).
- `servidor/README.md` dice «~115 tests»: hoy son 358.

---

## 10. Para seguir trabajando

1. Leer este archivo, `servidor/README.md`, `servidor/CLAUDE.md` e `INTEGRACION_PYRO.md`.
2. Entorno local: `cd servidor && python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,buscador]"`,
   `export HOOKLINE_CLAVE_SECRETA=$(hookline generar-clave) HOOKLINE_CLAVE_SESION=algo-largo`,
   `hookline crear-usuario vos@ejemplo.com`, `hookline api` (http://localhost:8000) y `hookline worker` en otra terminal.
3. Rama → test que falle → arreglo → `pytest` (SQLite y Postgres) → prueba en navegador → PR → CI verde → squash
   merge → deploy (§3). Textos de la UI en español rioplatense («vos», «probá»).
4. Nunca: secretos en git o en chats, publicar sin aprobación, generar imágenes/videos con IA, aflojar la regla de
   menores, conectarse a Instagram desde la máquina de desarrollo con cuentas reales.
