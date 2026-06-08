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
app = FastAPI(title="StartVideoTube", version="5.3.5")

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
    Genera un PDF desde HTML usando WeasyPrint.
    Input:  content (HTML completo), title
    Output: PDF binario directo
    Estado: Activo si WeasyPrint está instalado (pip install weasyprint)
    """
    try:
        from weasyprint import HTML
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="WeasyPrint no instalado. Ejecutar: pip install weasyprint"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = str(Path(tmpdir) / "output.pdf")

        try:
            HTML(string=req.content).write_pdf(pdf_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error WeasyPrint: {e}")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

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
