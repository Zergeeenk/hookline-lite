# Requerimientos de integración: Hookline Lite → Pyro

> Documento para el agente que trabaja en el repo de Pyro. Asume que **nunca viste Hookline**.
> Todo lo que dice acá se verificó contra el código de este repo (commit `1be28d1`). Donde algo no se puede
> saber desde el código (por ejemplo, cómo es Pyro), está en **Decisiones abiertas** (§11).
> Las rutas `archivo.py:N` apuntan a la línea del código fuente.

---

## 1. Qué hace Hookline (en 5 líneas)

1. Es un **panel web local** (Python, sin framework, `http://127.0.0.1:8791`) para manejar **varias cuentas de Instagram** profesionales de "modelos" generadas con IA, con una sola app de Meta.
2. **Bandeja → jueces → aprobar → cola de publicación**: la dueña sube fotos o videos, Claude los puntúa y propone caption, hashtags y horario, y el panel publica a la hora elegida por la API oficial (Reels, fotos, carruseles e historias).
3. **"Fábrica" de reels**: toma memes o reels de otras cuentas (links de IG o TikTok), Claude lee el texto y el corte, ffmpeg arma un video nuevo con la foto de la modelo y el remate del original, un "juez" Claude lo revisa y se reparte solo entre las **multicuentas** de esa modelo.
4. **Buscador de memes**: un Chrome real con sesión iniciada recorre IG y TikTok con Playwright, junta miniaturas, y Claude filtra cuáles sirven para el banco de ejemplos.
5. **Lectura**: estadísticas (insights), comentarios con respuestas sugeridas (se mandan con aprobación), tendencias semanales por nicho y un chat con Claude. Las decisiones "de criterio" las toma **Claude Code corriendo en la misma PC**, a través de un puente de archivos (§7).

---

## 2. Módulos

| Módulo | Qué hace | Reusar tal cual | Reescribir | Depende de Windows / PC local |
|---|---|---|---|---|
| `panel.py` (1562 líneas) | Servidor `ThreadingHTTPServer` + API `/api/*` + 3 hilos de fondo: `bucle_rapido` cada 4 s, `bucle_cola` cada 30 s y `bucle_sincronizar` cada 1 h (`panel.py:1033-1073`). Bandeja, cola, repartir/encargo, comentarios, tendencias, puente con Claude. | Las reglas de negocio: `validar_tipo`, `accion_pieza`, `revisar_cola` (anticipo y vencidas), `repartir`, `calentamiento`, `mejores_horarios`. | El servidor HTTP (pasar a FastAPI/Flask), el almacenamiento JSON y los bucles con `sleep` (pasar a un scheduler o una cola de trabajos). | `os.startfile` y `explorer /select` (`panel.py:1379,1411,1484`), `SO_EXCLUSIVEADDRUSE` (protegido con `hasattr`), `USERPROFILE/.vscode` para detectar Claude. |
| `instagram_api.py` (673) | Cliente de la "Instagram API with Instagram Login": OAuth, refresco de token, perfil, medios, insights, comentarios, publicar (resumable o por URL), túnel de Cloudflare. Solo usa `urllib`. | **Casi todo**: `url_login`, `canjear_codigo`, `Cuenta._access` (refresco), `publicar`, `insights` (descubre qué métricas acepta), `comentarios`, `responder`, `ocultar`, `limite`. | La persistencia (hoy recibe `leer`/`escribir` de archivos JSON) y `Tunel`, si Pyro tiene URL pública. | `buscar_cloudflared` busca `.exe` en rutas de Windows (en Linux también usa `shutil.which`). `CREATE_NO_WINDOW` está protegido con `getattr`. |
| `copiar.py` (1149) | "Copiar video": baja el ejemplo, arma la hoja de cuadros, pide la lectura a Claude, elige la foto de la modelo, detecta caras (OpenCV Haar), ubica el texto, arma el filtro ffmpeg (texto, capturas pegadas, chat dibujado, borrado de marcas de agua, audio) y pide el juez con hasta 2 reintentos. | La lógica del filtro ffmpeg y `ubicar`/`_caras`. | Los hilos y semáforos (máximo 3 descargas y 2 armados a la vez, `copiar.py:140-141`) pasan a workers. | **Fuentes en `C:\Windows\Fonts`** (`copiar.py:21`): Segoe UI, Impact, Georgia, etc. En Linux el `drawtext` de ffmpeg falla porque el fallback también es `segoeuib.ttf`. Hay que empaquetar una fuente (por ejemplo, Montserrat SemiBold, como pide `buscador/estilo.md`). |
| `chat.py` (151) | Dibuja con Pillow capturas de WhatsApp (modo oscuro) y notificaciones de Instagram. | Sí. | — | Fuentes de `C:\Windows\Fonts` (si no están, usa `load_default`, que se ve muy mal). |
| `medios.py` (156) | Miniaturas, cuadros para los jueces, duración, JPEG para IG (4:5 a 1,91:1 con fondo desenfocado, historias 9:16, carrusel con la misma proporción). | Sí. | — | No (ffmpeg sale de `imageio-ffmpeg`). |
| `material.py` (242) | Carpetas de material por modelo, etiquetas, descarga de TikTok/IG con `yt-dlp` y extracción de audio. | Sí. | El almacenamiento (hoy es el sistema de archivos más `material.json`). | No. Usa `cookies.txt` del buscador para IG. |
| `buscador.py` (378) | Lanza **Google Chrome real** con perfil propio y `--remote-debugging-port=9333`, se conecta por CDP con Playwright, scrollea búsquedas y cuentas, baja miniaturas, arma hojas de 40, pide el filtro a Claude, aprende qué fuentes rinden y exporta cookies. | La parte de stats y fuentes. | Todo lo de navegador, si es que se mantiene (ver riesgos §11). | Rutas de `chrome.exe` fijas de Windows (`buscador.py:19-21`). Necesita una sesión de IG o TikTok iniciada a mano. |
| `respuestas.py` (123) | Prompt de clasificación y respuesta de comentarios, llamada a **`claude -p --model haiku`** (CLI de Claude Code, sin herramientas) y reglas fijas de revisión (regex de palabras prohibidas, repetidas). | `PROMPT`, `leer_items`, `revisar`. | `sugerir_haiku` pasa a la Messages API. | Busca `claude.exe` en `.vscode/extensions`. |
| `bandeja.py` (100) | Ícono en la bandeja de Windows (pystray) que mantiene vivo el panel. | — | Se descarta. | **Solo Windows.** Además está **huérfano**: ningún `.bat` lo usa y `pystray` no está en `requirements.txt`. |
| `panel/index.html` (1906 líneas, 205 KB) | SPA en JS puro, sin build. 11 páginas (Inicio/fábrica, Bandeja, Material, Memes/Inspiración, Copiar video, Calendario, Tendencias, Estadísticas, Comentarios, Cuentas, Ajustes). Consulta `GET /api/estado` **cada 4 s** (`index.html:1903`). Usa Google Fonts (Inter). | Sirve como referencia funcional o de UX. | Lo más probable es que se rehaga con la UI de Pyro. | No. |
| `.claude/agents/*.md` | Prompts de los 3 subagentes: `jueces-cm`, `tendencias-cm` y `buscador-memes`. | Los prompts y los esquemas de salida. | La forma de invocarlos (§7). | Asumen Claude Code local. |
| `CONTEXTO.md` / `CLAUDE.md` | Instrucciones de Claude Code: qué hacer con cada pedido, reglas de seguridad y formato de los JSON de salida (`lectura.json`, `juicio.json`, `filtro-k.json`). | Son la **especificación** de los pedidos. | Pasan a system prompts. | — |
| `*.bat`, `herramientas/` | Instalador (pip, cloudflared y acceso directo) y lanzador (Edge o Chrome `--app`). `puente-instagram.html` reenvía el callback OAuth a localhost. | — | Se reemplazan por Docker o un deploy. | Solo Windows. |

