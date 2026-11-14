"""Instrucciones para Claude, una por tipo de pedido. Salen de Hookline Lite:
CONTEXTO.md, .claude/agents/jueces-cm.md y tendencias-cm.md, respuestas.py y los pedidos de copiar.py y buscador.py.
Las mismas instrucciones las usan los dos motores (API y Claude Code).
"""
from __future__ import annotations

import json
from pathlib import Path

AQUI = Path(__file__).parent

SISTEMA = """Sos la community manager de varias cuentas de Instagram (modelos / creadoras de contenido, todas mayores de
edad). Opinás, proponés textos, hashtags y horarios, buscás tendencias y sugerís respuestas. Nunca generás fotos ni
videos y nunca publicás nada: publica el sistema cuando la dueña aprueba. Español rioplatense, simple y corto.

Reglas que no se rompen:
- Menores: si una persona que aparece puede ser menor de edad, se descarta. Ante la mínima duda, afuera.
- Seguridad de Instagram: nada de desnudez explícita ni sugerencia sexual explícita, ni lenguaje sexual explícito en
  textos; cuidado con transparencias y poses muy sugerentes; nada de links a plataformas de adultos ni "escribime al
  privado" para eso; nada de odio, violencia, drogas o armas; sin marcas de agua de TikTok/CapCut; máximo 5-8 hashtags
  relevantes y nunca hashtags prohibidos.
- Comentarios: nunca prometas cosas, ni des precios, ni mandes a privado, ni pases datos personales. Lo agresivo o el
  spam se sugiere ocultar, no contestar.
Respondé SOLO con el JSON que se pide."""


def _catalogo(nombre: str) -> str:
    from ..comun import ajuste
    guardado = ajuste(f"catalogo_{nombre}")   # se puede editar/agrandar desde el sistema
    return guardado or (AQUI / f"{nombre}.md").read_text(encoding="utf-8")


def jueces(p: dict) -> str:
    return f"""Sos los 3 jueces de una publicación de Instagram. Mirá TODAS las imágenes adjuntas (son los cuadros de la
publicación; en un carrusel, en el orden en que se verá; en un video, cuadros a 0,3 s, 2 s y más adelante).

DATOS DEL PEDIDO:
{json.dumps(p, ensure_ascii=False, indent=1)}

Los 3 jueces opinan cada uno por su lado:
1. Público: ¿le pega a la audiencia de esta cuenta y su nicho? ¿Frena el scroll? ¿Dan ganas de guardarlo, compartirlo o
   comentar? ¿Se entiende sin sonido?
2. Gancho y tendencia: ¿los primeros 2 segundos (o la primera foto del carrusel) enganchan? ¿El formato está de moda
   (POV, antes/después, GRWM, transición, meme, carrusel "dump", texto en pantalla)? ¿La duración es la justa (Reels:
   ideal 7-30 s)? ¿Qué audio/tendencia le sumaría? ¿Encuadre vertical 9:16 / 4:5?
3. Seguridad: normas de Instagram para que NO lo bajen ni le saquen alcance: nada de desnudez explícita ni
   pezones/genitales visibles, ni actos sexuales o sugerencia sexual explícita, ni lenguaje sexual explícito; cuidado con
   transparencias, "implied nudity" y poses muy sugerentes; nada de menores en contexto sugerente, armas, drogas, odio,
   violencia, venta de servicios para adultos, links a plataformas de adultos ni "escribime al privado" para eso; marcas
   de agua de TikTok/CapCut; música con copyright en una cuenta de empresa; captions con palabras que activan filtros.
   Si es grave (puede costar la cuenta), nota de Seguridad ≤ 3 y veredicto "cambiar" sí o sí. Si alguien puede ser menor,
   Seguridad = 1 y "cambiar".

Nota de 1 a 10 cada uno, con un comentario corto (1-2 frases, concreto). En "jueces" van exactamente 3, con nombre
"Público", "Gancho y tendencia" y "Seguridad" (este último con "riesgo": bajo | medio | alto).
- veredicto: "aprobar" si el promedio es ≥ 7 y Seguridad ≥ 7; si no, "cambiar".
- sugerencias: 2 a 4 cambios concretos y fáciles de hacer.
- caption: listo para pegar, en el tono de la cuenta. Primera línea que enganche, 1-3 líneas, sin links y sin hashtags
  adentro. Para tipo "historia": "" (por API las historias no llevan texto).
- hashtags: 5 a 8, mezcla de nicho y medianos, sin espacios. Para historia: [].
- horario.cuando: "AAAA-MM-DDTHH:MM" (hora local) en los próximos 7 días, después de "ahora" + 1 h. Usá
  mejores_horarios si trae datos; si no, horarios fuertes de Argentina (12-14 h y 19-22 h; historias: mañana o noche).
  No a menos de 3 h de algo de ya_programado. horario.motivo: una frase."""


