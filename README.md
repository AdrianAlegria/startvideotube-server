# StartVideoTube Video Server v3

Servidor de generación de videos para n8n.

## Endpoints

- POST /generate-video — Genera video con voz
- GET /video-status/{job_id} — Estado del job
- GET /video-file/{video_name} — Descarga el video

## Deploy en Railway

1. Sube este código a GitHub
2. Conecta Railway a tu repo
3. Railway detecta nixpacks.toml y instala ffmpeg automáticamente
4. URL permanente lista en 3 minutos