---

## 3. Dependencias

**Python**: 3.11+ (el instalador pide 3.12). Usa `datetime.fromisoformat` y `str | None` no aparece, así que 3.10 probablemente también funcione, pero no se probó.

**Paquetes** (`requirements.txt`): `Pillow>=10`, `imageio-ffmpeg>=0.5` (trae su propio binario de ffmpeg), `yt-dlp>=2026.10`, `opencv-python-headless>=4.8` (trae numpy), `playwright>=1.50`.
- Opcional: `pillow-heif` (fotos `.heic`; lo instala `Instalar.bat` aparte).
- No declarado: `pystray` (solo lo usa `bandeja.py`, que está huérfano).
- La parte de Instagram usa solo la librería estándar (`urllib`).

**Binarios**:
- ffmpeg: se usa `imageio_ffmpeg.get_ffmpeg_exe()` o, si no está, el del PATH. Tiene que tener `drawtext` (libfreetype).
- `cloudflared`: quick tunnel `*.trycloudflare.com`, sin cuenta. Solo hace falta si no hay URL pública (§6).
- **Google Chrome** instalado (no el Chromium de Playwright: `buscador.py` lanza `chrome.exe` y se conecta por CDP).
- **Claude Code** (CLI `claude` o la extensión de VS Code) con sesión iniciada en un plan de suscripción. `LEEME.md` dice "Pro $20" y `CONTEXTO.md` dice "plan Max": hay una inconsistencia en los documentos.

**Navegador del usuario**: cualquiera para el panel. El lanzador prefiere Edge o Chrome en modo `--app`.

**Fuentes**: hoy se toman de `C:\Windows\Fonts`. En servidor hay que **empaquetarlas** (§9).

---

## 4. Datos y almacenamiento

Todo está en archivos JSON con escritura atómica (`.tmp` + `os.replace`, `panel.py:70`) y un `RLock` global del proceso. **No hay base de datos.** La raíz de datos se puede mover con `HOOKLINE_DATOS` (`panel.py:36`). Las fechas son **ISO locales sin zona horaria** (`datetime.now()`), y se asume la hora de Argentina.

### 4.1 `datos/`
| Archivo | Contenido / esquema |
|---|---|
| `config.json` | `{app_id, app_secret, redirect, cloudflared, version}`. **App Secret de Meta en texto plano.** |
| `chat.json` | Lista (últimos 400) de `{id:int, de:"vos"\|"pedido"\|"claude"\|"panel", texto, hora:"dd/mm HH:MM", tipo_pedido?, pieza?, cuenta?, copia?, lote?}`. **También es la cola de pedidos a Claude** (§7). |
| `.visto` | Último `id` de chat entregado a Claude. |
| `puente.json` | `{escuchando: epoch, pid, entregado: epoch}`. Es el latido del puente. |
| `piezas.json` | La bandeja y la cola. Lista de piezas (ver 4.4). |
| `actividad.json` | Últimos 300 `{t, tipo, texto, img, cuenta, copia}` (lo que muestra la fábrica en vivo). |
| `encargo.json` | `{dia, reels, historias, estado:"trabajando"\|"listo"\|"incompleto", creado, pedidos:{modelo:n}}`. |
| `cookies.txt` | **Cookies de sesión de IG y TikTok** exportadas del Chrome del buscador (formato Netscape, para yt-dlp). |
| `navegador/` | Perfil completo de Chrome del buscador (con la sesión iniciada). |
| `jueces/<pieza>/` | `pedido.json`, `parteN/*.jpg` (cuadros) y `veredicto.json` (lo escribe el agente). |
| `respuestas/` | `<cuenta>_pedido.json` y `<cuenta>.json` (respuesta de Claude). |
| `salida/<pieza>/` | JPEG convertidos para IG (temporales mientras se publica). |
| `miniaturas/`, `claude_tmp/`, `panel.log` | Caché, carpeta de trabajo de `claude -p` y log del modo `fondo`. |

