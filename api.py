import os
import logging
import httpx
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, Response, RedirectResponse
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

def make_client(timeout=30):
    proxies = {"http://": PROXY_URL, "https://": PROXY_URL} if PROXY_URL else {}
    if proxies:
        return httpx.AsyncClient(mounts={k: httpx.AsyncHTTPTransport(proxy=v) for k, v in proxies.items()}, timeout=timeout)
    return httpx.AsyncClient(timeout=timeout)

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

# ── HEALTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "rapidapi_keys": len(RAPIDAPI_KEYS)}

# ── INFO ────────────────────────────────────────────────────
@app.get("/info")
async def get_info(url: str):
    api_key = get_next_key()
    if not api_key:
        return JSONResponse({"error": "No API key"}, status_code=500)

    platform = detect_platform(url)

    async with make_client(30) as client:
        try:
            if platform == "youtube":
                vid = extract_yt_id(url)
                # Essayer yt-api pour info complète
                try:
                    r = await client.get(
                        "https://yt-api.p.rapidapi.com/dl",
                        params={"id": vid, "cgeo": "US"},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "yt-api.p.rapidapi.com"}
                    )
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("title"):
                            dur = int(d.get("lengthSeconds", 0) or 0)
                            return {
                                "title": d.get("title", ""),
                                "duration": dur,
                                "thumbnail": d.get("thumbnail", [{}])[-1].get("url") if d.get("thumbnail") else f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                                "uploader": d.get("author", ""),
                                "platform": "youtube"
                            }
                except Exception as e:
                    logger.warning(f"yt-api info failed: {e}")

                # Fallback youtube-mp36
                r2 = await client.get(
                    "https://youtube-mp36.p.rapidapi.com/dl",
                    params={"id": vid},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                )
                if r2.status_code == 200:
                    d2 = r2.json()
                    dur = int(float(d2.get("duration", 0) or 0))
                    return {
                        "title": d2.get("title", "YouTube"),
                        "duration": dur,
                        "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                        "uploader": "YouTube",
                        "platform": "youtube"
                    }

            elif platform == "tiktok":
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": url, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    return {
                        "title": d.get("title", "TikTok"),
                        "duration": int(d.get("duration", 0) or 0),
                        "thumbnail": d.get("cover") or d.get("origin_cover", ""),
                        "uploader": d.get("author", {}).get("nickname", "TikTok"),
                        "platform": "tiktok"
                    }

        except Exception as e:
            logger.error(f"Info error [{platform}]: {e}")

    return JSONResponse({"error": "Impossible d'analyser ce lien. Vérifie qu'il est public."}, status_code=400)

# ── DOWNLOAD ────────────────────────────────────────────────
@app.get("/download")
async def download(url: str, format: str = "mp4", quality: str = "720"):
    api_key = get_next_key()
    if not api_key:
        return JSONResponse({"error": "No API key"}, status_code=500)

    platform = detect_platform(url)

    async with make_client(90) as client:
        try:
            if platform == "youtube":
                vid = extract_yt_id(url)

                if format == "mp3":
                    # youtube-mp36 pour MP3
                    r = await client.get(
                        "https://youtube-mp36.p.rapidapi.com/dl",
                        params={"id": vid},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                    )
                    if r.status_code == 200:
                        d = r.json()
                        link = d.get("link")
                        if link:
                            title = d.get("title", "audio").replace("/", "-")
                            # Stream le fichier
                            async with client.stream("GET", link, follow_redirects=True) as stream:
                                if stream.status_code == 200:
                                    return StreamingResponse(
                                        stream.aiter_bytes(),
                                        headers={
                                            "Content-Disposition": f'attachment; filename="{title}.mp3"',
                                            "Content-Type": "audio/mpeg",
                                        }
                                    )
                else:
                    # MP4 via yt-api
                    try:
                        r = await client.get(
                            "https://yt-api.p.rapidapi.com/dl",
                            params={"id": vid, "cgeo": "US"},
                            headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "yt-api.p.rapidapi.com"}
                        )
                        if r.status_code == 200:
                            d = r.json()
                            formats = d.get("formats", []) + d.get("adaptiveFormats", [])
                            target_h = int(quality)
                            mp4s = [f for f in formats
                                    if f.get("mimeType", "").startswith("video/mp4")
                                    and f.get("url")
                                    and f.get("height", 0) <= target_h]
                            if mp4s:
                                best = sorted(mp4s, key=lambda x: x.get("height", 0), reverse=True)[0]
                                # Redirect direct vers l'URL de la vidéo
                                return RedirectResponse(url=best["url"], status_code=302)
                    except Exception as e:
                        logger.warning(f"yt-api MP4 failed: {e}")

                    # Fallback: youtube-mp36 redirect
                    r2 = await client.get(
                        "https://youtube-mp36.p.rapidapi.com/dl",
                        params={"id": vid, "format": "mp4"},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                    )
                    if r2.status_code == 200:
                        d2 = r2.json()
                        link = d2.get("link") or d2.get("url")
                        if link:
                            return RedirectResponse(url=link, status_code=302)

            elif platform == "tiktok":
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": url, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    if format == "mp3":
                        dl_url = d.get("music_info", {}).get("play") or d.get("wmplay") or d.get("play")
                    else:
                        dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")

                    if dl_url:
                        return RedirectResponse(url=dl_url, status_code=302)

        except Exception as e:
            logger.error(f"Download error [{platform}]: {e}")

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

# ── STATIC FILES ─────────────────────────────────────────────
app.mount("/", StaticFiles(directory=".", html=True), name="static")
