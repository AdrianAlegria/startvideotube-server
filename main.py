from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from gtts import gTTS
import subprocess, os, uuid

app = FastAPI(title="StartVideoTube API", version="4.0")

class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    fecha: Optional[str] = ""

@app.get("/health")
def health():
    return {"status": "ok", "service": "StartVideoTube v4"}

@app.post("/generate-video")
def create_video(req: VideoRequest):
    """
    Genera video sincrónicamente y devuelve el MP4 como respuesta binaria.
    n8n lo recibe directamente y lo sube a Drive.
    """
    job_id = str(uuid.uuid4())[:8]
    fecha = req.fecha or datetime.now().strftime("%Y-%m-%d_%H-%M")
    canal_slug = req.canal.replace(" ", "_").replace("/", "_")
    
    OUTPUT_DIR = "/tmp/videos"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    audio_file = f"/tmp/audio_{job_id}.mp3"
    video_name = f"video_{canal_slug}_{fecha}_{job_id}.mp4"
    video_path = f"{OUTPUT_DIR}/{video_name}"
    
    try:
        # Generar audio
        tts = gTTS(text=req.script[:3000], lang='es', slow=False)
        tts.save(audio_file)
        
        # Generar video negro con audio
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=c=black:size=1080x1920:rate=30",
            "-i", audio_file,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            video_path
        ], check=True, capture_output=True, timeout=300)
        
        if os.path.exists(audio_file):
            os.remove(audio_file)
        
        # Devolver el video como respuesta binaria directa
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        
        # Limpiar
        os.remove(video_path)
        
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={video_name}",
                "X-Video-Name": video_name
            }
        )
        
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Video generation timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(audio_file):
            os.remove(audio_file)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