### 4.2 `cuentas/<usuario>/`
| Archivo | Esquema |
|---|---|
| `cuenta.json` | `{usuario, nombre, nicho, tono, creada, main?, apodo?, activa_desde?, tendencias_pedido?}`. **`main`** = la cuenta "modelo" de la que es multicuenta (comparten Material). |
| `token.json` | `{access_token, user_id, ig_id, permisos:"csv", obtenido, refrescado, vence (epoch), vencido?}`. **Secreto.** |
| `estado.json` | `{usuario, nombre, tipo_cuenta, foto, seguidores, siguiendo, publicaciones, ultima, error, aviso, cuenta_28d:{views,reach,total_interactions,accounts_engaged}, limite:{usadas,total,fecha}, metricas_ok:{reel:[],post:[]}, aviso_resumable}`. |
| `medios.json` | `{fecha, medios:[{id, caption, tipo, media_type, reel, url, publicado, miniatura, likes, comentarios, vistas, alcance, guardados, compartidos, interacciones}]}`. Hasta 100 medios; insights de los primeros 40. |
| `historial.json` | Lista de `{fecha, seguidores, vistas, publicaciones}`, una por hora (máximo 2880). |
| `comentarios.json` | `{fecha, sugiriendo, items:{<id>:{id, media, usuario, texto, fecha, likes, respuestas, respondido_por_mi, post, post_texto, miniatura, estado:"nuevo"\|"respondido"\|"oculto"\|"saltado", clase, sugerencia, alerta, respuesta_enviada, respondido}}}`. |
| `tendencias.json` | `{fecha, nicho, items:[{idea, formato, audio, porque, como, link}]}` (lo escribe `tendencias-cm`). |

### 4.3 Archivos de medios
- `Para subir/<cuenta>/`: lo que entra a la bandeja. Al publicar, el archivo pasa a `_publicados/`; al borrar la pieza, a `_descartados/`.
- `Material/<modelo>/{modelo, ejemplos, musica, <carpetas propias>}/` más `material.json` = `{"sec/archivo": {etiquetas[], link, autor, titulo, tema, musica, bajado, de}}`. `Material/_general/general/` es compartido. Las carpetas `"Salidas y comida"`, `"Día a día"` e `historias` alimentan las historias. Las demás (menos ejemplos, música y general) alimentan los reels (`panel.py:99-109`). La etiqueta `noche` decide el horario.
- `Material/_copias/<id 8 hex>/`: `copia.json`, `ejemplo.mp4`, `hoja.jpg`, `modelo-*.ext`, `audio-*.ext`, `resultado-<ts>.mp4` (máximo 15 versiones), `revision.jpg`, más `lectura.json` y `juicio.json` cuando los escribe Claude.
- `Material/_copias/` también tiene: `_links.json` (banco de links: `{links:[{link, agregado, copia, titulo, cat, ficha, dur, autor, titulo_orig, mini, error, origen, voto:"oro"|"bueno"|"malo", motivo}]}`), `_minis/`, `_previas/`, `_candidatos/<lote>/` (`candidatos.json`, `hoja-k.jpg`, `filtro-k.json`), `_busqueda.json`, `_fuentes_stats.json`, `_fotos_<cuenta>.jpg/.json` y `_nuevos_links.txt` (lo que deja `buscador-memes`).
- `Resultados/`: copia de cada reel armado con un nombre legible ("para mandar por WhatsApp").

### 4.4 Pieza (`datos/piezas.json`)
```
id "p<epoch_ms>", cuenta, tipo: historia|reel|carrusel|foto, archivos: [ruta relativa], creado, caption (≤2200),
cuando "AAAA-MM-DDTHH:MM", ahora: bool, estado, etapa (texto de progreso), error,
jueces: {veredicto.json + promedio}, sugerido, pedido_jueces, aprobada, inicio,
media_id, permalink, publicado, subida: "directa"|"tunel",
plan: bool, dia, copia (id de la copia de origen), foto (material usado)
```
Estados: `nueva → jueces → juzgada → programada → subiendo → publicada`. También existen `vencida`, `error` y `planificada` (ver §11: `planificada` nunca pasa a `programada` sola).

Copia (`copia.json`), estados: `bajando → leyendo → lista → armando → lista(+resultado)` o `error`. El juez va aparte: `juez.estado: mirando|aprobado|rechazado|rehaciendo`, con `intentos`.

### 4.5 Propuesta de base de datos (Postgres)
- `cuentas` (id, usuario, nombre, nicho, tono, main_id FK, activa_desde, creada)
- `credenciales_ig` (cuenta_id, access_token **cifrado**, ig_user_id, permisos, obtenido, refrescado, vence, vencido). Se puede guardar en un vault y dejar en la tabla solo la referencia.
- `estado_cuenta` / `snapshots_cuenta` (cuenta_id, fecha, seguidores, vistas, publicaciones, insights_28d JSONB)
- `medios_ig` (id IG, cuenta_id, tipo, caption, permalink, publicado_at, miniatura, métricas…)
- `comentarios` (id IG, cuenta_id, media_id, usuario, texto, fecha, estado, clase, sugerencia, alerta, respuesta_enviada)
- `piezas` (id, cuenta_id, tipo, estado, caption, programada_para **timestamptz**, ahora, etapa, error, veredicto JSONB, media_id, permalink, plan, dia, copia_id) + `pieza_archivos` (pieza_id, orden, archivo_id)
- `archivos` (id, cuenta/modelo, sección, nombre, storage_key (S3/GCS), tipo, etiquetas[], meta JSONB)
- `copias` (id, cuenta_id, link, estado, campos de lectura JSONB, juez JSONB, resultado_archivo_id) + `copia_versiones`
- `banco_links` (link único, título, categoría, voto, motivo, autor, mini, copia_id, origen)
- `fuentes_busqueda` (red, consulta, vistos, sirven)
- `tareas_ia` (id, tipo: jueces|tendencias|respuestas|copiar|filtrar|juez|chat, payload JSONB, estado, resultado JSONB, creado, terminado) **← reemplaza el puente de archivos**
- `chat_mensajes`, `actividad`, `encargos`
- `config_app` (app_id; el app_secret va al vault)

Los archivos pesados (videos, fotos, cuadros) van a object storage, no a la base.

---

## 5. API HTTP del panel (`panel.py:1202-1494`)

Sin autenticación. Escucha **solo en `127.0.0.1`**. Respuesta JSON; los errores son `400 {"error": "..."}` (ValueError/RuntimeError o token vencido), `404` y `500 {"error":"Error inesperado: ..."}`. El cuerpo POST se parsea como JSON **sin mirar el Content-Type**.

