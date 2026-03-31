import os
import logging
import httpx
import urllib.parse
import asyncio
import tempfile
import shutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(name)s:%(message)s")
logger = logging.getLogger("api")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=86400,
)

# ── CONFIG ──────────────────────────────────────────────────
RAPIDAPI_KEYS     = [k.strip() for k in os.getenv("RAPIDAPI_KEYS", "").split(",") if k.strip()]
PROXY_URL         = os.getenv("PROXY_URL", "")
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "")
SUPPORT_CHAT_ID   = os.getenv("SUPPORT_CHAT_ID", "")

logger.info(f"Proxies: {1 if PROXY_URL else 0} | RapidAPI keys: {len(RAPIDAPI_KEYS)}")

current_key_index = 0

def get_next_key():
    global current_key_index
    if not RAPIDAPI_KEYS:
        return None
    key = RAPIDAPI_KEYS[current_key_index % len(RAPIDAPI_KEYS)]
    current_key_index += 1
    return key

def extract_yt_id(url: str):
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0].split("&")[0]
    if "youtube.com/watch" in url:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return params.get("v", [None])[0]
    return None

def detect_platform(url: str):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u or "vt.tiktok" in u:
        return "tiktok"
    return "unknown"

def sanitize(name: str) -> str:
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()[:80] or "video"

async def resolve_tiktok(url: str) -> str:
    if not any(x in url for x in ["vt.tiktok.com", "vm.tiktok.com"]):
        return url
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resolved = str(r.url)
            if "@" in resolved or "/video/" in resolved:
                return resolved
    except Exception as e:
        logger.warning(f"TikTok resolve failed: {e}")
    return url

def stream_response(dl_url: str, filename: str, mime: str, req_headers: dict = {}):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Encoding": "identity",
        **req_headers
    }
    safe = filename.encode("ascii", errors="replace").decode("ascii")

    async def gen():
        t = httpx.Timeout(connect=15, read=600, write=60, pool=15)
        async with httpx.AsyncClient(timeout=t, follow_redirects=True) as client:
            async with client.stream("GET", dl_url, headers=headers) as resp:
                logger.info(f"Streaming {resp.status_code} — {resp.headers.get('content-length','?')}B — {filename}")
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    return StreamingResponse(
        gen(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'}
    )

# ── yt-dlp helpers ──────────────────────────────────────────
def _ydl_opts_base():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts

async def ytdlp_info(url: str) -> dict:
    """Récupère les infos d'une vidéo YouTube via yt-dlp (thread séparé)."""
    import yt_dlp

    def _run():
        opts = _ydl_opts_base()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    return await asyncio.get_event_loop().run_in_executor(None, _run)

async def ytdlp_download(url: str, fmt_selector: str, out_path: str) -> str:
    """Télécharge avec yt-dlp et retourne le chemin du fichier."""
    import yt_dlp

    def _run():
        opts = _ydl_opts_base()
        opts["format"] = fmt_selector
        opts["outtmpl"] = out_path + ".%(ext)s"
        opts["merge_output_format"] = "mp4"
        # Pas de ffmpeg requis pour les formats avec audio intégré
        opts["prefer_free_formats"] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info).replace(".webm", ".mp4").replace(".mkv", ".mp4")

    return await asyncio.get_event_loop().run_in_executor(None, _run)

# ── HEALTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "keys": len(RAPIDAPI_KEYS)}

# ── OPTIONS ─────────────────────────────────────────────────
@app.options("/info")
@app.options("/download")
@app.options("/chat")
async def preflight():
    return Response(status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })

# ── INFO ────────────────────────────────────────────────────
@app.get("/info")
async def get_info(url: str):
    platform = detect_platform(url)

    try:
        if platform == "youtube":
            # yt-dlp pour les infos YouTube (gratuit, fiable)
            info = await ytdlp_info(url)
            vid = extract_yt_id(url)
            return {
                "title":     info.get("title", "YouTube"),
                "duration":  int(info.get("duration") or 0),
                "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                "platform":  "youtube"
            }

        elif platform == "tiktok":
            api_key = get_next_key()
            if not api_key:
                return JSONResponse({"error": "No API key"}, status_code=500)
            resolved = await resolve_tiktok(url)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    return {
                        "title":     d.get("title", "TikTok"),
                        "duration":  int(d.get("duration", 0) or 0),
                        "thumbnail": d.get("cover") or d.get("origin_cover", ""),
                        "platform":  "tiktok"
                    }
                logger.error(f"TikTok info {r.status_code}: {r.text[:200]}")

    except Exception as e:
        logger.error(f"Info [{platform}]: {e}")

    return JSONResponse({"error": "Impossible d'analyser ce lien."}, status_code=400)

