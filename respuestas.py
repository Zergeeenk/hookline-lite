"""Respuestas sugeridas a comentarios (adaptado de ig_respuestas.py del proyecto base).

La dueña SIEMPRE aprueba antes de mandar: acá solo se clasifica y se propone el texto.
Camino barato: UN pedido a Claude Haiku por tanda (todos los comentarios juntos, salida JSON) con el mismo
`claude` de Claude Code, sin herramientas. Si Claude Code no está a mano, el panel le pide las respuestas al chat.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

CLASES = {"positivo", "neutro", "pregunta", "acoso", "insulto", "spam"}
NOMBRE_CLASE = {"positivo": "buena onda", "neutro": "neutro", "pregunta": "pregunta", "acoso": "acoso / zarpado",
                "insulto": "insulto", "spam": "spam"}
SIN_RESPUESTA = {"acoso", "insulto", "spam"}  # se sugiere ocultar, no contestar

# lo que nunca puede salir en una respuesta (si aparece, se marca para revisar)
PROHIBIDO_RESP = re.compile(r"https?://|whats ?app|telegram|onlyfans|\bof\b|precio|privado|\bdm\b|tarjeta|"
                            r"idiota|pelotud|bolud|forr[oa]|imb[eé]cil|puto|puta|mierda", re.I)

SISTEMA = ("Sos la community manager de una creadora de contenido en Instagram. Clasificás comentarios y redactás "
           "respuestas cortas. Respondés SOLO con JSON válido, sin texto extra.")

PROMPT = """Clasificá cada comentario y proponé una respuesta.

CUENTA: {cuenta} · nicho: {nicho} · tono: {tono}

CLASES:
- "positivo": elogio, apoyo, risa, emojis lindos, "hermosa", "me encanta".
- "neutro": comentario corto sin conflicto (una palabra, un emoji, una mención a un amigo).
- "pregunta": pregunta real (dónde es, qué ropa, cuándo sale, cómo hizo algo).
- "acoso": comentario sexual explícito, pedidos zarpados, insistencia rara, "pasame fotos", "cuánto cobrás".
- "insulto": insulta a la creadora o a otra persona, odio.
- "spam": promos, "seguime", links, bots, estafas.
Ante la duda entre positivo/neutro y otra clase, elegí la otra clase.

RESPUESTA:
- "positivo"/"neutro": corta (2 a 10 palabras), cálida, en el tono de la cuenta, relacionada con lo que dijo la persona,
  0 a 2 emojis. Todas distintas entre sí.
- "pregunta": respuesta amable de hasta 25 palabras SIN inventar datos (si no sabés, invitá a mirar el perfil o las historias).
- "acoso", "insulto", "spam": respuesta "" (se sugiere ocultar el comentario).
- Nunca links, nunca precios, nunca mandar a privado/DM, nunca datos personales, nunca contestar provocaciones.

COMENTARIOS:
{comentarios}

Devolvé exactamente: {{"items":[{{"id":"<id>","clase":"<clase>","respuesta":"<texto>"}}]}} con un item por comentario."""


def buscar_claude():
    """Ruta al ejecutable de Claude Code (o None)."""
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).exists():
            return str(c)
    ext = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".vscode" / "extensions"
    for p in sorted(glob.glob(str(ext / "anthropic.claude-code*" / "**" / "claude.exe"), recursive=True), reverse=True):
        return p
    return None


def _norm(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", t)).strip()


def armar_prompt(cuenta, nicho, tono, comentarios):
    coms = [{"id": c["id"], "usuario": c.get("usuario", ""), "texto": (c.get("texto") or "")[:400]} for c in comentarios]
    return PROMPT.format(cuenta=cuenta, nicho=nicho or "general", tono=tono or "cercano, buena onda, rioplatense",
                         comentarios=json.dumps(coms, ensure_ascii=False))


def leer_items(texto):
    m = re.search(r"\{.*\}", texto or "", re.S)
    try:
        datos = json.loads(m.group(0)) if m else {}
    except Exception:
        datos = {}
    res = {}
    for it in datos.get("items") or []:
        cid, clase = str(it.get("id", "")), str(it.get("clase", "")).strip().lower()
        if cid and clase in CLASES:
            res[cid] = {"clase": clase, "respuesta": str(it.get("respuesta") or "").strip().strip('"')[:300]}
    return res


def sugerir_haiku(cuenta, nicho, tono, comentarios, carpeta):
    """UN pedido a Haiku por tanda. Devuelve {id: {clase, respuesta}}."""
    exe = buscar_claude()
    if not exe:
        raise RuntimeError("no encuentro Claude Code")
    Path(carpeta).mkdir(parents=True, exist_ok=True)
    r = subprocess.run([exe, "-p", "--model", "haiku", "--max-turns", "1", "--output-format", "json",
                        "--system-prompt", SISTEMA, "--tools", "", "--strict-mcp-config", "--disable-slash-commands"],
                       input=armar_prompt(cuenta, nicho, tono, comentarios), capture_output=True, text=True,
                       encoding="utf-8", timeout=180, cwd=str(carpeta), env={**os.environ, "MAX_THINKING_TOKENS": "0"},
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        salida = json.loads(r.stdout)
    except Exception:
        raise RuntimeError((r.stderr or r.stdout or "Haiku no contestó")[:200])
    res = leer_items(salida.get("result", ""))
    if not res:
        raise RuntimeError("Haiku no devolvió el JSON esperado.")
    return res


def revisar(c, r, usadas=()):
    """Aplica reglas fijas sobre la sugerencia. Devuelve (motivo_alerta, respuesta). Alerta "" = se ve bien."""
    clase, resp = r.get("clase", ""), (r.get("respuesta") or "").strip()
    if clase in SIN_RESPUESTA:
        return f"{NOMBRE_CLASE[clase]}: mejor ocultarlo", ""
    if not resp:
        return "sin sugerencia", resp
    if PROHIBIDO_RESP.search(resp) or "@" in resp or len(resp) > 220:
        return "revisá la respuesta antes de mandar", resp
    if _norm(resp) in {_norm(x) for x in usadas}:
        return "respuesta repetida", resp
    return "", resp