### GET
| Ruta | Entrada | Salida |
|---|---|---|
| `/`, `/index.html` | — | `panel/index.html` |
| `/api/estado` | — | `{cuentas[], piezas[] (+vistas_previas), chat (últimos 80), claude:{instalado,cli,extension,puente:"escuchando"\|"trabajando"\|"apagado",ultimo}, app:{configurada,cloudflared,ffmpeg}, ahora, carpeta}` |
| `/api/ajustes` | — | `{app_id, tiene_secret, redirect, redirect_defecto, cloudflared, ffmpeg, carpeta, version}` (nunca devuelve el secret) |
| `/api/stats` | `?cuenta` | `{estado, historial (últimos 1440), medios (60), horarios:{mejores[5], por_hora[], con_datos}}` |
| `/api/calendario` | `?cuenta` (`""`/`todas`/id) | `{medios:[{cuenta,id,publicado,tipo,url,miniatura,vistas,caption}]}` |
| `/api/comentarios` | `?cuenta` | contenido de `comentarios.json` |
| `/api/tendencias` | `?cuenta` | contenido de `tendencias.json` o `{items:[]}` |
| `/api/actividad` | — | `{items (60, de la más nueva a la más vieja), encargo}` |
| `/api/copias` | — | `{items: copias[] (con *_url), opciones:{fuentes,colores,...}, links: banco[], busqueda: estado del buscador}` |
| `/api/modelos` | — | `{modelos:[{id,usuario,videos,fotos,carpetas,multicuentas,ultima,foto}]}` (solo cuentas main) |
| `/api/material` | `?cuenta` | `{<sección>:[{nombre,archivo,tipo,fecha,etiquetas,...}], bajando:[]}` |
| `/mini`, `/media` | `?p=<ruta relativa>` (solo dentro de `Para subir/` o `Material/`) | miniatura JPEG / archivo con soporte de `Range` |
| `/jueces-cuadro` | `?p` (dentro de `datos/jueces/`) | JPEG |
| `/instagram/conectar` | — | 302 a la página de OAuth de Instagram |
| `/instagram/callback` | `?code&state` o `?error` | HTML "conectada" / error |

### POST
| Ruta | Entrada | Salida |
|---|---|---|
| `/api/subir` | `?cuenta&nombre`, cuerpo = **bytes crudos** (≤1 GB) | `{ok, archivo}`. Lo guarda en `Para subir/<cuenta>/` y escanea. |
| `/api/material_subir` | `?cuenta&sec&nombre`, bytes | `{ok, archivo}` |
| `/api/copiar_subir` | `?que=ejemplo\|audio\|modelo&id&nombre&cuenta`, bytes | `{ok, id}` (ejemplo) / `{ok, archivo}` |
| `/api/copiar` | `{op, id?, ...}`. Las ops son: `link`{link,cuenta}→`{id,ids}` · `cancelar`→`{n}` · `guardar_links`{link}→`{n}` · `buscar` · `abrir_navegador` · `agregar_encontrados`{items[{link,img,titulo,cat}]}→`{n}` · `votar`{link,voto,motivo} · `previa`{link}→`{url}` · `borrar_link`{link} · `hacer_guardados`{n,cuenta}→`{ids}` · `armar`{id,+campos} · `borrar`{id} · `modelo_material`{id,cuenta,sec,nombre} · `audio_material`{id,cuenta,sec,nombre,titulo} · `ubicar`{id,+campos}→`{texto_y,texto_x,texto_tam,tapa_cara,caras}` · `borrar_version`{id,archivo} · `a_bandeja`{id,cuenta,archivo} · `carpeta`{id} (abre el Explorador) | `{ok,...}` |
| `/api/material` | `{cuenta, op}`. Las ops son: `nueva_carpeta`{nombre} · `bajar`{link}→`{id}` · `etiquetar`{sec,nombre,etiquetas[]} · `descartar`{id} · `borrar`{sec,nombre} · `a_bandeja`{sec,nombre} · `carpeta`{sec} (abre el Explorador) | `{ok,...}` |
| `/api/chat` | `{texto}` | el mensaje creado |
| `/api/repartir` | `{dia:"AAAA-MM-DD"}` | `{ok, reels, historias, faltan[]}` |
| `/api/encargo` | `{dia, reels(0-6), historias(0-6)}` | igual que repartir; además pide reels nuevos a la fábrica |
| `/api/pieza` | `{op, id, ...}`. Las ops son: `jueces` · `juntar`{ids[]} (carrusel) · `tipo`{tipo} · `orden`{archivos[]} · `separar`{archivo} · `caption`{caption} · `programar`{caption?, cuando? (vacío = ahora)} · `cancelar` · `borrar` | la pieza / `{ok}` |
| `/api/comentario` | `{cuenta, op, comentario?, texto?}`. Las ops son: `actualizar`→`{nuevos}` · `sugerir`→`{via:"haiku"\|"chat"\|"nada", n}` · `responder`{texto} · `ocultar` · `saltar` | `{ok,...}` |
| `/api/tendencias` | `{cuenta}` | `{ok}` (deja el pedido a Claude) |
| `/api/ajustes` | `{app_id?, app_secret?, redirect? (https://), cloudflared?}` | igual que el GET de ajustes |
| `/api/conectar` | `{url}` (la dirección completa del callback pegada a mano) | `{ok, usuario, tipo}` |
| `/api/conectar_url` | — | `{url}` de login |
| `/api/cuenta` | `{id, op}`. Las ops son: `crear_local`{id, main?} · `guardar`{nicho,tono,apodo,activa_desde} · `sincronizar` · `desconectar` (borra `token.json`) · `carpeta` (abre el Explorador) | `{ok,...}` |

Límites que valida el servidor: caption ≤ 2200, ≤ 30 hashtags, reel = 1 video, foto = 1 foto, historia = 1 archivo, carrusel = 2-10, y no se programa a una hora que ya pasó.

---

## 6. Instagram

- **API**: *Instagram API with Instagram Login* (`graph.instagram.com`, versión `v26.0`, configurable con `config.version`). No usa la Facebook Login / Graph API de páginas. Cuentas **Business o Creator**. Una sola app de Meta para todas las cuentas.
- **Permisos** (`instagram_api.py:40`): `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_comments`, `instagram_business_manage_insights`.
- **Modo de la app**: desarrollo, con cada cuenta agregada como **Instagram Tester** (no hace falta App Review). En Pyro, si van a ser cuentas de terceros o muchas cuentas, va a hacer falta **App Review y verificación del negocio** (§11).
- **OAuth** (verificado):
  1. `GET https://www.instagram.com/oauth/authorize?client_id&redirect_uri&response_type=code&scope&state&force_reauth=true`. El `state` se guarda **en memoria** (`_states`, se pierde si el panel se reinicia) y expira en 1 h.
  2. `POST https://api.instagram.com/oauth/access_token` (code → token corto). Se le recorta el `#_` final al code.
  3. `GET graph.instagram.com/access_token?grant_type=ig_exchange_token` → **token largo de 60 días**. Se guarda `vence = ahora + expires_in − 1 h`.
  4. `GET /me` → crea `cuentas/<username>/`.