# ── DOWNLOAD ────────────────────────────────────────────────
@app.get("/download")
async def download(url: str, format: str = "mp4", quality: str = "720"):
    platform = detect_platform(url)

    try:
        # ── YOUTUBE ────────────────────────────────────────────
        if platform == "youtube":
            tmpdir = tempfile.mkdtemp()
            try:
                out_base = os.path.join(tmpdir, "video")

                if format == "mp3":
                    # MP3 : meilleur audio disponible
                    fmt_selector = "bestaudio[ext=m4a]/bestaudio/best"
                    file_path = await ytdlp_download(url, fmt_selector, out_base)
                    # Cherche le fichier téléchargé
                    files = os.listdir(tmpdir)
                    if not files:
                        raise Exception("No file downloaded")
                    file_path = os.path.join(tmpdir, files[0])
                    filename = sanitize(os.path.splitext(files[0])[0]) + ".mp3"
                    return FileResponse(
                        file_path,
                        media_type="audio/mpeg",
                        filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
                    )
                else:
                    # MP4 : on choisit la qualité demandée
                    q = int(quality)
                    # Format : video ≤ qualité demandée + audio, merge en mp4
                    fmt_selector = f"bestvideo[height<={q}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={q}]+bestaudio/best[height<={q}]/best"
                    file_path = await ytdlp_download(url, fmt_selector, out_base)

                    files = os.listdir(tmpdir)
                    if not files:
                        raise Exception("No file downloaded")
                    file_path = os.path.join(tmpdir, files[0])
                    filename = sanitize(os.path.splitext(files[0])[0]) + ".mp4"
                    return FileResponse(
                        file_path,
                        media_type="video/mp4",
                        filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
                    )
            except Exception as e:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise e

        # ── TIKTOK ─────────────────────────────────────────────
        elif platform == "tiktok":
            api_key = get_next_key()
            if not api_key:
                return JSONResponse({"error": "No API key"}, status_code=500)

            resolved = await resolve_tiktok(url)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d     = r.json().get("data", {})
                    title = sanitize(d.get("title", "tiktok"))
                    if format == "mp3":
                        dl_url = (d.get("music_info") or {}).get("play") or d.get("wmplay") or d.get("play")
                        mime, ext = "audio/mpeg", "mp3"
                    else:
                        dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                        mime, ext = "video/mp4", "mp4"

                    if dl_url:
                        return stream_response(
                            dl_url, f"{title}.{ext}", mime,
                            req_headers={"Referer": "https://www.tiktok.com/", "Origin": "https://www.tiktok.com"}
                        )
                else:
                    logger.error(f"TikTok dl {r.status_code}: {r.text[:200]}")

    except Exception as e:
        logger.error(f"Download [{platform}]: {e}")

    return JSONResponse({"error": "Téléchargement impossible"}, status_code=500)

# ── CHAT → TELEGRAM ─────────────────────────────────────────
class ChatMessage(BaseModel):
    message: str
    user_email: str = "Anonyme"

@app.post("/chat")
async def send_chat(body: ChatMessage):
    if not SUPPORT_BOT_TOKEN or not SUPPORT_CHAT_ID:
        return JSONResponse({"error": "config_missing"}, status_code=500)
    text = (
        f"💬 *Message Support AlphaConvert*\n\n"
        f"👤 *De:* {body.user_email}\n"
        f"📝 *Message:* {body.message}"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                f"https://api.telegram.org/bot{SUPPORT_BOT_TOKEN}/sendMessage",
                json={"chat_id": SUPPORT_CHAT_ID, "text": text, "parse_mode": "Markdown"}
            )
            result = resp.json()
            if result.get("ok"):
                return {"success": True}
            return JSONResponse({"error": result.get("description", "telegram_error")}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

# ── STATIC ──────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=".", html=True), name="static")