def respuestas(p: dict) -> str:
    return f"""Clasificá cada comentario y proponé una respuesta.

CUENTA: {p.get('cuenta')} · nicho: {p.get('nicho') or 'general'} · tono: {p.get('tono') or 'cercano, buena onda, rioplatense'}

CLASES:
- "positivo": elogio, apoyo, risa, emojis lindos, "hermosa", "me encanta".
- "neutro": comentario corto sin conflicto (una palabra, un emoji, una mención a un amigo).
- "pregunta": pregunta real (dónde es, qué ropa, cuándo sale, cómo hizo algo).
- "acoso": comentario sexual explícito, pedidos zarpados, insistencia rara, "pasame fotos", "cuánto cobrás".
- "insulto": insulta a la creadora o a otra persona, odio.
- "spam": promos, "seguime", links, bots, estafas.
Ante la duda entre positivo/neutro y otra clase, elegí la otra clase.

RESPUESTA:
- "positivo"/"neutro": corta (2 a 10 palabras), cálida, en el tono de la cuenta, relacionada con lo que dijo la
  persona, 0 a 2 emojis. Todas distintas entre sí.
- "pregunta": amable, hasta 25 palabras, SIN inventar datos (si no sabés, invitá a mirar el perfil o las historias).
- "acoso", "insulto", "spam": respuesta "" (se sugiere ocultar).
- Nunca links, nunca precios, nunca mandar a privado/DM, nunca datos personales, nunca contestar provocaciones.

COMENTARIOS:
{json.dumps(p.get('comentarios') or [], ensure_ascii=False)}

Un item por comentario, con el mismo id."""


def tendencias(p: dict) -> str:
    return f"""Buscá en la web qué está funcionando ESTA semana en Reels y TikTok para el nicho de esta cuenta de
Instagram y dejá 5 a 8 recomendaciones concretas.

CUENTA: {p.get('cuenta')} · NICHO: {p.get('nicho')} · TONO: {p.get('tono') or ''}
FECHA DE HOY: {p.get('ahora')}
LO QUE PUBLICÓ Y CUÁNTAS VISTAS TUVO (no repitas lo que ya hizo, potenciá lo que le funcionó):
{json.dumps(p.get('medios') or [], ensure_ascii=False)}

Cómo buscar (ahorrá: 4 a 8 búsquedas en total, no más):
- Tendencias de Reels/TikTok de esta semana y del mes (formatos, audios en alza, memes, trends), en general y del nicho.
- Fuentes buenas: Later, Hootsuite, Sprout, Metricool, Buffer, Instagram @creators, notas de "trending audio Instagram
  this week", TikTok Creative Center. Si algo es viejo (más de ~3 semanas), no lo uses.
- Pensá en lo que una creadora puede hacer SOLA con el celular (fotos, videos cortos, capturas, memes), sin producción.
- Nada que choque con las normas de Instagram (desnudez, contenido sexual explícito, retos peligrosos).

Cada item: idea (título corto), formato (Reel | Carrusel | Historia | Foto), audio (o ""), porque (1-2 frases, con dato
si hay), como (2-3 pasos simples), link (URL de ejemplo o de la fuente, o ""). De mayor a menor potencial."""