- **Redirect URI**: el código usa por defecto **`https://localhost:8791/instagram/callback`** (`panel.py:279`). El borrador decía 8770 y estaba mal; `CONECTAR-INSTAGRAM.md` coincide con el código. Como el panel sirve **HTTP**, el navegador muestra "No se puede acceder" y la usuaria **copia y pega la URL** en `/api/conectar`. Alternativa: hospedar `herramientas/puente-instagram.html` en HTTPS para que reenvíe a `http://localhost:8791/instagram/callback`. **En Pyro: un callback HTTPS real del backend.**
- **Renovación del token** (`Cuenta._access`, `instagram_api.py:290-321`). Es *lazy*: ocurre dentro de cualquier llamada a la API. Se refresca con `GET /refresh_access_token?grant_type=ig_refresh_token` si el token tiene más de 24 h desde `obtenido` **y** además (pasaron más de 5 días desde el último refresco **o** faltan menos de 20 días para que venza). Si ya venció o el refresco devuelve 190, queda `vencido: true` y se borra el token: hay que reconectar. Como `bucle_sincronizar` llama a la API cada hora por cuenta conectada, el token se renueva solo **mientras el panel esté prendido**. En Pyro conviene un job diario explícito de refresco.
- **Publicar** (`Cuenta.publicar`):
  - Antes de publicar consulta `content_publishing_limit` (cuota de 24 h; por defecto 100).
  - **Reel o historia con video**: primero prueba la **subida resumable** (`upload_type=resumable` + POST del archivo local a `rupload.facebook.com` con `Authorization: OAuth`). Si falla, sirve el archivo por **túnel cloudflared** y usa `video_url`.
  - **Foto, historia con foto y carrusel**: **siempre por URL pública** (túnel). Instagram baja el archivo.
  - Espera el contenedor (`status_code`) cada 10 s, hasta 20 min. Después llama a `media_publish` y luego `GET permalink,timestamp`.
  - Reels con `share_to_feed=true`. Las historias van sin caption. Solo se aceptan fotos en JPEG (el panel convierte).
  - **Programación**: la API no se usa para programar. El panel arranca el proceso **8 min antes** (`ANTICIPO_S`), deja el contenedor listo y hace `sleep` hasta la hora exacta. Se publica **de a una pieza a la vez en todo el sistema** (`_publicando` lock). Si el panel estuvo apagado y se pasaron más de 15 min, la pieza queda **`vencida`** y no se publica sola.
- **Lectura**: `/me`, `/{ig_id}/media` (2 páginas × 50), `/{media}/insights` con `views, reach, likes, comments, saved, shares, total_interactions` (si la API rechaza alguna métrica, las prueba de a una y guarda las que acepta), `/{ig_id}/insights` de 28 días (`views, reach, total_interactions, accounts_engaged`) y comentarios (los 10 medios con comentarios × 50).
- **Comentarios**: `POST /{comment_id}/replies` (message ≤ 2200) y `POST /{comment_id}?hide=true`.
- **Límites y restricciones** que tiene en cuenta el código o la documentación: 100 publicaciones por API cada 24 h por cuenta; Reels de hasta 15 min y 300 MB; carrusel de 2 a 10; la música tiene que venir ya en el video; las historias por API van sin stickers ni links; con cuentas Creator, las historias pueden fallar. Los errores de Meta se reducen a código y mensaje, sin cuerpo ni URL, para que nunca se filtre el token (`instagram_api.py:60-74`).

---

## 7. Integración con Claude Code (el "puente")

### Cómo funciona hoy
1. La dueña abre VS Code en la carpeta y le escribe "arrancá" a Claude Code. Claude corre en segundo plano **`py panel.py esperar`**.
2. `cmd_esperar` (`panel.py:175`) **consulta `datos/chat.json` cada 2 s**. Cada 15 s escribe el latido `puente.json.escuchando`. Cuando aparecen mensajes con `de ∈ {"vos","pedido"}` y `id > .visto`, los imprime (`"La dueña dice: …"` o `"PEDIDO DEL PANEL: …"`) y termina.
3. Claude atiende el pedido y **vuelve a lanzar `esperar`**. Al chat responde con **`py panel.py responder "texto"`**, que agrega un mensaje `de:"claude"`. A los pedidos responde **escribiendo un archivo JSON en la ruta que indica el pedido**.
4. El `bucle_rapido` del panel (cada 4 s) **levanta esos archivos**: `recoger_veredictos`, `recoger_respuestas`, `COP.recoger` (`lectura.json`, `juicio.json`, `_nuevos_links.txt`) y `BUS.recoger` (`filtro-k.json`).
5. Estado del puente: `escuchando` si hubo latido hace menos de 60 s, `trabajando` si entregó algo hace menos de 30 min, si no `apagado` (aparece un cartel rojo en el panel).

