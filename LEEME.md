# Hookline Lite

Tu community manager de Instagram, en tu compu. Manejás varias cuentas desde un solo lugar:

- **Bandeja**: subís las fotos, capturas, memes o videos que ya hiciste (arrastrándolos al panel o dejándolos en la
  carpeta `Para subir\<cuenta>`).
- **Jueces**: tres jueces (Público, Gancho y tendencia, Seguridad) miran cada publicación, le ponen nota, te dicen si
  la subirías o qué cambiar, y te proponen el texto, los hashtags y el mejor horario.
- **Aprobar y programar**: vos aprobás (o cambiás el texto), elegís el día y la hora, y el panel la publica solo:
  Historias, Reels, Carruseles y Fotos.
- **Calendario**, **Tendencias** de tu nicho cada semana, **Estadísticas** (seguidores, alcance, mejores horarios),
  **Comentarios** con respuestas sugeridas (vos aprobás cada una) y un **chat con Claude**.

Hookline Lite **no crea fotos ni videos** y **nunca publica nada sin que lo apruebes**.

## Primera vez

1. Seguí **INSTALAR.md** (Python, Visual Studio Code y Claude Code). Son 10 minutos.
2. Seguí **CONECTAR-INSTAGRAM.md** para crear tu app de Meta y conectar tus cuentas.

## Todos los días

1. Doble clic en **Hookline Lite** (el acceso directo del Escritorio). Se abre el panel en el navegador.
2. Abrí **Visual Studio Code** en la carpeta Hookline Lite y escribile **arrancá** a Claude (así funcionan los jueces,
   las tendencias y el chat). Si te olvidás, el panel te avisa arriba con un cartel.
3. En **Bandeja**: subí lo nuevo → **Pedir a los jueces** → mirá lo que dicen → **Aprobar y programar**.
4. Dejá la compu prendida a la hora de las publicaciones. Si estaba apagada, la publicación queda **vencida** y el
   panel te avisa con un botón **Publicar ahora** (nunca la publica sola tarde).

## Cuidar el plan de Claude ($20)

El plan Pro tiene un límite de uso. Para que te rinda:
- Pedí los jueces solo para lo que de verdad vas a publicar.
- Las tendencias se buscan solas una vez por semana; no hace falta pedirlas todos los días.
- En el chat, pedidos cortos y concretos.

## Lo que NO tenés que compartir nunca

Las carpetas **`datos`** y **`cuentas`** tienen el permiso de tus Instagram y la clave de tu app de Meta.
Si le pasás la carpeta Hookline Lite a alguien, **borrá esas dos carpetas antes** (o pasale solo el programa).
Nunca mandes por chat ni pegues en ningún lado el **App Secret** ni archivos `token.json`.

## Qué hay en la carpeta

| Archivo / carpeta | Para qué |
|---|---|
| `Abrir Hookline Lite.bat` | Abre el panel (lo usa el acceso directo) |
| `Instalar.bat` | Instala lo necesario y crea el acceso directo |
| `Para subir\<cuenta>\` | Dejá acá las fotos y videos de cada cuenta |
| `datos\`, `cuentas\` | Tus datos y permisos (no compartir) |
| `panel.py`, `panel\`, `*.py` | El programa |
| `CONTEXTO.md`, `CLAUDE.md`, `.claude\` | Instrucciones para Claude |

---
## Para el programador (repo)
- Este repo NO trae datos privados: `datos/` (config con App ID/secret de Meta, perfil del navegador, cookies), `cuentas/<usuario>/token.json` ni el material/medios. Ver `.gitignore`.
- `cuentas/ejemplo/cuenta.json` muestra el formato de una cuenta. `buscador/fuentes.ejemplo.txt` muestra el formato de fuentes.
- La app de Meta se configura desde el panel (ver CONECTAR-INSTAGRAM.md); cada desarrollador usa su propia app de prueba.
- `herramientas/cloudflared.exe` no está en el repo: bajarlo de https://github.com/cloudflare/cloudflared/releases y ponerlo en `herramientas/`.
