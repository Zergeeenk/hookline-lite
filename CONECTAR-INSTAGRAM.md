# Conectar tus cuentas de Instagram

Instagram solo deja publicar desde un programa si ese programa tiene una **app de Meta**. La creás **una sola vez**,
en tu propia cuenta de desarrolladora, y con esa misma app conectás **todas** tus cuentas de Instagram.
Es gratis y no hace falta que Meta la "revise": como las cuentas son tuyas (o de tus clientas) y las agregás como
**evaluadoras**, todo funciona en modo desarrollo.

> Lo que vas a copiar: **App ID** y **App Secret** de Instagram. El App Secret es como una contraseña:
> pegalo solo en Hookline Lite (Ajustes) y en ningún otro lado.

---

## Paso 1. Cada Instagram tiene que ser "profesional"

En el celular, en cada cuenta que vas a manejar:

1. Perfil → menú (las tres rayitas) → **Configuración y privacidad**.
2. **Tipo de cuenta y herramientas** → **Cambiar a cuenta profesional**.
3. Elegí **Creador** o **Empresa**.
   - Para publicar **historias** por la API conviene **Empresa** (Meta la habilita para cuentas de empresa;
     con Creador puede fallar y tendrías que subir la historia a mano).

## Paso 2. Crear tu cuenta de desarrolladora de Meta

1. Entrá a **https://developers.facebook.com** con tu Facebook (el tuyo, el de la dueña).
2. Arriba a la derecha: **Empezar / Get Started** y seguí los pasos (verificar el correo o el teléfono).
   *Si ya tenés cuenta de desarrolladora, salteá este paso.*

## Paso 3. Crear la app

1. Arriba: **Mis apps** → **Crear app**.
2. Nombre: por ejemplo **Hookline Lite Sofi** (cualquiera). Correo: el tuyo. **Siguiente**.
3. En **Casos de uso**, elegí **"Administrar mensajes y contenido en Instagram"** (dice *Instagram API*). **Siguiente**.
4. Si pregunta por un portfolio comercial: **"Todavía no quiero conectar un portfolio comercial"**. **Siguiente**.
5. Revisá y tocá **Crear app** (puede pedirte tu contraseña de Facebook).

## Paso 4. Copiar el App ID y el App Secret de Instagram

1. Dentro de la app: menú izquierdo **Casos de uso** → en "Administrar mensajes y contenido en Instagram" tocá
   **Personalizar**.
2. Entrá a **Configuración de la API con el inicio de sesión de Instagram** (*API setup with Instagram login*).
3. Arriba vas a ver **Identificador de la app de Instagram** (*Instagram app ID*) y **Clave secreta de la app de
   Instagram** (*Instagram app secret*, tocá **Mostrar**).
   **Ojo:** usá estos dos de **Instagram**, no el "Identificador de la app" de Facebook que aparece en Configuración básica.
4. Abrí Hookline Lite → **Ajustes** → pegá el **App ID** y el **App Secret** → **Guardar**.

## Paso 5. Poner la dirección de vuelta

1. En Hookline Lite → **Ajustes**, al lado de "Dirección de vuelta" tocá **Copiar**
   (es `https://localhost:8791/instagram/callback`).
2. En la página de Meta del paso 4, bajá hasta **3. Configurar el inicio de sesión para empresas de Instagram**
   (*Set up Instagram business login*) → **Configuración de la empresa** (*Business login settings*).
3. En **URI de redireccionamiento de OAuth válidos** pegá la dirección y tocá **Guardar**.

## Paso 6. Agregar cada Instagram como evaluadora

1. En la app de Meta: menú izquierdo **Roles de la app** → **Roles** → **Agregar personas**.
2. Elegí **Evaluador de Instagram** (*Instagram Tester*) y escribí el **@usuario** de la cuenta. **Agregar**.
3. Repetilo con cada cuenta que vas a manejar.

## Paso 7. Cada cuenta acepta la invitación

Con cada cuenta de Instagram (mejor desde la compu):

1. Entrá a **https://www.instagram.com/accounts/manage_access/** con esa cuenta.
2. Pestaña **Invitaciones de evaluador** (*Tester invites*) → **Aceptar**.

## Paso 8. Conectar desde el panel

1. En Hookline Lite → **Cuentas** → **Conectar cuenta**.
2. Tocá **"Tocá acá para abrir Instagram"**. Entrá con la cuenta que querés conectar y **aceptá los permisos**
   (publicar, comentarios, estadísticas: aceptá todos).
3. Instagram te manda a una página que dice **"No se puede acceder a este sitio"**. **Es normal.**
   Copiá la **dirección completa** de la barra de arriba (empieza con `https://localhost:8791/instagram/callback?code=...`),
   volvé al panel y pegala en el cuadrito. Tocá **Conectar**.
4. Listo: aparece la cuenta con su foto y seguidores.

**Para conectar otra cuenta:** primero **cerrá sesión de Instagram en el navegador** (o entrá con la otra cuenta),
porque Instagram autoriza la cuenta que esté abierta. Después repetí el paso 8.

---

## Cosas para saber

- **El permiso dura 60 días** y el panel lo renueva solo si lo abrís al menos una vez cada tanto. Si vence, el panel
  te avisa y tocás **Reconectar**.
- **Fotos, carruseles e historias**: Instagram baja los archivos desde internet. Hookline Lite abre por unos minutos un
  "túnel" gratis de Cloudflare (`herramientas\cloudflared.exe`, lo baja `Instalar.bat`) solo con esos archivos y lo
  cierra al terminar. No necesitás cuenta en Cloudflare. Si el antivirus o la red lo bloquean, las fotos no salen
  (los Reels igual pueden salir por subida directa).
  *Si falta:* volvé a correr `Instalar.bat`, o instalalo con `winget install --id Cloudflare.cloudflared`.
- **Formatos**: fotos JPG/PNG/WEBP (se convierten solas a JPG; si la foto es más alta que 4:5 o más ancha que 1,91:1 se
  completa con un fondo desenfocado, no se recorta). Videos MP4 o MOV (H.264), Reels de hasta 15 minutos y 300 MB.
  La música tiene que venir **ya puesta en el video** (por API no se elige música de la biblioteca de Instagram).
- **Historias por API**: salen sin stickers, encuestas, links ni música (eso solo desde la app).
- **Límite**: Instagram deja publicar hasta 100 publicaciones por día por cuenta desde programas.
- **No hace falta "Revisión de la app"** mientras las cuentas sean evaluadoras de tu app.

## Opcional (avanzado): no copiar y pegar la dirección

Si sabés publicar una página web (por ejemplo con GitHub Pages), subí `herramientas\puente-instagram.html`, poné su
dirección `https://...` en **Ajustes → Avanzado** y en "URI de redireccionamiento de OAuth válidos" de Meta.
Así, al aceptar los permisos, vuelve solo al panel.