### Tipos de pedido
| Pedido | Lo genera | Quién lo hace | Entrada | Salida (archivo que se levanta) |
|---|---|---|---|---|
| `jueces` | botón "Pedir a los jueces" (`panel.py:527`) | subagente **`jueces-cm`** | `datos/jueces/<id>/pedido.json` con `{pieza, cuenta, nicho, tono, tipo, seguidores, texto_actual, partes[{archivo,es_video,duracion_s,cuadros[]}], ahora, mejores_horarios, ya_programado, escribir_en}`. Videos: 5 cuadros (0,3 s, 2 s, 40 %, 65 % y 90 %). | `veredicto.json`: `{jueces[3]{nombre,nota 1-10,comentario,riesgo?}, veredicto:"aprobar"\|"cambiar", resumen, sugerencias[], caption, hashtags[], horario{cuando,motivo}}`. Se aprueba si el promedio es ≥ 7 y Seguridad ≥ 7. |
| `tendencias` | botón o automático cada 7 días por cuenta con nicho, de a una por hora y solo si el puente escucha | subagente **`tendencias-cm`** (WebSearch y WebFetch, 4-8 búsquedas) | cuenta, nicho y ruta | `cuentas/<id>/tendencias.json` (5-8 items) |
| `respuestas` | "Sugerir respuestas" | **primero `claude -p --model haiku`** sin herramientas (`respuestas.py:91`). Si falla o no hay CLI, va como pedido al chat y lo hace Claude directo. | máximo 30 comentarios nuevos; el prompt está en `respuestas.py:28` | `{"items":[{id, clase: positivo\|neutro\|pregunta\|acoso\|insulto\|spam, respuesta}]}`, que después pasa por las reglas de `revisar()` |
| `copiar` | se arma una copia nueva (`copiar.py:467`) | Claude directo (visión) | `hoja.jpg` (cuadros cada ~0,5 s con el segundo marcado) + `_fotos_<cuenta>.jpg` (fotos numeradas de la modelo, máximo 100) | `lectura.json`: `{corte, texto, texto_y, texto_tam, modelo:n, notas, chat?{mensajes[],x0,x1,y0}, pegar?[{x0,y0,x1,y1,cuadro,a_y0}], marcas?[...], noti?, textos?, recorte_remate?}` o **`{"descartar":"menor"}`** |
| `juez` | después de cada armado automático (`copiar.py:1041`) | Claude directo (visión) | `revision.jpg` (8 cuadros con las líneas de zona de IG al 11 % y 81 %) + `hoja.jpg` | `juicio.json`: `{nota_texto 1-5, nota_tecnica 1-5, motivo, arreglo?: otra_foto\|texto\|musica, foto?, texto_y?}`. Cuenta la nota más baja. Si es menor a 3, se rehace hasta 2 veces. |
| `filtrar` | buscador (`buscador.py:273`) | Claude directo (visión) | `hoja-k.jpg` (40 miniaturas) + `candidatos.json` + `buscador/formatos.md` | `filtro-k.json`: `{sirven:[{n,cat,titulo}], fuentes_nuevas?:[...]}` |
| chat | `/api/chat` | Claude directo | texto | `panel.py responder` |

**`buscador-memes`** es el tercer agente. **El panel no lo dispara nunca**: se lanza a mano en Claude Code, hace búsquedas en la web, agrega renglones `URL | título | categoría` a `Material/_copias/_nuevos_links.txt`, y además **edita `buscador/formatos.md`** (agranda el catálogo y la sección "Lo que aprendí de sus votos").

### Cómo quedaría en Pyro
Recomendación: **reemplazar el puente por la Claude API del lado del servidor**, con una tabla o cola `tareas_ia` y workers. Motivos: el puente necesita una PC con VS Code abierto, un humano que escriba "arrancá", un plan de suscripción personal (los términos de uso de Claude.ai/Claude Code no están pensados para backend automatizado multi-cliente), y se cae en silencio.

| Pedido | Llamada sugerida |
|---|---|
| jueces | Messages API con imágenes (base64 o Files API) + **structured outputs** (`output_config.format` con el esquema de `veredicto.json`). Modelo `claude-opus-5-5`. |
| tendencias | Messages API + server tool **`web_search_20260209`** (y `web_fetch_20260209`) con `max_uses` ≈ 8. Puede ir por **Batch API** (50 % más barato; no es urgente). |
| respuestas | Messages API con `claude-haiku-5-5`, structured output `{items[]}`. Hay que mantener `revisar()` como filtro determinístico. |
| copiar / juez / filtrar | Messages API con visión + structured outputs. Los esquemas ya están definidos en `CONTEXTO.md` y en los prompts de `copiar.py`/`buscador.py`. |
| chat | Messages API con contexto de la cuenta (o tool use para consultar piezas y medios). |

Notas técnicas para quien lo implemente: SDK oficial `anthropic` (Python); manejar `stop_reason == "refusal"` (este contenido es "subidito" y puede gatillar rechazos de seguridad: hay que tener una política para eso). Con `claude-opus-5-5` no se fuerza `tool_choice` (devuelve 400): usar structured outputs. Usar prompt caching para los system prompts largos (`formatos.md`, reglas de seguridad). Los IDs de modelo y features están verificados con la documentación de la API vigente (noviembre de 2026); conviene consultar `GET /v1/models` al implementar.

**Decisión abierta (§11):** si Pyro mantiene Claude Code local (por ejemplo, por costos), el contrato para conservar es el de la tabla de arriba (archivo de pedido → archivo de salida), pero con una cola real en lugar de `chat.json`.

---

## 8. Seguridad

### Dónde viven los secretos hoy
- `datos/config.json`: `app_secret` de Meta **en texto plano**.
- `cuentas/*/token.json`: tokens de IG de 60 días **en texto plano**.
- `datos/cookies.txt` y `datos/navegador/`: **sesión completa de IG y TikTok** de la cuenta "para mirar" del buscador.
- El `.gitignore` excluye todo eso (y además `*.env`, `*client*.json`, `cookies*.txt`). **En el repo no hay secretos** (se verificó: solo hay `cuentas/ejemplo/cuenta.json` vacío).

### Problemas que se encontraron en el código
1. **El panel no tiene autenticación ni protección CSRF.** Está atado a `127.0.0.1`, pero `self.json()` parsea el cuerpo sin mirar el `Content-Type`, así que cualquier página web que la usuaria abra puede mandar un `POST` "simple" (`text/plain`) a `http://localhost:8791/api/pieza` con `{"op":"programar",...}` y **publicar**, o cambiar `redirect` en `/api/ajustes`. También es vulnerable a DNS rebinding (no valida el `Host`). En Pyro esto **no se puede copiar**: hace falta sesión, CSRF y validación del origen.
2. `absoluta()` y `/mini` validan con `str.startswith` sobre la ruta (`panel.py:93-95`). Un directorio hermano con el mismo prefijo pasaría. Hay que usar `Path.is_relative_to`.
3. El `state` de OAuth está en memoria, y si `state` llega vacío se acepta igual (`panel.py:294`: `if state and state not in _states`). Eso deja la puerta abierta a login CSRF. En Pyro, `state` tiene que ser **obligatorio** y persistido.
4. Los tokens van como `access_token` en la query string de los GET (es lo que pide Meta). Hay que asegurarse de que los logs de Pyro y del proxy **no guarden las URLs**.

