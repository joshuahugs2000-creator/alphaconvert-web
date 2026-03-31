import os
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(name)s:%(message)s")
logger = logging.getLogger("api")

app = FastAPI()

# ── CORS ── en premier, avant tout
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

def extract_video_id(url: str):
    if "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0].split("&")[0]
    if "youtube.com/watch" in url:
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return params.get("v", [None])[0]
    return None

# ── PREFLIGHT OPTIONS explicite ──────────────────────────────
@app.options("/chat")
async def options_chat():
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )

@app.options("/info")
async def options_info():
    return Response(status_code=200, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"})

@app.options("/download")
async def options_download():
    return Response(status_code=200, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Access-Control-Allow-Headers": "*"})

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

    video_id = extract_video_id(url)
    # proxy géré directement
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "youtube-mp36.p.rapidapi.com"
    }

    async with httpx.AsyncClient(proxy=PROXY_URL if PROXY_URL else None, timeout=30) as client:
        try:
            resp = await client.get(
                "https://youtube-mp36.p.rapidapi.com/dl",
                params={"id": video_id or url},
                headers=headers
            )
            data = resp.json()
            if video_id and not data.get("thumbnail"):
                data["thumbnail"] = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            return data
        except Exception as e:
            logger.error(f"Info error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

# ── DOWNLOAD ────────────────────────────────────────────────
@app.get("/download")
async def download(url: str):
    api_key = get_next_key()
    if not api_key:
        return JSONResponse({"error": "No API key"}, status_code=500)

    video_id = extract_video_id(url)
    # proxy géré directement
    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": "youtube-mp36.p.rapidapi.com"
    }

    async with httpx.AsyncClient(proxy=PROXY_URL if PROXY_URL else None, timeout=60) as client:
        try:
            resp = await client.get(
                "https://youtube-mp36.p.rapidapi.com/dl",
                params={"id": video_id or url},
                headers=headers
            )
            data = resp.json()
            link = data.get("link")
            if not link:
                return JSONResponse({"error": "No download link"}, status_code=500)

            async with client.stream("GET", link) as r:
                filename = data.get("title", "audio").replace("/", "-") + ".mp3"
                return StreamingResponse(
                    r.aiter_bytes(),
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Type": "audio/mpeg",
                    }
                )
        except Exception as e:
            logger.error(f"Download error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

# ── CHAT → TELEGRAM ─────────────────────────────────────────
class ChatMessage(BaseModel):
    message: str
    user_email: str = "Anonyme"

@app.post("/chat")
async def send_chat(body: ChatMessage):
    if not SUPPORT_BOT_TOKEN or not SUPPORT_CHAT_ID:
        logger.error("SUPPORT_BOT_TOKEN ou SUPPORT_CHAT_ID manquant")
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
                json={
                    "chat_id": SUPPORT_CHAT_ID,
                    "text": text,
                    "parse_mode": "Markdown"
                }
            )
            result = resp.json()
            if result.get("ok"):
                return {"success": True}
            else:
                logger.error(f"Telegram error: {result}")
                return JSONResponse({"error": result.get("description", "telegram_error")}, status_code=500)
        except Exception as e:
            logger.error(f"Chat error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

# ── STATIC FILES ─────────────────────────────────────────────
# En dernier pour ne pas intercepter les routes API
app.mount("/", StaticFiles(directory=".", html=True), name="static")
