from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from gtts import gTTS
import subprocess, os, uuid, httpx, asyncio

app = FastAPI(title="StartVideoTube API", version="3.0")

JOBS = {}

# ─── Google Drive Upload via n8n webhook ───────────────────────────
# No necesitamos credenciales de Drive en el servidor
# El servidor genera el video y lo devuelve como bytes
# n8n lo recibe y lo sube a Drive

class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    fecha: Optional[str] = ""

def generate_video_task(job_id: str, script: str, canal: str, tema: str, fecha: str):
    try:
        JOBS[job_id]["status"] = "processing"
        
        # Directorio temporal en Railway
        OUTPUT_DIR = "/tmp/videos"
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        canal_slug = canal.replace(" ", "_").replace("/", "_")
        audio_file = f"/tmp/audio_{job_id}.mp3"
        video_name = f"video_{canal_slug}_{fecha}_{job_id[:8]}.mp4"
        video_path = os.path.join(OUTPUT_DIR, video_name)
        
        # Generar audio con gTTS
        tts = gTTS(text=script[:3000], lang='es', slow=False)
        tts.save(audio_file)
        
        # Generar video con ffmpeg - fondo negro + audio
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=c=black:size=1080x1920:rate=30",
            "-i", audio_file,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420x",
            "-shortest",
            video_path
        ], check=True, capture_output=True)
        
        if os.path.exists(audio_file):
            os.remove(audio_file)
            
        JOBS[job_id].update({
            "status": "done",
            "video_path": video_path,
            "video_name": video_name,
            "download_url": f"/video-file/{video_name}",
            "error": ""
        })
        
    except Exception as e:
        JOBS[job_id].update({"status": "error", "error": str(e)})

@app.get("/health")
def health():
    return {"status": "ok", "service": "StartVideoTube v3"}

@app.post("/generate-video")
def create_video(req: VideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    fecha = req.fecha or datetime.now().strftime("%Y-%m-%d_%H-%M")
    JOBS[job_id] = {"status": "pending", "video_path": "", "video_name": "", "download_url": "", "error": ""}
    background_tasks.add_task(generate_video_task, job_id, req.script, req.canal, req.tema, fecha)
    return {"job_id": job_id, "status": "pending"}

@app.get("/video-status/{job_id}")
def get_status(job_id: str):
    if job_id not in JOBS:
        return {"status": "not_found"}
    return {"job_id": job_id, **JOBS[job_id]}

@app.get("/video-file/{video_name}")
def download_video(video_name: str):
    from fastapi.responses import FileResponse
    video_path = f"/tmp/videos/{video_name}"
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video not found or expired")
    return FileResponse(
        video_path,
        media_type="video/mp4",
        filename=video_name,
        headers={"Content-Disposition": f"attachment; filename={video_name}"}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