### Cómo debería ser en Pyro
- App ID: configuración normal. **App Secret: vault** (o secret manager del cloud) o, como mínimo, una variable de entorno; **nunca en el repo, la base en claro ni los logs**.
- Tokens por cuenta: tabla con **cifrado a nivel de aplicación** (KMS / envelope) o en el vault. El refresco lo hace un job.
- API key de Anthropic: variable de entorno o vault.
- Cookies de sesión del buscador: si se mantiene esa función (§11), en un vault y aisladas por worker.
- Hay que mantener la sanitización de errores de Meta (`_error_http`) y la regla de nunca loguear tokens.

### Reglas de producto que se tienen que mantener (están en el código y en los prompts)
- **Menores:** el filtro, la lectura (`{"descartar":"menor"}`) y los jueces descartan **ante la mínima duda**. Hay que mantenerlo y, en lo posible, **hacerlo cumplir del lado del servidor** (que un descarte bloquee la copia y no dependa solo del prompt).
- **Publicar solo con aprobación y solo por la API oficial** (ver §11: el "encargo" hoy se saltea la aprobación por pieza).
- Nada de desnudez ni lenguaje sexual explícito, ni links a plataformas para adultos, ni "escribime al privado" (`CONTEXTO.md`, regex `PROHIBIDO_RESP` en `respuestas.py:22`).
- Las respuestas a comentarios siempre las aprueba un humano. Lo agresivo o el spam se sugiere ocultar, no contestar.

---

## 9. Lo que asume Windows / PC local, y cómo correrlo en un servidor

| Supuesto | Dónde | En servidor (Linux/Docker) |
|---|---|---|
| Fuentes en `C:\Windows\Fonts` | `copiar.py:21`, `chat.py:7` | Empaquetar TTF libres (Montserrat, Inter, Noto) y mapear `FUENTES`. **Si no, el armado de video se rompe.** |
| `os.startfile`, `explorer /select` | `panel.py:1379,1411,1484` | Sacarlos. Reemplazar por descarga o URL firmada. |
| `chrome.exe` en rutas de Windows y perfil en disco | `buscador.py:19-21` | Chrome/Chromium en el contenedor (`--headless=new`) con un volumen para el perfil, o descartar la función. |
| `cloudflared.exe` y quick tunnel | `instagram_api.py:177-255` | **No hace falta**: servir los medios desde object storage con **URL firmada HTTPS** de corta duración y pasar esa URL como `image_url`/`video_url`. |
| Callback `https://localhost` + copiar y pegar | `panel.py:278` | Callback HTTPS real del backend de Pyro. |
| Claude Code en VS Code, "arrancá" a mano | `CONTEXTO.md`, `panel.py:175` | Claude API + workers (§7). |
| `claude -p` buscado en `.vscode/extensions` | `respuestas.py:54` | Messages API con Haiku. |
| `pythonw`, `.bat`, acceso directo, pystray | `*.bat`, `bandeja.py` | Dockerfile + supervisor (systemd/k8s). |
| **Un solo proceso**: locks en memoria, `_states` en memoria, de a una publicación a la vez | `panel.py:55-59, 620` | Si hay más de una réplica: locks distribuidos o cola con *idempotency key* por pieza (para que **nunca se publique dos veces**: hoy lo evita `SO_EXCLUSIVEADDRUSE`). |
| "Si la PC estuvo apagada, la pieza queda vencida" | `revisar_cola` | En servidor casi no pasa, pero conviene mantener la regla para caídas del worker. |
| Hora local naive (Argentina) | todo `datetime.now()` | Guardar en UTC (`timestamptz`) y una zona horaria por cuenta (por defecto `America/Argentina/Buenos_Aires`). `HORAS_REELS` y `HORAS_HISTORIAS` están pensadas en hora de Argentina. |
| Archivos en disco local | todo | Object storage + base de datos. |
| CPU: ffmpeg `libx264 -preset medium`, OpenCV | `copiar.py` | Workers dedicados (cola de render). Limitar la concurrencia como hoy (2 armados a la vez). |

Ejemplo de imagen base: `python:3.12-slim` + `ffmpeg` (o `imageio-ffmpeg`) + fuentes TTF + (opcional) `chromium` + `pip install -r requirements.txt`. Variables mínimas: `HOOKLINE_DATOS`, `HOOKLINE_PUERTO`, `ANTHROPIC_API_KEY`, `META_APP_ID`, `META_APP_SECRET` (o sus equivalentes en el vault).

---

## 10. Puntos de integración sugeridos

### Qué expondría Hookline (como servicio o módulo de Pyro)
- **API REST** (la de §5, con autenticación y por tenant). Recursos: `cuentas`, `piezas` (CRUD + `programar`/`cancelar`/`jueces`), `comentarios` (`sugerir`/`responder`/`ocultar`), `stats`, `tendencias`, `copias`, `material`, `encargos`.
- **Webhooks o eventos salientes** hacia Pyro: `pieza.juzgada`, `pieza.publicada`, `pieza.error`, `pieza.vencida`, `cuenta.token_por_vencer` / `cuenta.token_vencido`, `copia.lista` / `copia.rechazada`, `encargo.listo` / `encargo.incompleto`. Hoy estos avisos van como mensajes `de:"panel"` en `chat.json`: ese es el lugar exacto para emitir los eventos.
- **Cola de trabajos internos**: `publicar(pieza_id)` (programado), `sincronizar(cuenta_id)` (cada hora), `refrescar_token(cuenta_id)` (diario), `tarea_ia(tipo, payload)` y `render_copia(copia_id)`.
- **Webhooks entrantes de Meta** (opcional, mejora): suscribirse a `comments` para no hacer polling de comentarios.

### Qué necesita recibir de Pyro
- Identidad: usuario y organización (tenant), roles (quién aprueba y quién publica).
- Credenciales: App ID/Secret de Meta, API key de Anthropic, y dónde se guardan los tokens (vault).
- Storage: bucket con URLs firmadas públicas (para que Meta baje los archivos).
- Scheduler o cola (Celery/RQ/SQS/lo que use Pyro) y base de datos.
- Dominio HTTPS para el callback OAuth (`/instagram/callback`).
- Política de zona horaria y de notificaciones (cómo avisar a la dueña: email, push, UI de Pyro).

---

## 11. Riesgos y decisiones abiertas

