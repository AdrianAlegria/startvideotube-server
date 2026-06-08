"""
StartVideoTube — Servidor FastAPI v5.3
Propietario: Adrian Alegría — La Cruz, Guanacaste, Costa Rica
GitHub: github.com/AdrianAlegria/startvideotube-server

CAMBIOS v5.3 vs v5.2:
- Audio: gTTS reemplazado por Fish Audio como motor principal
- Fallback automático a gTTS si FISH_API_KEY no está configurada o falla
- Endpoint /generate-pdf con WeasyPrint (activa automáticamente si está instalado)
- Endpoint /generate-audio standalone para pruebas
- Ken Burns effect en ffmpeg (zoom lento sobre imagen de fondo)
- Health endpoint actualizado a v8.1
"""

import os
import re
import uuid
import tempfile
import subprocess
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("startvideotube")

# ─── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="StartVideoTube", version="5.3.6")

# ─── Variables de entorno ──────────────────────────────────────────────────────
FISH_API_KEY  = os.environ.get("FISH_API_KEY", "")
FISH_VOICE_ID = os.environ.get("FISH_VOICE_ID", "")

# ─── Modelos ───────────────────────────────────────────────────────────────────
class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    thumbnail_url: str = ""
    fecha: str = ""

class AudioRequest(BaseModel):
    script: str

class PDFRequest(BaseModel):
    content: str   # HTML completo
    title: str = "Documento"


# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════════════════

def clean_script_for_tts(text: str) -> str:
    """
    Elimina caracteres especiales que gTTS/Fish Audio leen en voz alta.
    Limita a 450 palabras máximo.
    """
    # Eliminar caracteres problemáticos
    text = re.sub(r"[*#@\[\]|`~^]", "", text)
    # Reemplazar guiones dobles por pausa natural
    text = re.sub(r"—|--", ", ", text)
    # Colapsar espacios múltiples
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()

    # Limitar a 450 palabras
    words = text.split()
    if len(words) > 450:
        text = " ".join(words[:450])
        logger.info(f"Script recortado a 450 palabras (era {len(words)})")

    return text


def generate_audio_fish(script: str, audio_path: str) -> bool:
    """
    Genera audio con Fish Audio API.
    Retorna True si tuvo éxito, False si debe usar fallback.
    """
    if not FISH_API_KEY or not FISH_VOICE_ID:
        logger.warning("FISH_API_KEY o FISH_VOICE_ID no configurados — usando gTTS")
        return False

    try:
        logger.info("Generando audio con Fish Audio...")
        response = httpx.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "text": script,
                "reference_id": FISH_VOICE_ID,
                "format": "mp3",
                "latency": "normal",
                "model": "s1",
            },
            timeout=60,
        )

        if response.status_code == 200:
            with open(audio_path, "wb") as f:
                f.write(response.content)
            logger.info(f"Audio Fish generado: {audio_path}")
            return True
        else:
            logger.error(f"Fish Audio error {response.status_code}: {response.text[:200]}")
            return False

    except Exception as e:
        logger.error(f"Fish Audio excepción: {e}")
        return False


def generate_audio_gtts(script: str, audio_path: str) -> None:
    """
    Fallback: genera audio con gTTS.
    """
    try:
        from gtts import gTTS
        logger.info("Generando audio con gTTS (fallback)...")
        tts = gTTS(text=script, lang="es", slow=False)
        tts.save(audio_path)
        logger.info(f"Audio gTTS generado: {audio_path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error gTTS fallback: {e}")


def generate_audio(script: str, audio_path: str) -> None:
    """
    Motor de audio principal con fallback automático.
    1. Intenta Fish Audio (si hay API key configurada)
    2. Si falla, usa gTTS
    """
    success = generate_audio_fish(script, audio_path)
    if not success:
        generate_audio_gtts(script, audio_path)


