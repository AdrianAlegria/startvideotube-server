from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from gtts import gTTS
import subprocess, os, uuid, httpx, re

def clean_script_for_tts(text: str) -> str:
    """Elimina caracteres especiales que gTTS lee en voz alta."""
    # Reemplazar caracteres especiales comunes
    replacements = {
        '*': '', '#': '', '@': '', '!': '.', 
        '%': ' por ciento', '^': '', '&': ' y ',
        '|': '.', '~': '', '`': '', '=': '',
        '+': ' más ', '<': '', '>': '',
        '\\': '', '/': ' ', '_': ' ',
        '[': '', ']': '', '{': '', '}': '',
        '•': '.', '→': '.', '✅': '', '❌': '',
        '🔥': '', '📁': '', '🎯': '', '🕐': '',
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Eliminar emojis y símbolos Unicode
    text = re.sub(r'[^\w\s\.\,\;\:\-\?áéíóúÁÉÍÓÚñÑüÜ]', ' ', text)
    # Limpiar espacios múltiples
    text = re.sub(r'\s+', ' ', text).strip()
    return text


app = FastAPI(title="StartVideoTube API", version="5.2")

class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    thumbnail_url: Optional[str] = ""
    fecha: Optional[str] = ""

def generate_srt(text: str, audio_duration: float) -> str:
    words = text.split()
    if not words: return ""
    words_per_second = max(1, len(words) / audio_duration)
    # ~6 palabras por bloque para subtítulos más legibles
    chunk_size = max(5, min(8, int(words_per_second * 2.5)))
    chunks = [' '.join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]
    srt = ""
    chunk_duration = audio_duration / len(chunks)
    for i, chunk in enumerate(chunks):
        start = i * chunk_duration
        end = (i + 1) * chunk_duration
        def fmt(t):
            h,m,s,ms = int(t//3600),int((t%3600)//60),int(t%60),int((t%1)*1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        srt += f"{i+1}\n{fmt(start)} --> {fmt(end)}\n{chunk}\n\n"
    return srt

def get_audio_duration(audio_file: str) -> float:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_file
    ], capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except:
        return 60.0

def limit_script_for_tiktok(script: str, max_words: int = 400) -> str:
    """Limita el script a ~2 minutos (400 palabras a velocidad normal de habla)"""
    words = script.split()
    if len(words) > max_words:
        words = words[:max_words]
        return ' '.join(words)
    return script

@app.get("/health")
def health():
    return {"status": "ok", "service": "StartVideoTube v5.2"}

@app.post("/generate-video")
def create_video(req: VideoRequest):
    job_id = str(uuid.uuid4())[:8]
    fecha = req.fecha or datetime.now().strftime("%Y-%m-%d_%H-%M")
    canal_slug = req.canal.replace(" ", "_").replace("/", "_")
    
    OUTPUT_DIR = "/tmp/videos"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    audio_file = f"/tmp/audio_{job_id}.mp3"
    srt_file = f"/tmp/subs_{job_id}.srt"
    bg_file = f"/tmp/bg_{job_id}.jpg"
    video_path = f"{OUTPUT_DIR}/video_{canal_slug}_{fecha}_{job_id}.mp4"
    video_name = f"video_{canal_slug}_{fecha}_{job_id}.mp4"
    
    try:
        # 1. Limpiar y limitar script
        script_clean = clean_script_for_tts(req.script)
        script_clean = limit_script_for_tiktok(script_clean, max_words=450)
        
        # 2. Generar audio
        tts = gTTS(text=script_clean, lang='es', slow=False)
        tts.save(audio_file)
        
        # 3. Duración
        duration = get_audio_duration(audio_file)
        
        # 4. Subtítulos
        srt_content = generate_srt(script_clean, duration)
        with open(srt_file, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 5. Imagen de fondo
        has_bg = False
        if req.thumbnail_url and req.thumbnail_url.startswith('http'):
            try:
                resp = httpx.get(req.thumbnail_url, timeout=15)
                if resp.status_code == 200:
                    with open(bg_file, 'wb') as f:
                        f.write(resp.content)
                    has_bg = True
            except:
                has_bg = False
        
        # 6. Estilo subtítulos - grande, legible, EN LA PARTE BAJA sin tapar imagen
        # Alignment=2 = bottom center, MarginV=60 = margen desde abajo
        subtitle_style = (
            "FontName=Arial,"
            "FontSize=24,"
            "Bold=1,"
            "PrimaryColour=&HFFFFFF&,"
            "OutlineColour=&H000000&,"
            "BorderStyle=3,"
            "Outline=3,"
            "Shadow=1,"
            "Alignment=2,"
            "MarginV=60"
        )
        
        if has_bg:
            # Con imagen de fondo
            vf = (
                f"scale=1080:1920:force_original_aspect_ratio=decrease,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,"
                f"subtitles={srt_file}:force_style='{subtitle_style}'"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", bg_file,
                "-i", audio_file,
                "-c:v", "libx264",
                "-preset", "fast",
                "-b:v", "2000k",
                "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
                "-shortest",
                video_path
            ]
        else:
            # Fondo azul oscuro gradiente
            vf = (
                f"subtitles={srt_file}:force_style='{subtitle_style}'"
            )
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "color=c=0x0d1117:size=1080x1920:rate=30",
                "-i", audio_file,
                "-c:v", "libx264",
                "-preset", "fast",
                "-b:v", "2000k",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
                "-shortest",
                video_path
            ]
        
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={video_name}",
                "X-Duration": str(int(duration))
            }
        )
        
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg error: {e.stderr.decode()[:500] if e.stderr else str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for f in [audio_file, srt_file, bg_file]:
            if os.path.exists(f): os.remove(f)
        if os.path.exists(video_path): os.remove(video_path)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