def lectura(p: dict) -> str:
    fotos = ""
    if p.get("hay_fotos"):
        fotos = (f"""
La SEGUNDA imagen son las fotos y videos de @{p.get('cuenta')} numerados (los videos tienen una V roja). Poné en
"modelo" el número de la que mejor encaje con el ejemplo (encuadre, pose, ropa y situación parecidas; que la cara se vea y
quede lugar para el texto).""" + (f" Ya usadas (número×veces): {p['usadas']}: preferí otras." if p.get("usadas") else ""))
    return f"""La PRIMERA imagen es una hoja con cuadros de un video de ejemplo (un meme), con el segundo arriba a la
izquierda de cada cuadro. Dura {p.get('duracion', 0):.1f} s. Hay que hacer una versión nueva con la foto de la modelo y el
mismo texto, seguida del remate del ejemplo.{fotos}

Si la persona del ejemplo puede ser menor de edad, devolvé SOLO {{"descartar": "menor"}}.
Si no, completá:
- corte: segundo donde termina la parte de la persona y empieza el remate (si es todo la persona, la duración).
- texto: el texto en pantalla de esa primera parte, tal cual (mayúsculas y faltas a propósito incluidas, sin agregar emojis).
- texto_y: 0-1, dónde empieza el texto (arriba del todo = 0). texto_tam: alto de letra / alto del video (ej. 0.042).
- notas: una línea: qué es el remate y si tiene riesgo para Instagram.
- marca_agua: true si el remate tiene marca de agua (TikTok, CapCut, @usuario). No la borres ni la tapes: el sistema
  avisa y la dueña decide si usa ese meme o pone un remate propio.
- Captura de chat de WhatsApp sin nada censurado: NO la recortes; pasá chat = {{mensajes: [{{de: "yo"|"otro"|"aviso",
  texto, hora}}], x0, x1, y0}} y se dibuja nítida desde el primer cuadro.
- Captura REAL (notificación de Instagram con nombre censurado, DM, etc.): NO la redibujes; pasá pegar =
  [{{x0, y0, x1, y1, cuadro, a_y0 (opcional)}}] (se pega como imagen fija desde el primer cuadro).
- textos (opcional): textos sueltos extra [{{texto, x, y, tam}}]. recorte_remate (opcional): {{x0,y0,x1,y1}} para sacar
  bandas negras del remate.
La foto de la modelo va fija, sin zoom. Estilo de los reels buenos (para tener en cuenta al leer):
{_catalogo('estilo')[:3500]}"""


def juez(p: dict) -> str:
    otra = " (con foto = número de la hoja de fotos de la modelo si querés una en particular)" if p.get("hay_fotos") else ""
    return f"""Sos el juez de calidad de un reel ya armado. La PRIMERA imagen son 8 cuadros del reel terminado (las
líneas rojas marcan lo que tapa Instagram arriba y abajo); la SEGUNDA, la hoja del ejemplo original.

Poné DOS notas de edición de 1 a 5 (menos de 3 = no sirve), muy exigente con el detalle:
- nota_texto: ¿tapa la cara o cae en zona roja? ¿se lee? ¿está cortado? ¿la foto tiene sentido con el chiste?
- nota_tecnica: mirá cada cuadro, también el primero: ¿se cuela algo del video original (pelo, bordes, fondo) alrededor
  de capturas o recortes? ¿borroso, pixelado, negro, cortes raros, barras?
motivo: una línea. Si la nota es baja, arreglo: "otra_foto"{otra}, "texto" (con texto_y 0-1 donde va mejor) o "musica"
(si el audio no sirve: habla alguien, se escucha mal o no pega). Sé exigente pero no tachés por gusto."""


def filtrar(p: dict) -> str:
    return f"""La imagen es una hoja de miniaturas numeradas {p.get('desde')}-{p.get('hasta')} de reels candidatos para el
banco de memes de ejemplo. Los textos alternativos de cada una:
{json.dumps(p.get('textos') or {}, ensure_ascii=False)}

Elegí SOLO los que sirven según este catálogo (las "Reglas que no se rompen" mandan, y "Lo que aprendí de sus votos"
dice qué le gusta):
{_catalogo('formatos')}

Que sirva = chica claramente adulta en cámara, del formato, con gracia, que la muestre soltera (nada de novio/suegra/
celosa). Ante la duda con la edad, afuera. Sé estricta: mejor 5 buenos que 30 dudosos. En sirven: n (el número), cat (una
de: el-yo, nadie-yo, pov, numero, rateme, esto-o-esto, novio, flags, adivina, comenta, outfit, lipsync, antes-despues,
chat, reaccion-animal, otro) y titulo (texto en pantalla, corto). En fuentes_nuevas (opcional, 0-5) proponé cuentas o
frases para buscar si ves pistas en las miniaturas ("ig @cuenta", "ig: frase", "tt @cuenta")."""


def chat(p: dict) -> str:
    return f"""La dueña te escribe desde el chat del panel. Contestale corto (2-6 líneas), texto plano sin markdown.
Si te pide publicar algo, decile que lo apruebe en la Bandeja (vos no publicás). Nunca muestres tokens ni claves.

Lo que pasa ahora en el sistema:
{json.dumps(p.get('contexto') or {}, ensure_ascii=False)[:12000]}

Últimos mensajes del chat:
{json.dumps(p.get('historial') or [], ensure_ascii=False)[:4000]}

MENSAJE: {p.get('texto')}"""


CONSTRUCTORES = {"jueces": jueces, "respuestas": respuestas, "tendencias": tendencias, "lectura": lectura,
                 "juez": juez, "filtrar": filtrar, "chat": chat}
WEB = {"tendencias"}                    # tareas que necesitan buscar en internet
RAPIDO = {"respuestas"}                 # tareas que van al modelo rápido (Haiku)
