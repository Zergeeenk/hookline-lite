# Hookline ↔ Pyro: cómo se conectan

Este documento sale de leer los tres repositorios de Pyro (`pyromodels/backend`, `pyromodels/frontend`,
`pyromodels/infra`) con acceso **de solo lectura**. Explica qué tiene Pyro, cómo se engancha Hookline y qué falta
para encenderlo. En los repos de Pyro **no se tocó nada**: lo de su lado va como propuesta en
[`propuesta-pyro/`](propuesta-pyro/), para que su equipo lo revise y lo suba.

## 1. Qué es Pyro (Model Pro)

Un CRM para agencias de modelos, en Go (chi + Postgres) con frontend en React/TypeScript. Tiene:

- una bandeja de chats de WhatsApp, Telegram, Instagram y Messenger; fans, ventas, tienda, caja, agenda, tareas;
- **Contenido**: la biblioteca de fotos y videos de cada modelo (carpetas), las solicitudes de contenido y un
  **calendario de publicaciones** (borrador, programado, publicado, fallido);
- **Redes sociales**: cada cuenta de Instagram conectada con la app de Meta de Pyro se lee sola cada hora
  (seguidores, publicaciones y, con permiso, alcance y vistas). Lo que sale en Instagram **se marca solo** en su
  calendario;
- **Lorax** (respuestas sugeridas con IA) y **Contenido IA** (Shelton, un servicio externo de agentes);
- una **API de solo lectura** (`/api/v1`, `docs/API-METRICAS.md`) con claves de la agencia (`pyro_...`):
  modelos, sus cuentas en redes, su biblioteca, su calendario, sus estadísticas. 120 pedidos por minuto por clave.

Lo que **no** tiene: publicar en Instagram. Su app de Meta pide `instagram_business_basic`,
`instagram_business_manage_insights` e `instagram_business_manage_messages`, pero no
`instagram_business_content_publish`. Publicar, programar, los jueces, la fábrica de reels, los memes y los
comentarios son de Hookline.

## 2. Cómo se conectan (tres caminos)

```
                 ① lee (clave pyro_, solo lectura)
   Hookline  ───────────────────────────────────────▶  Pyro /api/v1
             ◀───────────────────────────────────────
                 ② el puente de Pyro (token hl_)       Pyro /api/hookline/* → Hookline /api/*
             ───────────────────────────────────────▶
                 ③ avisos (webhooks firmados)          Pyro /api/hookline/webhook/{agencia}
```

### ① Hookline lee de Pyro — **hecho** (este repo)

En Hookline, **Ajustes → Pyro**: se pega la dirección de Pyro y una clave de su API (Pyro → Accesos → API de Pyro).

- **Ata cada cuenta a su modelo.** Lee las modelos y sus cuentas de Instagram (`socialAccounts`) y ata cada
  cuenta de Hookline por su usuario (`pyro_modelo_id`). Las multicuentas que Pyro no tiene heredan la modelo de su
  main. Si la cuenta no tenía nicho, toma el de Pyro. Una atadura a mano se respeta mientras la modelo exista.
- **Muestra lo que falta.** Las cuentas que Pyro tiene y Hookline no aparecen con un botón **Crear**.
- **Trae el material.** La biblioteca de fotos y videos de cada modelo entra a su **Material**, en carpetas con el
  mismo nombre que en Pyro (`Playa / Junio` → `Playa - Junio`; sin carpeta → `Pyro`). Lo que dice "noche" queda
  etiquetado `noche`. No baja dos veces lo mismo. Botón **Traer de Pyro** en el Material de cada modelo.
- **Se repite solo**: modelos cada 6 horas, material una vez por día (se puede apagar).
- **Nunca escribe en Pyro** (su API es de solo lectura).

Seguridad: solo `https`; la clave se guarda cifrada y nunca vuelve (solo sus últimos 4 caracteres); si cambia la
dirección hay que volver a pegar la clave (la clave solo va a donde se pegó); los archivos se bajan con la clave
solo si son del mismo dominio de Pyro; tamaño máximo por archivo; respeta el límite de pedidos de Pyro (espera
cuando Pyro dice `429`). Solo un admin cambia la conexión.

Código: `servidor/hookline/pyro.py` (cliente), `servidor/hookline/servicios/pyro.py`, migración
`0002_pyro.py`, tarjeta en Ajustes y botón en Material. Pruebas: `servidor/tests/test_pyro.py` (42 casos, con un
Pyro falso: conexión, direcciones y claves inválidas, errores de Pyro, ataduras, material, nombres de archivo y
carpetas peligrosos, archivos de otro dominio, archivo gigante, permisos por rol).

### ② Pyro llama a Hookline — **propuesta** en `propuesta-pyro/`

Pyro ya resolvió esto mismo con **Contenido IA (Shelton)**: pone la pantalla, la persona y el permiso, y reenvía
una lista cerrada de rutas al servicio de afuera. La propuesta copia ese patrón para Hookline:

- **Conexión** guardada cifrada en Pyro (`hookline_settings`): dirección de Hookline y un token `hl_...` que se
  crea en Hookline con `hookline token pyro --rol editor`. El token nunca vuelve; si cambia la dirección, se pega
  de nuevo. Sin conexión, la sección está apagada (503) y no afecta a nada más.
