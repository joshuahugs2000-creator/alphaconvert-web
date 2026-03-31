"""
api.py — Backend FastAPI AlphaConvert
Fixes définitifs:
  1. Suppression totale de la variable PROXY_URL (elle polluait yt-dlp)
  2. Nettoyage des variables d'env proxy AVANT d'appeler yt-dlp (Railway injecte des proxys)
  3. Endpoint /thumbnail-proxy pour servir les miniatures YouTube (contourne le CORS)
  4. ffmpeg_location forcé avec recherche exhaustive
  5. Glob robuste pour trouver le fichier téléchargé
"""
import os, re, logging, unicodedata, httpx, urllib.parse, time, glob, uuid, shutil
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

# ── RATE LIMIT ────────────────────────────────────────────────────────────────
_rate_store: dict = defaultdict(list)
_ban_store:  dict = {}
RATE_LIMIT = 15; RATE_WINDOW = 60; BAN_AT = 40; BAN_DURATION = 300

def _get_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else request.client.host

async def rate_limit(request: Request):
    ip = _get_ip(request); now = time.time()
    if ip in _ban_store:
        if now < _ban_store[ip]:
            raise HTTPException(status_code=429, detail=f"Trop de requetes. Reessaie dans {int(_ban_store[ip]-now)}s.")
        del _ban_store[ip]
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
    if len(_rate_store[ip]) >= BAN_AT:
        _ban_store[ip] = now + BAN_DURATION
        raise HTTPException(status_code=429, detail="Abus detecte.")
    if len(_rate_store[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Limite atteinte.")
    _rate_store[ip].append(now)

async def check_ua(request: Request):
    ua = request.headers.get("user-agent", "").lower()
    for p in ["sqlmap", "nikto", "nmap", "masscan", "scrapy", "dirbuster"]:
        if p in ua:
            raise HTTPException(status_code=403, detail="Acces refuse.")

ALLOWED_DOMAINS = [
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"
]
DANGEROUS = ["javascript:", "data:", "file://", "../", "..\\",
             "127.0.0.1", "0.0.0.0", "169.254.", "192.168.", "10.0."]

def validate_url(url: str) -> str:
    if not url or len(url) > 500:
        raise HTTPException(status_code=400, detail="URL invalide.")
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="URL invalide.")
    for d in DANGEROUS:
        if d in url.lower():
            raise HTTPException(status_code=400, detail="URL non autorisee.")
    try:
        parsed = urllib.parse.urlparse(url)
        if not any(domain in parsed.netloc.lower() for domain in ALLOWED_DOMAINS):
            raise HTTPException(status_code=400, detail="YouTube et TikTok uniquement.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="URL malformee.")
    return url

SECURITY = [Depends(rate_limit), Depends(check_ua)]

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

_raw_keys     = os.environ.get("RAPIDAPI_KEYS", os.environ.get("RAPIDAPI_KEY", ""))
RAPIDAPI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_rapi_idx     = 0

def _get_rapidapi_key():
    global _rapi_idx
    if not RAPIDAPI_KEYS:
        return None
    key = RAPIDAPI_KEYS[_rapi_idx % len(RAPIDAPI_KEYS)]
    _rapi_idx += 1
    return key

# Cherche ffmpeg dans tous les emplacements possibles sur Railway / Nix
def _find_ffmpeg() -> str:
    candidates = [
        shutil.which("ffmpeg"),
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/nix/var/nix/profiles/default/bin/ffmpeg",
    ]
    # Cherche aussi dans /nix/store
    nix_results = glob.glob("/nix/store/*/bin/ffmpeg")
    candidates.extend(nix_results)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""

FFMPEG_PATH = _find_ffmpeg()
FFMPEG_OK   = bool(FFMPEG_PATH)
logger.info(f"ffmpeg={'FOUND: ' + FFMPEG_PATH if FFMPEG_OK else 'NOT FOUND'}")


def _clean_proxy_env():
    """
    Supprime toutes les variables d'environnement proxy AVANT d'appeler yt-dlp.
    Railway injecte parfois des proxys qui bloquent YouTube.
    IMPORTANT: on restaure après pour ne pas casser le reste de l'app.
    """
    proxy_vars = [k for k in os.environ if 'proxy' in k.lower()]
    saved = {k: os.environ.pop(k) for k in proxy_vars}
    return saved

def _restore_proxy_env(saved: dict):
    os.environ.update(saved)


# ── HELPERS ───────────────────────────────────────────────────────────────────
def clean_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url.strip())
        params = urllib.parse.parse_qs(parsed.query)
        if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
            cp = {k: v for k, v in params.items() if k == "v"}
            return parsed._replace(query=urllib.parse.urlencode(cp, doseq=True)).geturl()
        if "tiktok.com" in parsed.netloc or "vm.tiktok" in parsed.netloc:
            return parsed._replace(query="", fragment="").geturl()
    except Exception:
        pass
    return url.strip()

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u:
        return "tiktok"
    return "unknown"

def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^\w\s\-.]", "_", name).strip()[:60] or "video"

