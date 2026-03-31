import os
import logging
import httpx
import urllib.parse
from fastapi import FastAPI
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
    kwargs = dict(timeout=timeout, follow_redirects=True)
    if PROXY_URL:
        kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=PROXY_URL)
    return httpx.AsyncClient(**kwargs)

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

async def resolve_tiktok(url: str) -> str:
    """Résout les liens courts TikTok (vt/vm) en URL complète."""
    if not any(x in url for x in ["vt.tiktok.com", "vm.tiktok.com"]):
        return url
    for ua in [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "TikTok/26.2.0 (iPhone; iOS 17.0)",
    ]:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": ua})
                resolved = str(r.url)
                if "@" in resolved or "/video/" in resolved:
                    logger.info(f"TikTok resolved → {resolved[:80]}")
                    return resolved
        except Exception as e:
            logger.warning(f"TikTok resolve attempt failed: {e}")
    logger.warning(f"Could not resolve TikTok short URL, using as-is: {url}")
    return url

def sanitize(name: str) -> str:
    return "".join(c for c in name if c not in r'\/:*?"<>|').strip()[:80] or "video"

def stream_response(dl_url: str, filename: str, mime: str, req_headers: dict = {}):
    """
    Crée un StreamingResponse correct.
    FIX 1 : Range: bytes=0-  → les CDN vidéo (googlevideo, tiktok) renvoient
             un corps vide sans ce header.
    FIX 2 : Vérification du status HTTP dans le générateur → évite les
             fichiers 0 octet silencieux quand le CDN répond 403/416.
    """
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "*/*",
        "Accept-Encoding": "identity",   # pas de gzip → taille exacte
        "Range":           "bytes=0-",   # ← FIX 1 : indispensable pour googlevideo & co.
        **req_headers
    }
    safe = filename.encode("ascii", errors="replace").decode("ascii")

    async def gen():
        # Timeout long pour les grosses vidéos (lecture 10 min)
        t = httpx.Timeout(connect=15, read=600, write=60, pool=15)
        async with httpx.AsyncClient(timeout=t, follow_redirects=True) as client:
            async with client.stream("GET", dl_url, headers=headers) as resp:
                logger.info(
                    f"CDN {resp.status_code} — "
                    f"{resp.headers.get('content-length', '?')}B — {filename}"
                )
                # FIX 2 : ne pas yielder si le CDN refuse
                if resp.status_code not in (200, 206):
                    logger.error(f"CDN refused {resp.status_code} → {dl_url[:100]}")
                    return
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    return StreamingResponse(
        gen(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{safe}"'}
    )

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
        "Access-Control-Allow-Origin":  "*",
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
                r = await client.get(
                    "https://youtube-mp36.p.rapidapi.com/dl",
                    params={"id": vid},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                )
                if r.status_code == 200:
                    d = r.json()
                    return {
                        "title":     d.get("title", "YouTube"),
                        "duration":  int(float(d.get("duration", 0) or 0)),
                        "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                        "platform":  "youtube"
                    }

            elif platform == "tiktok":
                resolved = await resolve_tiktok(url)
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                logger.info(f"TikTok /info HTTP {r.status_code}")

                if r.status_code == 200:
                    body     = r.json()
                    # FIX 3 : vérifier le code métier de l'API, pas seulement HTTP 200
                    api_code = body.get("code", 0)
                    if api_code != 0:
                        logger.error(
                            f"TikTok API code {api_code}: {body.get('msg', '')} | "
                            f"url={resolved[:80]}"
                        )
                    else:
                        # FIX 4 : certaines versions mettent data à la racine du JSON
                        d = body.get("data") or body
                        if isinstance(d, dict) and (d.get("title") or d.get("id")):
                            return {
                                "title":     d.get("title") or d.get("desc", "TikTok"),
                                "duration":  int(d.get("duration", 0) or 0),
                                "thumbnail": d.get("cover") or d.get("origin_cover", ""),
                                "platform":  "tiktok"
                            }
                        logger.error(f"TikTok data vide/inattendu: {str(body)[:200]}")
                else:
                    logger.error(f"TikTok info HTTP {r.status_code}: {r.text[:200]}")

        except Exception as e:
            logger.error(f"Info [{platform}]: {e}")

    return JSONResponse({"error": "Impossible d'analyser ce lien."}, status_code=400)

# ── DOWNLOAD ────────────────────────────────────────────────
@app.get("/download")
async def download(url: str, format: str = "mp4", quality: str = "720"):
    api_key = get_next_key()
    if not api_key:
        return JSONResponse({"error": "No API key"}, status_code=500)

    platform = detect_platform(url)

    async with make_client(60) as client:
        try:
            # ── YOUTUBE ────────────────────────────────────────
            if platform == "youtube":
                vid = extract_yt_id(url)

                if format == "mp3":
                    r = await client.get(
                        "https://youtube-mp36.p.rapidapi.com/dl",
                        params={"id": vid},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}
                    )
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("link"):
                            return stream_response(
                                d["link"],
                                f"{sanitize(d.get('title', 'audio'))}.mp3",
                                "audio/mpeg"
                            )

                else:  # MP4
                    r = await client.get(
                        "https://yt-api.p.rapidapi.com/dl",
                        params={"id": vid, "cgeo": "US"},
                        headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "yt-api.p.rapidapi.com"}
                    )
                    logger.info(f"yt-api HTTP {r.status_code}")

                    if r.status_code == 200:
                        d       = r.json()
                        all_fmt = d.get("formats", []) + d.get("adaptiveFormats", [])
                        target  = int(quality)

                        # Priorité : MP4 muxé (audio + vidéo dans le même flux)
                        candidates = [
                            f for f in all_fmt
                            if f.get("mimeType", "").startswith("video/mp4")
                            and f.get("url") and f.get("audioQuality")
                            and f.get("height", 0) <= target
                        ]
                        # Fallback : n'importe quel MP4 sous la qualité demandée
                        if not candidates:
                            candidates = [
                                f for f in all_fmt
                                if f.get("mimeType", "").startswith("video/mp4")
                                and f.get("url") and f.get("height", 0) <= target
                            ]

                        if candidates:
                            best  = max(candidates, key=lambda x: x.get("height", 0))
                            title = sanitize(d.get("title", "video"))
                            logger.info(f"YT MP4 {best.get('height')}p → stream")
                            # FIX 5 : Referer requis par le CDN googlevideo.com
                            return stream_response(
                                best["url"],
                                f"{title}.mp4",
                                "video/mp4",
                                req_headers={
                                    "Referer": "https://www.youtube.com/",
                                    "Origin":  "https://www.youtube.com",
                                }
                            )
                        else:
                            logger.warning(f"yt-api: aucun format MP4 trouvé pour {vid}")
                    else:
                        logger.warning(f"yt-api HTTP {r.status_code}: {r.text[:200]}")

                    # FIX 6 : le fallback youtube-mp36 EST SUPPRIMÉ pour MP4
                    # Cette API ne fournit que des liens MP3 → servis en .mp4 = fichier 0 octet / corrompu

            # ── TIKTOK ─────────────────────────────────────────
            elif platform == "tiktok":
                resolved = await resolve_tiktok(url)
                r = await client.get(
                    "https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": resolved, "hd": "1"},
                    headers={"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}
                )
                logger.info(f"TikTok /download HTTP {r.status_code}")

                if r.status_code == 200:
                    body     = r.json()
                    # FIX 7 : même vérification du code métier que /info
                    api_code = body.get("code", 0)
                    if api_code != 0:
                        logger.error(
                            f"TikTok DL API code {api_code}: {body.get('msg', '')} | "
                            f"url={resolved[:80]}"
                        )
                    else:
                        # FIX 8 : gestion structure data imbriquée ou à la racine
                        d     = body.get("data") or body
                        title = sanitize(d.get("title") or d.get("desc") or "tiktok")

                        if format == "mp3":
                            dl_url = (
                                (d.get("music_info") or {}).get("play")
                                or d.get("wmplay")
                                or d.get("play")
                            )
                            mime, ext = "audio/mpeg", "mp3"
                        else:
                            dl_url = (
                                d.get("hdplay")
                                or d.get("play")
                                or d.get("wmplay")
                            )
                            mime, ext = "video/mp4", "mp4"

                        if dl_url:
                            return stream_response(
                                dl_url,
                                f"{title}.{ext}",
                                mime,
                                req_headers={
                                    "Referer": "https://www.tiktok.com/",
                                    "Origin":  "https://www.tiktok.com",
                                }
                            )
                        else:
                            logger.error(
                                f"TikTok: aucun dl_url dans data={str(d)[:200]}"
                            )
                else:
                    logger.error(f"TikTok download HTTP {r.status_code}: {r.text[:200]}")

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