- **Lista cerrada** bajo `/api/hookline/*`: cuentas, bandeja, jueces, aprobar/programar/cancelar, texto,
  estadísticas, comentarios (responder/ocultar/saltar) y miniaturas. Lo que no está en la lista da 404 sin llegar a
  Hookline.
- **Permiso** nuevo `hookline` (ver/editar) en Equipo.
- **Alcance**: cada cuenta de Hookline trae `pyro_modelo_id`; quien no es admin ve solo las cuentas de sus modelos
  (y todo lo que cuelga de ellas). Afuera del alcance: 404. Si no se puede leer el mapa, no muestra nada.
- **Quién aprobó**: el puente manda `X-Hookline-Actor: Nombre (pyro:ID)` y Hookline lo anota en la pieza. El JWT
  de Pyro nunca viaja.
- **Hookline caído no le pega a nada más**: conexiones propias, plazos cortos y 502 `hookline_down`.

Está armada y probada: 4 patches sobre `pyromodels/backend` (migración 136, `hooklinebus`, `hooklineapp`,
`docs/HOOKLINE.md`), compilados y con sus pruebas en verde contra Postgres. El detalle, cómo aplicarlos y lo que
falta (la pantalla en el frontend) están en [`propuesta-pyro/README.md`](propuesta-pyro/README.md).

### ③ Hookline le avisa a Pyro — **hecho** en Hookline, recepción en la propuesta

Hookline manda `POST` JSON firmado (`X-Hookline-Firma: sha256=HMAC(secreto, "<tiempo>.<cuerpo>")`) con reintentos
(hasta 12, ~1 día). Eventos: `pieza.juzgada`, `pieza.publicada`, `pieza.error`, `pieza.vencida`,
`cuenta.conectada`, `cuenta.token_por_vencer`, `cuenta.token_vencido` (y los de la fábrica). Todos los que tienen
`cuenta` llevan `pyro_modelo_id`. La propuesta de Pyro los recibe en `/api/hookline/webhook/{agencia}`, verifica la
firma y avisa a quien corresponda.

`pieza.publicada` en realidad no hace falta: el barrido horario de Pyro ya marca en su calendario lo que salió en
Instagram.

## 3. Encenderlo, paso a paso

**En Hookline (ya se puede):**
1. En Pyro, un admin crea una clave en **Accesos → API de Pyro** (nombre: "Hookline"). Se muestra una sola vez.
2. En Hookline, **Ajustes → Pyro**: pegar la dirección de Pyro (`https://...`) y la clave → **Guardar**. Se prueba
   antes de guardar y sincroniza solo.
3. Revisar en **Cuentas** que cada una diga "Pyro: <modelo>". Crear las que falten desde Ajustes → Pyro.

**En Pyro (cuando su equipo suba la propuesta):**
4. En Hookline, crear el token del puente:
   `docker compose run --rm api token pyro --rol editor` (se muestra una sola vez).
5. En Pyro, sección Hookline → Ajustes: pegar la dirección de Hookline, el token y un secreto para los webhooks
   (16 caracteres o más) → **Probar**.
6. En Hookline, poner `HOOKLINE_WEBHOOK_URL=https://<pyro>/api/hookline/webhook/<id de la agencia>` y
   `HOOKLINE_WEBHOOK_SECRETO=<el mismo secreto>`, y reiniciar.
7. En Pyro, dar el permiso **Hookline** en Equipo a quien corresponda.

Requisito para los dos: **HTTPS con dominio** en Hookline (también lo pide Meta).

## 4. Decisiones para el equipo

1. **¿Una app de Meta o dos?** Pyro tiene la suya (mensajes y estadísticas) y Hookline necesita publicar.
   Opción A: dos apps (cada servicio con la suya; más simple, dos revisiones de Meta). Opción B: una sola app con
   todos los permisos (una revisión, pero los dos servicios comparten la configuración).
2. **Estadísticas de Instagram**: hoy las leen los dos. Se puede dejar a Pyro como fuente única y que Hookline las
   lea de su API (`/models/{id}/networks`) para no pedirle dos veces lo mismo a Meta.
3. **Contenido IA (Shelton) y la fábrica de Hookline** se pisan en parte (IA para contenido). Conviene definir qué
   hace cada uno.
4. **Calendario**: Pyro marca solo lo publicado. Si se quiere ver en Pyro también lo **programado** en Hookline,
   el puente ya lo trae (bandeja con `cuando`); falta decidir si se muestra en su calendario o en una sección aparte.

## 5. Aviso de seguridad (solo aviso: no se tocó nada)

En `pyromodels/infra` hay un archivo **commiteado** con valores reales de producción: `prod.env.bak-2057`
(20 variables, 16 con valor: contraseñas de base de datos y del admin, la clave que firma las sesiones, la clave de
cifrado, tokens de los gateways y de Meta). Además `PENDIENTES.md` muestra los datos de la app de Telegram. Esto no
se copió ni se mostró en ningún lado. Lo decide su devops, pero lo recomendable es **cambiar esas claves** y
sacar el archivo del repositorio (y de su historial): quien tuvo acceso al repo pudo verlas.