### Decisiones abiertas
1. **¿Qué es Pyro?** (stack, base de datos, cola, multi-tenant o no, UI propia). Sin eso no se puede detallar más la integración.
2. **Claude: ¿API en el servidor o Claude Code local?** Se recomienda la API (§7). Hay que estimar el costo por pieza, copia y filtro, y definir qué hacer cuando Claude se niega a algo (`refusal`).
3. **¿Se mantiene el buscador con Chrome y sesión iniciada, y la descarga de reels ajenos con yt-dlp y cookies?** (ver riesgos).
4. **¿La UI se reescribe en Pyro o se embebe `panel/index.html`?** El HTML depende del contrato de `/api/estado` y del polling cada 4 s.
5. **Aprobación del "encargo"**: `repartir()` crea piezas directamente en estado **`programada`** (`panel.py:739`) para las multicuentas conectadas, **sin aprobación pieza por pieza** y con caption vacío. Además, en ese camino no se valida `puede_publicar`. Esto **contradice** la regla "nunca publica sin aprobación" (`LEEME.md`, `CONTEXTO.md`). Definir: ¿alcanza con el clic de "Mandar a trabajar" o cada pieza necesita aprobación?
6. **Estado `planificada`**: las piezas de cuentas sin conectar quedan `planificada` y **nunca pasan a `programada`** cuando la cuenta se conecta (no hay código para eso). Bug o pendiente.
7. **Inconsistencias en la documentación** que hay que confirmar con la dueña: el plan Pro ($20) de `LEEME.md` contra el plan Max de `CONTEXTO.md`. Además, `LEEME.md` dice que los reels de la fábrica "no crean fotos ni videos", pero la fábrica sí arma videos nuevos (con material existente y ffmpeg, sin IA generativa).

### Riesgos (para que los evalúen los responsables, no son decisiones técnicas)
- **Términos de Meta (Instagram)**:
  - Varias **multicuentas** por la misma "modelo", con **calentamiento** programado (1+1 la semana 1, 2+2 la semana 2: `CALENTAMIENTO`, `panel.py:695`) y horarios corridos para que no coincidan. Meta puede considerar esto **comportamiento no auténtico o coordinado**: riesgo de que bajen el alcance o deshabiliten las cuentas, e incluso la **app de Meta**, que es una sola para todas.
  - El **scraping** de Instagram y TikTok con una sesión iniciada y la exportación de cookies violan los términos de ambas plataformas, y la cuenta "para mirar" se puede bloquear.
  - Personas **generadas con IA**: Meta pide etiquetar contenido realista hecho o modificado con IA. Hoy el código no agrega ninguna etiqueta ni aviso.
  - Si Pyro maneja cuentas de terceros en producción, va a hacer falta **App Review** y verificación del negocio. El modo desarrollo con testers no escala.
- **Derechos de autor**: la fábrica reutiliza el **remate (video) y el audio** de reels ajenos (`copiar.py`, audio `"ejemplo"`), y además **borra marcas de agua** (`marcas`, delogo/blur). Borrar marcas de agua de contenido de terceros agrava el riesgo legal (DMCA, retiro del contenido y strikes). `buscador/estilo.md` también lo marca como riesgo.
- **Contenido**: el tono es "pícaro/subidito" y el objetivo implícito es llevar tráfico a contenido pago ("soltera y disponible, vende su contenido", `buscador/formatos.md`). Eso lo pone cerca de los límites de las normas comunitarias, de las políticas de uso de Anthropic y de los términos de los proveedores de pago. La protección de **menores** depende hoy **solo de prompts** de visión. Recomendación: que sea una regla dura del lado del servidor, con revisión humana y registro de auditoría.
- **Seguridad**: lo de §8 (CSRF y falta de autenticación en el panel, `state` opcional, secretos en texto plano).
- **Operación**: un solo proceso, sin reintentos de publicación (un error deja la pieza en `error` y hay que tocar algo a mano), carga máxima de 100 publicaciones cada 24 h por cuenta, y el procesamiento de Meta puede tardar hasta 20 min.
- **Dependencias frágiles**: `yt-dlp` y los selectores del DOM de Instagram y TikTok (`buscador.py:219-236`) se rompen cuando cambian las páginas.

---

## 12. Estimación (tareas y orden)

Supone 1 dev con experiencia en Python backend. Los números son orientativos (días-persona) y dependen de §11.1.

| # | Tarea | Días |
|---|---|---|
| 0 | Resolver las decisiones abiertas 1-5 con los responsables (en especial Claude API sí/no, el buscador sí/no y la aprobación del encargo) | — |
| 1 | Modelo de datos (§4.5) + migración de los JSON existentes (script que lee `datos/` y `cuentas/`) | 3-4 |
| 2 | Storage de medios (bucket + URLs firmadas) y reemplazo de rutas relativas | 2 |
| 3 | Portar `instagram_api.py` (sin `Tunel`, con URLs firmadas), OAuth con callback HTTPS y `state` persistido, job de refresco de token, secretos en el vault | 3-4 |
| 4 | Bandeja y cola de publicación como jobs programados, idempotentes y con reintentos controlados; regla de vencidas | 3 |
| 5 | Sincronización (perfil, medios, insights), historial y comentarios (polling o webhooks de Meta) | 2-3 |
| 6 | Capa de IA: cola `tareas_ia` + workers con la Messages API para jueces, respuestas, tendencias (web search), copiar, juez y filtrar, con structured outputs y manejo de rechazos | 5-7 |
| 7 | Fábrica: `copiar.py` en workers, fuentes empaquetadas, límite de concurrencia, `repartir` y `encargo` | 4-5 |
| 8 | (Opcional, según §11.3) Buscador en un worker con Chromium y sesión aislada | 3-4 |
| 9 | API REST con autenticación y CSRF + eventos y webhooks hacia Pyro | 3-4 |
| 10 | UI en Pyro (o adaptar `index.html`) | 5-10 |
| 11 | Docker, deploy, observabilidad (logs sin tokens), pruebas end-to-end con una app de Meta y una cuenta de prueba | 3 |
| | **Total aproximado** | **~36-50 días** (sin el buscador: ~33-46) |

Orden recomendado: 0 → 1 → 2 → 3 → 4 → 5 (con esto ya se publica y se mide) → 6 → 9 → 10 → 7 → 8 → 11 (11 se hace en paralelo desde el paso 4).
