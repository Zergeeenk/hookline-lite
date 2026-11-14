# Propuesta para Pyro: el puente a Hookline

Cuatro patches para `pyromodels/backend`, armados sobre el commit `c952f61`. Copian el patrón del puente de
Contenido IA (Shelton). **No se subió nada a los repos de Pyro**: los revisa y los sube su equipo.

```sh
cd backend
git checkout -b hookline-puente
git am ../hookline-lite/propuesta-pyro/*.patch
make test && make lint && make check-money
```

| Patch | Qué hace |
|---|---|
| 0001 | Permiso propio `hookline` (ver/editar). Migración **136**: tabla `hookline_settings` (como `shelton_settings`) y el permiso `edit` para los admins. |
| 0002 | `business/domain/hooklinebus` y `stores/hooklinedb`: la conexión (dirección, token `hl_...`, secreto de webhooks) sellada con secretbox; cliente con `httpclient` (pozos propios, 3 s para conectar, 20 s por pedido, 5 s sin reintentar tras una caída). `HOOKLINE_ALLOW_HTTP` solo para pruebas. |
| 0003 | `app/domain/hooklineapp`: la lista cerrada `/api/hookline/*`, el alcance por modelo y el webhook. |
| 0004 | `docs/HOOKLINE.md` (y una línea en AGENTS, ARCHITECTURE y DESPLIEGUE). |

**Rutas** (lo demás bajo `/api/hookline/*` da 404 sin llegar a Hookline):
`GET cuentas`, `GET piezas?cuenta=`, `POST pieza` (solo `jueces`, `caption`, `programar`, `cancelar`),
`GET stats?cuenta=`, `GET comentarios?cuenta=`, `POST comentario` (solo `responder`, `ocultar`, `saltar`),
`GET mini?p=` (solo archivos de piezas en su alcance), `GET/PUT/DELETE settings`, `POST settings/test` (admin) y
`POST webhook/{agencyID}` (público, con firma).

**Errores**: sin conexión 503 `hookline_off`; Hookline caído o respuesta rara 502 `hookline_down`; token rechazado
502 `hookline_refused`; afuera del alcance 404.

**Webhook**: firma HMAC-SHA256 de `"<tiempo>.<cuerpo>"` comparada en tiempo constante, hora con ±5 minutos de
margen, hasta 64 KB, un mismo evento avisa una sola vez. Toda falla responde el mismo 401 (no revela qué agencias
tienen Hookline). Con `pieza.error`, `pieza.vencida`, `cuenta.token_vencido` y `cuenta.token_por_vencer` avisa por
`notifbus` a los admins y, si trae `pyro_modelo_id`, a quienes tienen esa modelo y el permiso `hookline`.

**Pruebas que se corrieron** (Postgres real, `-race`): `hooklineapp` (13 pruebas, con una ronda de rutas raras al
azar), `hooklinebus`, `hooklinedb`, `migrate` (`TestParse`), `userbus`, `userdb`, `userapp`, `sheltonapp`,
`sheltonbus`, `app/sdk/...`, `notifapp` y `api/...`: todo ok. `go build`, `go vet`, `gofmt` y `make check-money`:
ok. La suite completa: 90 paquetes ok y uno que falla (`aiapp TestHistory`), que **también falla sin estos
patches**, en `c952f61`.

**Falta (no está en estos patches)**
- **Frontend**: la sección `hookline` en `src/api/types.ts`, `src/layout/nav.ts`, `src/lib/labels.ts` y la página
  `/hookline` (las notificaciones ya apuntan ahí).
- Decidir si el webhook queda en `/api/hookline/webhook/{agencia}` o se mueve a `/webhooks/*` como los de los otros
  proveedores (es una línea).
- Todo el puente usa un solo token de Hookline por agencia; quién hizo qué se sabe por `X-Hookline-Actor`.
- El secreto de webhooks tiene que tener 16 caracteres o más y ser el mismo que `HOOKLINE_WEBHOOK_SECRETO` en
  Hookline.
