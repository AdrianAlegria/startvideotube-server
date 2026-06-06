from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from gtts import gTTS
import subprocess, os, uuid, httpx

app = FastAPI(title="StartVideoTube API", version="5.1")

class VideoRequest(BaseModel):
    script: str
    canal: str
    tema: str
    thumbnail_url: Optional[str] = ""
    fecha: Optional[str] = ""

def generate_srt(text: str, audio_duration: float) -> str:
    words = text.split()
    if not words:
        return ""
    words_per_second = max(1, len(words) / audio_duration)
    chunk_size = max(4, int(words_per_second * 3))
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

@app.get("/health")
def health():
    return {"status": "ok", "service": "StartVideoTube v5.1"}

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
        # 1. Generar audio
        script_clean = req.script[:4000]
        tts = gTTS(text=script_clean, lang='es', slow=False)
        tts.save(audio_file)
        
        # 2. Duración del audio
        duration = get_audio_duration(audio_file)
        
        # 3. Subtítulos SRT
        srt_content = generate_srt(script_clean, duration)
        with open(srt_file, 'w', encoding='utf-8') as f:
            f.write(srt_content)
        
        # 4. Descargar imagen de fondo
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
        
        # 5. Generar video
        # Subtítulos en la parte INFERIOR con MarginV grande para no tapar imagen
        subtitle_style = "FontName=Arial,FontSize=20,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=3,Outline=2,Shadow=1,Alignment=2,MarginV=40"
        
        if has_bg:
            vf = f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,subtitles={srt_file}:force_style='{subtitle_style}'"
            subprocess.run([
                "ffmpeg", "-y",
                "-loop", "1", "-i", bg_file,
                "-i", audio_file,
                "-c:v", "libx264", "-tune", "stillimage",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
                "-shortest", video_path
            ], check=True, capture_output=True, timeout=600)
        else:
            vf = f"subtitles={srt_file}:force_style='{subtitle_style}'"
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=0x1a1a2e:size=1080x1920:rate=30",
                "-i", audio_file,
                "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
                "-shortest", video_path
            ], check=True, capture_output=True, timeout=600)
        
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={"Content-Disposition": f"attachment; filename={video_name}"}
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        for f in [audio_file, srt_file, bg_file]:
            if os.path.exists(f): os.remove(f)
        if os.path.exists(video_path): os.remove(video_path)

@app.post("/generate-pdf")
def generate_pdf(request: dict):
    """Genera un PDF real desde HTML usando WeasyPrint"""
    try:
        from weasyprint import HTML
        content = request.get("content", "")
        title = request.get("title", "Documento")
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; line-height: 1.6; }}
                h1 {{ color: #1a1a2e; border-bottom: 3px solid #gold; padding-bottom: 10px; }}
                h2 {{ color: #2c3e50; margin-top: 30px; }}
                h3 {{ color: #34495e; }}
                table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
                th {{ background: #1a1a2e; color: white; padding: 10px; }}
                td {{ border: 1px solid #ddd; padding: 8px; }}
                tr:nth-child(even) {{ background: #f9f9f9; }}
                .header {{ background: #1a1a2e; color: white; padding: 20px; margin: -40px -40px 30px; }}
            </style>
        </head>
        <body>
            <div class="header"><h1 style="color:white;border:none">{title}</h1></div>
            {content}
        </body>
        </html>
        """
        
        pdf_bytes = HTML(string=html_content).write_pdf()
        
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={title}.pdf"}
        )
    except ImportError:
        raise HTTPException(status_code=500, detail="WeasyPrint not installed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