def download_thumbnail(thumbnail_url: str, image_path: str) -> bool:
    """
    Descarga la imagen de fondo y la convierte a JPEG RGB puro.
    Esto evita errores de ffmpeg con imágenes RGBA o con canal alpha.
    """
    if not thumbnail_url:
        return False
    try:
        from PIL import Image
        import io
        logger.info(f"Descargando thumbnail: {thumbnail_url[:60]}...")
        r = httpx.get(thumbnail_url, timeout=30, follow_redirects=True)
        if r.status_code == 200:
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            img.save(image_path, "JPEG", quality=95)
            logger.info(f"Thumbnail guardado como JPEG RGB: {image_path}")
            return True
        logger.warning(f"Thumbnail HTTP {r.status_code}")
        return False
    except Exception as e:
        logger.warning(f"Error descargando thumbnail: {e}")
        return False


def create_placeholder_image(image_path: str, canal: str) -> None:
    """
    Crea imagen de fondo sólida oscura con ffmpeg si no hay thumbnail.
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=0x0d1117:size=1080x1920:rate=1",
        "-frames:v", "1",
        image_path
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    logger.info(f"Imagen placeholder creada para canal: {canal}")


def generate_srt(script: str, audio_duration: float, srt_path: str) -> None:
    """
    Genera archivo SRT dividiendo el script en bloques de ~8 palabras.
    Distribuye el tiempo proporcionalmente.
    """
    words = script.split()
    chunk_size = 8
    chunks = [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]

    if not chunks:
        chunks = [script]

    time_per_chunk = audio_duration / len(chunks)

    def fmt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, chunk in enumerate(chunks):
        start = i * time_per_chunk
        end   = (i + 1) * time_per_chunk
        lines.append(str(i + 1))
        lines.append(f"{fmt_time(start)} --> {fmt_time(end)}")
        lines.append(chunk)
        lines.append("")

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"SRT generado: {len(chunks)} bloques")


def get_audio_duration(audio_path: str) -> float:
    """
    Obtiene duración del audio en segundos usando ffprobe.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"No se pudo obtener duración: {e} — usando 150s por defecto")
        return 150.0


def render_video(
    image_path: str,
    audio_path: str,
    srt_path: str,
    output_path: str,
    duration: float,
) -> None:
    """
    Renderiza el video final con ffmpeg.
    - Formato: MP4 H.264, 1080x1920 (9:16 TikTok)
    - Audio: AAC 192k
    - Subtítulos: SRT quemados, blanco con borde negro
    - Ken Burns: zoom lento en imagen de fondo
    """
    # Escalar imagen a 1080x1920 + audio
    # Subtítulos se agregan via drawtext (no requiere libass)
    vf_filter = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",  # Menos CPU y RAM
        "-threads", "2",          # Limitar threads — evita SIGKILL en Railway free
        "-b:v", "800k",           # Bitrate reducido — menos RAM
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    logger.info("Renderizando video con ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"ffmpeg FULL stderr: {result.stderr}")
        logger.error(f"ffmpeg stdout: {result.stdout}")
        logger.error(f"ffmpeg returncode: {result.returncode}")
        raise HTTPException(status_code=500, detail=f"Error ffmpeg: {result.stderr[-500:]}")

    logger.info(f"Video renderizado: {output_path}")


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    fish_status = "configurado" if FISH_API_KEY else "sin_configurar_usando_gtts"
    return {
        "status": "ok",
        "service": "StartVideoTube v8.1",
        "version": "5.3",
        "audio_engine": "fish_audio" if FISH_API_KEY else "gtts_fallback",
        "fish_audio": fish_status,
    }


@app.post("/generate-video")
async def generate_video(req: VideoRequest):
    """
    Genera un video MP4 completo.
    Proceso: Limpiar script → Audio (Fish/gTTS) → Descargar imagen → SRT → ffmpeg → MP4
    Input:  script, canal, tema, thumbnail_url (opcional), fecha (opcional)
    Output: MP4 binario directo
    Timeout n8n recomendado: 600000ms
    IMPORTANTE: usar keypair body en n8n, NO JSON body
    """
    job_id = str(uuid.uuid4())[:8]
    logger.info(f"[{job_id}] Iniciando video — canal: {req.canal} | tema: {req.tema[:40]}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        audio_path  = str(tmpdir / "audio.mp3")
        image_path  = str(tmpdir / "background.jpg")
        srt_path    = str(tmpdir / "subtitles.srt")
        output_path = str(tmpdir / "output.mp4")

        # 1. Limpiar script
        script_clean = clean_script_for_tts(req.script)
        logger.info(f"[{job_id}] Script limpio: {len(script_clean.split())} palabras")

        # 2. Generar audio
        generate_audio(script_clean, audio_path)

        # 3. Obtener duración del audio
        duration = get_audio_duration(audio_path)
        logger.info(f"[{job_id}] Duración audio: {duration:.1f}s")

        # 4. Imagen de fondo
        img_ok = download_thumbnail(req.thumbnail_url, image_path)
        if not img_ok:
            logger.info(f"[{job_id}] Sin thumbnail URL — creando placeholder")
            create_placeholder_image(image_path, req.canal)

        # 5. Generar SRT
        generate_srt(script_clean, duration, srt_path)

        # 6. Renderizar video
        render_video(image_path, audio_path, srt_path, output_path, duration)

        # 7. Verificar que el archivo existe y leer MP4
        import os
        if not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail=f"ffmpeg no generó el archivo de salida: {output_path}")
        
        file_size = os.path.getsize(output_path)
        logger.info(f"[{job_id}] Archivo MP4 existe: {file_size} bytes")
        
        if file_size == 0:
            raise HTTPException(status_code=500, detail="ffmpeg generó archivo vacío")

        with open(output_path, "rb") as f:
            video_bytes = f.read()

        logger.info(f"[{job_id}] Video listo: {len(video_bytes) / 1024 / 1024:.1f} MB")

    return Response(
        content=video_bytes,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="video_{req.canal}_{req.fecha or job_id}.mp4"',
            "X-Job-ID": job_id,
        },
    )