def _extract_yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""

def _ydl_base(uid: str) -> dict:
    """Options yt-dlp de base — SANS proxy."""
    opts = {
        "outtmpl":          os.path.join(DOWNLOAD_PATH, f"{uid}.%(ext)s"),
        "quiet":            True,
        "no_warnings":      True,
        "noplaylist":       True,
        "restrictfilenames": False,
        "proxy":            "",   # Force aucun proxy — clé pour Railway
    }
    if FFMPEG_OK:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts

def _find_file(uid: str):
    """Trouve le fichier téléchargé par yt-dlp (peu importe l'extension)."""
    files = [
        f for f in glob.glob(os.path.join(DOWNLOAD_PATH, f"{uid}*"))
        if os.path.isfile(f) and os.path.getsize(f) > 1024
    ]
    return max(files, key=os.path.getsize) if files else None

def _serve(path: str, title: str) -> FileResponse:
    ext     = os.path.splitext(path)[1]
    dl_name = f"{safe_filename(title)}{ext}"
    size    = os.path.getsize(path)
    logger.info(f"Serving: {dl_name} ({size:,} bytes)")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=dl_name,
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )

def _save_stream(dl_url: str, title: str, ext: str) -> str:
    """Télécharge un flux HTTP direct et le sauvegarde."""
    path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex[:8]}{ext}")
    with httpx.stream(
        "GET", dl_url, timeout=120, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"}
    ) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)
    return path

def _tiktok_rapidapi(url: str, fmt: str):
    """Fallback RapidAPI pour TikTok."""
    key = _get_rapidapi_key()
    if not key:
        return None, "tiktok"
    try:
        r = httpx.get(
            "https://tiktok-scraper7.p.rapidapi.com/video/info",
            params={"url": url, "hd": "1"},
            headers={
                "X-RapidAPI-Key":  key,
                "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com",
            },
            timeout=30,
        )
        if r.status_code == 200:
            d     = r.json().get("data", {})
            title = d.get("title", "tiktok")
            if fmt == "mp3":
                dl_url = (d.get("music_info") or {}).get("play") or d.get("wmplay") or d.get("play")
                ext    = ".mp3"
            else:
                dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                ext    = ".mp4"
            if dl_url:
                return _save_stream(dl_url, title, ext), title
    except Exception as e:
        logger.error(f"TikTok RapidAPI: {e}")
    return None, "tiktok"


def _youtube_rapidapi_info(url: str) -> dict | None:
    """
    Récupère les infos d'une vidéo YouTube via RapidAPI (YouTube v3).
    Utilisé en fallback quand yt-dlp est bloqué.
    Variable d'env requise : RAPIDAPI_KEYS
    API utilisée : youtube-media-downloader.p.rapidapi.com
    """
    key = _get_rapidapi_key()
    if not key:
        return None
    vid = _extract_yt_id(url)
    if not vid:
        return None
    try:
        r = httpx.get(
            "https://youtube-media-downloader.p.rapidapi.com/v2/video/details",
            params={"videoId": vid},
            headers={
                "X-RapidAPI-Key":  key,
                "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com",
            },
            timeout=20,
        )
        if r.status_code == 200:
            d = r.json()
            if not d.get("status"):
                return None
            thumb_url = ""
            thumbnails = d.get("thumbnails", [])
            if thumbnails:
                # Prend la meilleure qualité
                thumb_url = thumbnails[-1].get("url", "") or thumbnails[0].get("url", "")
            if not thumb_url:
                thumb_url = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            # Proxyfie la miniature
            if "ytimg.com" in thumb_url or "youtube.com" in thumb_url:
                thumb_proxied = f"/thumbnail-proxy?url={urllib.parse.quote(thumb_url)}"
            else:
                thumb_proxied = thumb_url
            return {
                "title":     d.get("title", "YouTube Video"),
                "duration":  d.get("lengthSeconds", 0),
                "thumbnail": thumb_proxied,
                "uploader":  d.get("author", {}).get("title", "YouTube") if isinstance(d.get("author"), dict) else d.get("author", "YouTube"),
                "platform":  "youtube",
                "_rapidapi_data": d,   # gardé pour le download
            }
    except Exception as e:
        logger.error(f"YouTube RapidAPI info: {e}")
    return None


