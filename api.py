"""
api.py — Backend FastAPI pour AlphaConvert
"""
import os, re, logging, unicodedata, base64, random
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# ── PROXIES (rotation) ───────────────────────────────────────────────────────
# Variable Railway: PROXY_URLS = http://user:pass@host:port,http://user:pass@host2:port2
_raw_proxies = os.environ.get("PROXY_URLS", os.environ.get("PROXY_URL", ""))
PROXY_LIST = [p.strip() for p in _raw_proxies.split(",") if p.strip()]
logger.info(f"Proxies configurés: {len(PROXY_LIST)}")

def _get_proxy() -> str | None:
    return random.choice(PROXY_LIST) if PROXY_LIST else None

# ── COOKIES ──────────────────────────────────────────────────────────────────
def write_cookie(env_var: str, filename: str):
    val = os.environ.get(env_var, "")
    if not val:
        return None
    try:
        path = f"/tmp/{filename}"
        with open(path, "wb") as f:
            f.write(base64.b64decode(val))
        logger.info(f"Cookie écrit : {path}")
        return path
    except Exception as e:
        logger.warning(f"Cookie write failed ({filename}): {e}")
        return None

COOKIE_INSTAGRAM = write_cookie("COOKIES_INSTAGRAM", "ig_cookies.txt")
logger.info(f"Instagram cookie: {bool(COOKIE_INSTAGRAM)}")


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    elif "instagram.com" in u:
        return "instagram"
    elif "tiktok.com" in u:
        return "tiktok"
    return "unknown"


def safe_filename(name: str) -> str:
    name = unicodedata.normalize('NFKD', name)
    name = name.encode('ascii', 'ignore').decode('ascii')
    name = re.sub(r'[^\w\s\-.]', '_', name)
    return name.strip() or "video"


def _apply_instagram_opts(opts: dict) -> dict:
    """Applique impersonate + proxy + cookies pour Instagram."""
    opts["impersonate"] = ImpersonateTarget("chrome", "131")
    proxy = _get_proxy()
    if proxy:
        opts["proxy"] = proxy
        logger.info(f"Instagram : proxy activé → {proxy.split('@')[-1]}")
    else:
        logger.warning("Instagram : aucun proxy — risque de blocage")
    if COOKIE_INSTAGRAM:
        opts["cookiefile"] = COOKIE_INSTAGRAM
    return opts


@app.get("/info")
async def get_info(url: str):
    platform = detect_platform(url)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}

    if platform == "instagram":
        opts = _apply_instagram_opts(opts)

    try:
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
    platform = detect_platform(url)
    tpl = os.path.join(DOWNLOAD_PATH, "%(id)s.%(ext)s")
    base_opts = {
        "outtmpl": tpl,
        "quiet": False,
        "no_warnings": False,
        "restrictfilenames": True,
    }

    if platform == "instagram":
        base_opts = _apply_instagram_opts(base_opts)

    if format == "mp3":
        opts = {**base_opts, "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"}
    else:
        if platform == "instagram":
            fmt = "best[ext=mp4]/best"
        else:
            qmap = {
                "1080": "best[height<=1080][ext=mp4]/best[height<=1080]/best[ext=mp4]/best",
                "720":  "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best",
                "480":  "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
                "360":  "best[height<=360][ext=mp4]/best[height<=360]/best[ext=mp4]/best",
            }
            fmt = qmap.get(quality, qmap["720"])
        opts = {**base_opts, "format": fmt}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id", "video")
            for ext in [".mp4", ".mp3", ".mkv", ".webm", ".m4a"]:
                candidate = os.path.join(DOWNLOAD_PATH, f"{video_id}{ext}")
                if os.path.exists(candidate):
                    title = safe_filename(info.get("title", "video"))
                    dl_name = f"{title}{ext}"
                    return FileResponse(
                        candidate,
                        media_type="application/octet-stream",
                        filename=dl_name,
                        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'}
                    )
        raise HTTPException(status_code=500, detail="Fichier introuvable")
    except Exception as e:
        logger.error(f"download error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok"}