@app.post("/generate-audio")
async def generate_audio_endpoint(req: AudioRequest):
    """
    Genera solo el audio MP3 (útil para pruebas de voz).
    Input:  script
    Output: MP3 binario directo
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = str(Path(tmpdir) / "audio.mp3")
        script_clean = clean_script_for_tts(req.script)
        generate_audio(script_clean, audio_path)

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={"Content-Disposition": 'attachment; filename="audio_test.mp3"'},
    )


@app.post("/generate-pdf")
async def generate_pdf(req: PDFRequest):
    """
    Genera un PDF profesional con diseño premium usando ReportLab.
    Input:  content (texto del ebook), title (título del documento)
    Output: PDF binario directo con diseño
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.colors import HexColor, white, black
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak,
        HRFlowable, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    from reportlab.platypus import KeepTogether
    import io

    # ── Colores del tema premium ──────────────────────────────────────
    NAVY    = HexColor("#0D1B2A")
    GOLD    = HexColor("#C9A84C")
    LIGHT   = HexColor("#F5F0E8")
    GRAY    = HexColor("#4A4A4A")
    LGRAY   = HexColor("#888888")

    # ── Estilos ──────────────────────────────────────────────────────
    styles = getSampleStyleSheet()

    style_title = ParagraphStyle(
        "EbookTitle",
        fontName="Helvetica-Bold",
        fontSize=28,
        textColor=GOLD,
        alignment=TA_CENTER,
        spaceAfter=8,
        leading=34,
    )
    style_subtitle = ParagraphStyle(
        "EbookSubtitle",
        fontName="Helvetica",
        fontSize=13,
        textColor=LIGHT,
        alignment=TA_CENTER,
        spaceAfter=6,
        leading=18,
    )
    style_brand = ParagraphStyle(
        "EbookBrand",
        fontName="Helvetica-Bold",
        fontSize=10,
        textColor=GOLD,
        alignment=TA_CENTER,
        spaceAfter=4,
        letterSpacing=3,
    )
    style_h1 = ParagraphStyle(
        "EbookH1",
        fontName="Helvetica-Bold",
        fontSize=20,
        textColor=NAVY,
        spaceBefore=20,
        spaceAfter=6,
        leading=26,
    )
    style_h2 = ParagraphStyle(
        "EbookH2",
        fontName="Helvetica-Bold",
        fontSize=14,
        textColor=GOLD,
        spaceBefore=14,
        spaceAfter=4,
        leading=18,
    )
    style_body = ParagraphStyle(
        "EbookBody",
        fontName="Helvetica",
        fontSize=11,
        textColor=GRAY,
        alignment=TA_JUSTIFY,
        spaceAfter=8,
        leading=17,
    )
    style_quote = ParagraphStyle(
        "EbookQuote",
        fontName="Helvetica-Oblique",
        fontSize=12,
        textColor=NAVY,
        alignment=TA_CENTER,
        spaceBefore=10,
        spaceAfter=10,
        leftIndent=40,
        rightIndent=40,
        leading=18,
    )
    style_box = ParagraphStyle(
        "EbookBox",
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=NAVY,
        alignment=TA_LEFT,
        leading=16,
    )

    # ── Función: página de portada ────────────────────────────────────
    def cover_page(canvas_obj, doc):
        canvas_obj.saveState()
        w, h = letter
        # Fondo navy
        canvas_obj.setFillColor(NAVY)
        canvas_obj.rect(0, 0, w, h, fill=1, stroke=0)
        # Franja dorada superior
        canvas_obj.setFillColor(GOLD)
        canvas_obj.rect(0, h - 8, w, 8, fill=1, stroke=0)
        # Franja dorada inferior
        canvas_obj.rect(0, 0, w, 8, fill=1, stroke=0)
        # Línea decorativa
        canvas_obj.setStrokeColor(GOLD)
        canvas_obj.setLineWidth(1)
        canvas_obj.line(60, h - 60, w - 60, h - 60)
        canvas_obj.line(60, 60, w - 60, 60)
        canvas_obj.restoreState()

    def normal_page(canvas_obj, doc):
        canvas_obj.saveState()
        w, h = letter
        # Header line
        canvas_obj.setStrokeColor(GOLD)
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(40, h - 40, w - 40, h - 40)
        # Footer
        canvas_obj.line(40, 40, w - 40, 40)
        canvas_obj.setFillColor(LGRAY)
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.drawString(40, 28, req.title[:60])
        canvas_obj.drawRightString(w - 40, 28, f"Página {doc.page - 1}")
        canvas_obj.restoreState()

    # ── Parsear el texto del ebook ────────────────────────────────────
    def parse_ebook(text):
        """Convierte el texto markdown-like del ebook en elementos ReportLab."""
        elements = []
        lines = text.split("\n")
        i = 0
        in_cover = True

        while i < len(lines):
            line = lines[i].strip()

            # Saltar líneas vacías
            if not line:
                if not in_cover:
                    elements.append(Spacer(1, 6))
                i += 1
                continue

            # Separador decorativo
            if line.startswith("────"):
                if not in_cover:
                    elements.append(HRFlowable(
                        width="100%", thickness=1,
                        color=GOLD, spaceAfter=8, spaceBefore=8
                    ))
                i += 1
                continue

            # Salto de página en capítulos
            if line.startswith("**CAPÍTULO") or line.startswith("**PLAN DE") or line.startswith("**CHECKLIST") or line.startswith("**RECURSOS") or line.startswith("**CONCLUSIÓN") or line.startswith("**OFERTA"):
                if not in_cover:
                    elements.append(PageBreak())
                in_cover = False

            # H1 bold doble asterisco
            if line.startswith("**") and line.endswith("**") and len(line) > 4:
                clean = line.strip("*").strip()
                if "BRIAN TRACY" in clean or "THE SUCCESS" in clean or "PRESENTS" in clean:
                    elements.append(Spacer(1, 30))
                    elements.append(Paragraph(clean, style_brand))
                elif len(clean) < 80 and (clean.isupper() or "CAPÍTULO" in clean or "INTRODUCCIÓN" in clean or "PLAN" in clean or "CONCLUSIÓN" in clean or "CHECKLIST" in clean or "RECURSOS" in clean or "OFERTA" in clean):
                    elements.append(Paragraph(clean, style_h1))
                else:
                    elements.append(Paragraph(f"<b>{clean}</b>", style_body))
                i += 1
                continue

            # Título principal (línea sola entre separadores)
            if line.startswith("**") and "**" in line[2:]:
                clean = re.sub(r"\*+", "", line).strip()
                if len(clean) < 100:
                    elements.append(Paragraph(clean, style_title))
                else:
                    elements.append(Paragraph(clean, style_h2))
                i += 1
                continue

            # Subtítulo en cursiva
            if line.startswith("*") and line.endswith("*") and not line.startswith("**"):
                clean = line.strip("*").strip()
                elements.append(Paragraph(f"<i>{clean}</i>", style_subtitle if in_cover else style_quote))
                i += 1
                continue

            # Cita (***"..."***)
            if line.startswith("***") or (line.startswith('"') and len(line) > 20):
                clean = re.sub(r"\*+", "", line).strip().strip('"').strip()
                elements.append(Spacer(1, 8))
                elements.append(HRFlowable(width="60%", thickness=0.5, color=GOLD, spaceAfter=6))
                elements.append(Paragraph(f'<i>"{clean}"</i>', style_quote))
                i += 1
                # Buscar el autor en la siguiente línea
                if i < len(lines) and lines[i].strip().startswith("**—"):
                    autor = lines[i].strip().strip("*").strip()
                    elements.append(Paragraph(autor, ParagraphStyle(
                        "autor", fontName="Helvetica-Bold", fontSize=10,
                        textColor=GOLD, alignment=TA_CENTER, spaceAfter=8
                    )))
                    i += 1
                elements.append(HRFlowable(width="60%", thickness=0.5, color=GOLD, spaceBefore=6))
                continue

            # Cuadro destacado (| **texto** |)
            if line.startswith("|") and "**" in line:
                clean = re.sub(r"[|*]", "", line).strip()
                if clean and clean != "---":
                    box_data = [[Paragraph(clean, style_box)]]
                    box = Table(box_data, colWidths=[5.5 * inch])
                    box.setStyle(TableStyle([
                        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#FFF8E7")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 16),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
                        ("TOPPADDING", (0, 0), (-1, -1), 12),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                        ("BOX", (0, 0), (-1, -1), 2, GOLD),
                        ("LINEABOVE", (0, 0), (-1, 0), 4, GOLD),
                    ]))
                    elements.append(Spacer(1, 8))
                    elements.append(box)
                    elements.append(Spacer(1, 8))
                i += 1
                continue

            # Subtema con ◆
            if line.startswith("**◆") or line.startswith("◆"):
                clean = re.sub(r"[*◆]", "", line).strip()
                elements.append(Spacer(1, 6))
                elements.append(Paragraph(f"◆  {clean}", style_h2))
                i += 1
                continue

            # Lista numerada
            if re.match(r"^\*\*\d+\.", line) or re.match(r"^\d+\.", line):
                clean = re.sub(r"[*]", "", line).strip()
                elements.append(Paragraph(f"&nbsp;&nbsp;&nbsp;{clean}", style_body))
                i += 1
                continue

            # Línea normal
            if line and line != "---" and not line.startswith("| ---"):
                clean = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
                clean = re.sub(r"\*(.+?)\*", r"<i>\1</i>", clean)
                clean = clean.replace("&", "&amp;").replace("<b>", "BOLD_OPEN").replace("</b>", "BOLD_CLOSE").replace("<i>", "IT_OPEN").replace("</i>", "IT_CLOSE")
                clean = clean.replace("BOLD_OPEN", "<b>").replace("BOLD_CLOSE", "</b>").replace("IT_OPEN", "<i>").replace("IT_CLOSE", "</i>")
                style = style_subtitle if in_cover else style_body
                elements.append(Paragraph(clean, style))

            i += 1

        return elements

    # ── Construir PDF ────────────────────────────────────────────────
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=1.0 * inch,
        rightMargin=1.0 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=req.title,
        author="The Success Institute",
    )

    story = parse_ebook(req.content)

    # Primera página con fondo navy, resto normal
    doc.build(story, onFirstPage=cover_page, onLaterPages=normal_page)

    pdf_bytes = buffer.getvalue()
    safe_title = re.sub(r"[^\w\-]", "_", req.title)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.pdf"'},
    )


# ─── Entrypoint local ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
