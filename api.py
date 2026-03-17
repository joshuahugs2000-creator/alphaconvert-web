"""
api.py — Backend FastAPI pour AlphaConvert
Lance avec : uvicorn api:app --reload --port 8000
"""
import os, re, logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API")

# CORS — autorise le site à appeler l'API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    elif "instagram.com" in u:
        return "Instagram"
    elif "tiktok.com" in u:
        return "TikTok"
    return "Inconnu"


@app.get("/info")
async def get_info(url: str):
    """Retourne les infos d'une vidéo sans télécharger."""
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "title":     info.get("title", "Vidéo"),
            "duration":  info.get("duration", 0),
            "thumbnail": info.get("thumbnail"),
            "uploader":  info.get("uploader", ""),
            "platform":  detect_platform(url),
        }
    except Exception as e:
        logger.error(f"get_info error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/download")
async def download(url: str, format: str = "mp4", quality: str = "720"):
    """Télécharge et retourne le fichier vidéo ou audio."""
    tpl = os.path.join(DOWNLOAD_PATH, "%(id)s_%(title).60s.%(ext)s")

    base_opts = {
        "outtmpl": tpl,
        "quiet": False,
        "no_warnings": False,
    }

    if format == "mp3":
        opts = {
            **base_opts,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    else:
        qmap = {
            "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
            "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
            "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]/best",
        }
        opts = {
            **base_opts,
            "format": qmap.get(quality, qmap["720"]),
            "merge_output_format": "mp4",
        }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            base = re.sub(r'\.\w+$', '', path)
            for ext in [".mp4", ".mp3", ".mkv", ".webm", ".m4a"]:
                candidate = base + ext
                if os.path.exists(candidate):
                    filename = os.path.basename(candidate)
                    return FileResponse(
                        candidate,
                        media_type="application/octet-stream",
                        filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
                    )
        raise HTTPException(status_code=500, detail="Fichier introuvable après téléchargement")
    except Exception as e:
        logger.error(f"download error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}
