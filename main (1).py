from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from gtts import gTTS
import subprocess, os, uuid, textwrap, httpx

app = FastAPI(title="StartVideoTube API", version="5.0")

class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    thumbnail_url: Optional[str] = ""
    fecha: Optional[str] = ""

def generate_srt(text: str, audio_duration: float) -> str:
    """Genera subtítulos SRT dividiendo el texto en bloques."""
    words = text.split()
    words_per_second = len(words) / audio_duration
    chunk_size = max(4, int(words_per_second * 3))  # ~3 segundos por bloque
    
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunks.append(' '.join(words[i:i+chunk_size]))
    
    srt = ""
    chunk_duration = audio_duration / len(chunks)
    
    for i, chunk in enumerate(chunks):
        start = i * chunk_duration
        end = (i + 1) * chunk_duration
        
        def fmt_time(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = int(t % 60)
            ms = int((t % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        
        srt += f"{i+1}\n{fmt_time(start)} --> {fmt_time(end)}\n{chunk}\n\n"
    
    return srt

def get_audio_duration(audio_file: str) -> float:
    """Obtiene la duración del audio en segundos."""
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_file
    ], capture_output=True, text=True)
    return float(result.stdout.strip())

@app.get("/health")
def health():
    return {"status": "ok", "service": "StartVideoTube v5"}

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
    video_name = f"video_{canal_slug}_{fecha}_{job_id}.mp4"
    video_path = f"{OUTPUT_DIR}/{video_name}"
    
    try:
        # 1. Generar audio con gTTS
        script_clean = req.script[:4000].replace('"', "'")
        tts = gTTS(text=script_clean, lang='es', slow=False)
        tts.save(audio_file)
        
        # 2. Obtener duración del audio
        duration = get_audio_duration(audio_file)
        
        # 3. Generar subtítulos SRT
        srt_content = generate_srt(script_clean, duration)
        with open(srt_file, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 4. Descargar imagen de fondo si se proporciona URL
        has_bg = False
        if req.thumbnail_url:
            try:
                response = httpx.get(req.thumbnail_url, timeout=15)
                if response.status_code == 200:
                    with open(bg_file, 'wb') as f:
                        f.write(response.content)
                    has_bg = True
            except:
                has_bg = False
        
        # 5. Generar video con ffmpeg
        if has_bg:
            # Video con imagen de fondo + audio + subtítulos
            subprocess.run([
                "ffmpeg", "-y",
                "-loop", "1", "-i", bg_file,
                "-i", audio_file,
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-c:a", "aac",
                "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,subtitles={srt_file}:force_style='FontName=Arial,FontSize=18,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Outline=2,Alignment=2,MarginV=80'",
                "-shortest",
                video_path
            ], check=True, capture_output=True, timeout=600)
        else:
            # Video con fondo negro degradado + subtítulos
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=0x1a1a2e:size=1080x1920:rate=30",
                "-i", audio_file,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", f"subtitles={srt_file}:force_style='FontName=Arial,FontSize=22,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Outline=2,Shadow=1,Alignment=2,MarginV=100'",
                "-shortest",
                video_path
            ], check=True, capture_output=True, timeout=600)
        
        # 6. Leer y devolver el video
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={video_name}",
                "X-Video-Name": video_name,
                "X-Duration": str(int(duration))
            }
        )
        
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Video generation timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for f in [audio_file, srt_file, bg_file]:
            if os.path.exists(f):
                os.remove(f)
        if os.path.exists(video_path):
            os.remove(video_path)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