def _youtube_rapidapi_download(url: str, fmt: str, quality: str) -> tuple:
    """
    Télécharge une vidéo YouTube via RapidAPI.
    Retourne (path, title) ou (None, None).
    """
    key = _get_rapidapi_key()
    if not key:
        return None, None
    vid = _extract_yt_id(url)
    if not vid:
        return None, None
    try:
        r = httpx.get(
            "https://youtube-media-downloader.p.rapidapi.com/v2/video/details",
            params={"videoId": vid},
            headers={
                "X-RapidAPI-Key":  key,
                "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None, None
        d = r.json()
        if not d.get("status"):
            return None, None
        title = d.get("title", "video")

        if fmt == "mp3":
            # Audio uniquement
            audios = d.get("audios", [])
            dl_url = audios[0].get("url") if audios else None
            ext = ".mp3"
        else:
            # Vidéo MP4 — cherche la qualité demandée
            videos = d.get("videos", [])
            target_h = int(quality)
            # Trie par hauteur décroissante, prend la première <= target
            candidates = [
                v for v in videos
                if v.get("extension") == "mp4" and v.get("height", 0) <= target_h
            ]
            if not candidates:
                candidates = [v for v in videos if v.get("extension") == "mp4"]
            if not candidates:
                candidates = videos  # dernier recours
            # Prend la meilleure qualité disponible <= target
            best = max(candidates, key=lambda v: v.get("height", 0)) if candidates else None
            dl_url = best.get("url") if best else None
            ext = ".mp4"

        if dl_url:
            logger.info(f"YouTube RapidAPI download: {title} ({fmt} {quality}p)")
            path = _save_stream(dl_url, title, ext)
            return path, title
    except Exception as e:
        logger.error(f"YouTube RapidAPI download: {e}")
    return None, None


# ── THUMBNAIL PROXY ───────────────────────────────────────────────────────────
@app.get("/thumbnail-proxy")
async def thumbnail_proxy(url: str):
    """
    Proxy les miniatures YouTube pour contourner le blocage CORS côté frontend.
    Le frontend appelle /thumbnail-proxy?url=https://img.youtube.com/...
    et le backend fetch l'image et la retourne.
    """
    # Sécurité : on n'accepte que les domaines image YouTube/TikTok
    allowed_thumb_domains = [
        "img.youtube.com",
        "i.ytimg.com",
        "p16-sign.tiktokcdn.com",
        "p19-sign.tiktokcdn.com",
        "p16-sign-va.tiktokcdn.com",
        "p77-sign.tiktokcdn.com",
    ]
    try:
        parsed = urllib.parse.urlparse(url)
        if not any(d in parsed.netloc for d in allowed_thumb_domains):
            raise HTTPException(status_code=400, detail="Domaine non autorisé")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="URL invalide")

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            content_type = r.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([r.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except Exception as e:
        logger.error(f"thumbnail-proxy error: {e}")
        raise HTTPException(status_code=502, detail="Impossible de récupérer la miniature")


# ── INFO ──────────────────────────────────────────────────────────────────────
@app.get("/info", dependencies=SECURITY)
async def get_info(url: str):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Plateforme non supportee")

    # Nettoie les proxy env avant yt-dlp
    saved = _clean_proxy_env()
    try:
        opts = {
            "quiet":         True,
            "no_warnings":   True,
            "skip_download": True,
            "noplaylist":    True,
            "proxy":         "",  # Force aucun proxy
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # Miniature — on passe par notre proxy pour YouTube
            thumb = info.get("thumbnail", "")
            if not thumb and platform == "youtube":
                vid   = _extract_yt_id(url)
                thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else ""

            # Encapsule l'URL de miniature dans notre proxy
            if thumb and ("ytimg.com" in thumb or "youtube.com" in thumb):
                thumb = f"/thumbnail-proxy?url={urllib.parse.quote(thumb)}"

            return {
                "title":     info.get("title", "Video"),
                "duration":  info.get("duration", 0),
                "thumbnail": thumb,
                "uploader":  info.get("uploader", ""),
                "platform":  platform,
            }
        except Exception as e:
            logger.warning(f"yt-dlp info failed: {e}")
    finally:
        _restore_proxy_env(saved)

    # Fallback YouTube : essaie RapidAPI d'abord, puis miniature statique
    if platform == "youtube":
        rapi_info = _youtube_rapidapi_info(url)
        if rapi_info:
            # Retire la clé interne avant de retourner
            rapi_info.pop("_rapidapi_data", None)
            return rapi_info
        # Dernier recours : miniature statique uniquement
        vid = _extract_yt_id(url)
        raw_thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else ""
        thumb_proxied = f"/thumbnail-proxy?url={urllib.parse.quote(raw_thumb)}" if raw_thumb else ""
        return {
            "title":     "YouTube Video",
            "duration":  0,
            "thumbnail": thumb_proxied,
            "uploader":  "YouTube",
            "platform":  platform,
        }

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")


# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
@app.get("/download", dependencies=SECURITY)
async def download(url: str, format: str = "mp4", quality: str = "720"):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if format  not in ("mp4", "mp3"):              format  = "mp4"
    if quality not in ("360", "480", "720", "1080"): quality = "720"

    uid  = uuid.uuid4().hex[:8]
    base = _ydl_base(uid)

    # Construction du format yt-dlp
    if format == "mp3":
        if FFMPEG_OK:
            opts = {
                **base,
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "192",
                }],
            }
        else:
            opts = {**base, "format": "bestaudio[ext=m4a]/bestaudio"}
    else:
        # MP4 vidéo
        if FFMPEG_OK:
            # bestvideo + bestaudio → merge par ffmpeg (vraie vidéo)
            fmt_map = {
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[ext=mp4]/best",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[ext=mp4]/best",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[ext=mp4]/best",
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[ext=mp4]/best",
            }
            opts = {**base, "format": fmt_map[quality], "merge_output_format": "mp4"}
        else:
            # Sans ffmpeg : format progressif (vidéo+audio dans 1 fichier, max 720p)
            fmt_map = {
                "1080": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "720":  "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "480":  "best[height<=480][ext=mp4]/best[ext=mp4]/best",
                "360":  "best[height<=360][ext=mp4]/best[ext=mp4]/best",
            }
            opts = {**base, "format": fmt_map[quality]}

    logger.info(f"DL | platform={platform} fmt={format} q={quality} ffmpeg={FFMPEG_OK} uid={uid}")

    # Nettoie les proxy env avant yt-dlp
    saved = _clean_proxy_env()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        logger.error(f"yt-dlp download failed: {e}")
        info = None
    finally:
        _restore_proxy_env(saved)

    if info:
        path = _find_file(uid)
        if path:
            return _serve(path, info.get("title", "video"))
        logger.warning(f"yt-dlp succeeded but file not found uid={uid}")

    # Fallback RapidAPI selon la plateforme
    if platform == "youtube":
        path, title = _youtube_rapidapi_download(url, format, quality)
        if path and os.path.exists(path):
            return _serve(path, title)

    if platform == "tiktok":
        path, title = _tiktok_rapidapi(url, format)
        if path and os.path.exists(path):
            return _serve(path, title)

    raise HTTPException(status_code=500, detail="Telechargement impossible")


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status":        "ok",
        "rapidapi_keys": len(RAPIDAPI_KEYS),
        "ffmpeg":        FFMPEG_OK,
        "ffmpeg_path":   FFMPEG_PATH,
    }


# ── CHAT ──────────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    message:    str
    user_email: str = "Anonyme"

@app.post("/chat")
async def send_chat(body: ChatMessage):
    tok = os.environ.get("SUPPORT_BOT_TOKEN", "")
    cid = os.environ.get("SUPPORT_CHAT_ID", "")
    if not tok or not cid:
        return {"error": "config_missing"}
    text = f"Message Support AlphaConvert\n\nDe: {body.user_email}\nMessage: {body.message}"
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r   = await c.post(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                json={"chat_id": cid, "text": text},
            )
            res = r.json()
            return {"success": True} if res.get("ok") else {"error": res.get("description")}
        except Exception as e:
            return {"error": str(e)}


# ── STATIC (en dernier) ───────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=".", html=True), name="static")
