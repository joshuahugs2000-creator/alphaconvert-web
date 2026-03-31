import os
import logging
import httpx
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, Response
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
    if PROXY_URL:
        transport = httpx.AsyncHTTPTransport(proxy=PROXY_URL)
        return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=True)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)

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

async def resolve_tiktok_url(url: str) -> str:
    """Resolve short TikTok URLs (vt.tiktok.com, vm.tiktok.com) to full URL."""
    if "vt.tiktok.com" in url or "vm.tiktok.com" in url:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                return str(r.url)
        except Exception as e:
            logger.warning(f"Failed to resolve short TikTok URL: {e}")
    return url

def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()[:80]

# ── HEALTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "rapidapi_keys": len(RAPIDAPI_KEYS)}

# ── OPTIONS ─────────────────────────────────────────────────
@app.options("/info")
@app.options("/download")
@app.options("/chat")
async def options_handler():
    return Response(status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    })

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
                            thumb = (d.get("thumbnail") or [{}])[-1].get("url") or f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
                            return {
                                "title": d.get("title", ""),
                                "duration": dur,
                                "thumbnail": thumb,
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
                resolved_url = await resolve_tiktok_url(url)
                logger.info(f"TikTok URL resolved: {resolved_url[:80]}")
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved_url, "hd": "1"},
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
                else:
                    logger.error(f"TikTok scraper7 /info returned {r.status_code}: {r.text[:200]}")

        except Exception as e:
            logger.error(f"Info error [{platform}]: {e}")

    return JSONResponse({"error": "Impossible d'analyser ce lien."}, status_code=400)

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
                    # MP3 via youtube-mp36 — stream direct
                    r = await client.get(
                        "https://youtube-mp36.p.rapidapi.com/dl",
                        params={"id": vid},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                    )
                    if r.status_code == 200:
                        d = r.json()
                        link = d.get("link")
                        if link:
                            title = sanitize_filename(d.get("title", "audio"))
                            stream_resp = await client.get(link)
                            return StreamingResponse(
                                iter([stream_resp.content]),
                                headers={
                                    "Content-Disposition": f'attachment; filename="{title}.mp3"',
                                    "Content-Type": "audio/mpeg",
                                    "Content-Length": str(len(stream_resp.content)),
                                }
                            )

                else:
                    # MP4 via yt-api — stream pour forcer le téléchargement
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
                                dl_url = best["url"]
                                title = sanitize_filename(d.get("title", "video"))
                                # Stream en chunks pour forcer le téléchargement
                                # Download complet puis envoyer (évite les coupures 0-octet)
                                dl_resp = await client.get(dl_url)
                                if dl_resp.status_code == 200 and len(dl_resp.content) > 0:
                                    return StreamingResponse(
                                        iter([dl_resp.content]),
                                        headers={
                                            "Content-Disposition": f'attachment; filename="{title}.mp4"',
                                            "Content-Type": "video/mp4",
                                            "Content-Length": str(len(dl_resp.content)),
                                        }
                                    )
                                logger.warning(f"yt-api MP4 download empty, content length: {len(dl_resp.content)}")
                    except Exception as e:
                        logger.warning(f"yt-api MP4 failed: {e}")

                    # Fallback youtube-mp36
                    r2 = await client.get(
                        "https://youtube-mp36.p.rapidapi.com/dl",
                        params={"id": vid, "format": "mp4"},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                    )
                    if r2.status_code == 200:
                        d2 = r2.json()
                        link = d2.get("link") or d2.get("url")
                        if link:
                            title = sanitize_filename(d2.get("title", "video"))
                            stream_resp = await client.get(link)
                            return StreamingResponse(
                                iter([stream_resp.content]),
                                headers={
                                    "Content-Disposition": f'attachment; filename="{title}.mp4"',
                                    "Content-Type": "video/mp4",
                                    "Content-Length": str(len(stream_resp.content)),
                                }
                            )

            elif platform == "tiktok":
                resolved_url = await resolve_tiktok_url(url)
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved_url, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    title = sanitize_filename(d.get("title", "tiktok"))

                    if format == "mp3":
                        dl_url = d.get("music_info", {}).get("play") or d.get("wmplay") or d.get("play")
                        content_type = "audio/mpeg"
                        ext = "mp3"
                    else:
                        dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                        content_type = "video/mp4"
                        ext = "mp4"

                    if dl_url:
                        async with client.stream("GET", dl_url, headers={"User-Agent": "Mozilla/5.0"}) as stream:
                            headers = {
                                "Content-Disposition": f'attachment; filename="{title}.{ext}"',
                                "Content-Type": content_type,
                            }
                            ct = stream.headers.get("content-length")
                            if ct:
                                headers["Content-Length"] = ct
                            return StreamingResponse(stream.aiter_bytes(chunk_size=65536), headers=headers)
                else:
                    logger.error(f"TikTok download scraper7 returned {r.status_code}: {r.text[:200]}")

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
