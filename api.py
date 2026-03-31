"""
api.py — Backend FastAPI AlphaConvert
— Sécurité légère : rate limiting, validation URL, headers, anti-abus
— CORS ouvert pour ne pas bloquer le fonctionnement normal
"""
import os, re, logging, unicodedata, httpx, urllib.parse, time
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API", docs_url=None, redoc_url=None)

# ── CORS ouvert (nécessaire pour le bot + frontend) ───────────────────────────
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

# ── Security Headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]         = "SAMEORIGIN"
    response.headers["X-XSS-Protection"]        = "1; mode=block"
    response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
    return response

# ── Rate Limiting ─────────────────────────────────────────────────────────────
_rate_store: dict = defaultdict(list)
_ban_store:  dict = {}

RATE_LIMIT   = 15   # requêtes max par minute (assez large pour usage normal)
RATE_WINDOW  = 60
BAN_AT       = 40   # ban si vraiment abusif
BAN_DURATION = 300  # 5 minutes

def _get_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else request.client.host

async def rate_limit(request: Request):
    ip  = _get_ip(request)
    now = time.time()

    # IP bannie ?
    if ip in _ban_store:
        if now < _ban_store[ip]:
            raise HTTPException(status_code=429, detail=f"Trop de requêtes. Réessaie dans {int(_ban_store[ip]-now)}s.")
        del _ban_store[ip]

    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]

    if len(_rate_store[ip]) >= BAN_AT:
        _ban_store[ip] = now + BAN_DURATION
        logger.warning(f"IP bannie (abus): {ip}")
        raise HTTPException(status_code=429, detail="Abus détecté. Banni 5 minutes.")

    if len(_rate_store[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Limite atteinte. Patiente un peu.")

    _rate_store[ip].append(now)

# ── Blocage outils de hacking ─────────────────────────────────────────────────
BAD_UA = ["sqlmap", "nikto", "nmap", "masscan", "zgrab", "scrapy", "dirbuster", "hydra"]

async def check_ua(request: Request):
    ua = request.headers.get("user-agent", "").lower()
    for p in BAD_UA:
        if p in ua:
            logger.warning(f"UA malveillant bloqué: {ua} | IP: {_get_ip(request)}")
            raise HTTPException(status_code=403, detail="Accès refusé.")

# ── Validation URL (bloque les injections, garde YouTube + TikTok) ────────────
ALLOWED_DOMAINS = [
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
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
            raise HTTPException(status_code=400, detail="URL non autorisée.")
    try:
        parsed = urllib.parse.urlparse(url)
        if not any(domain in parsed.netloc.lower() for domain in ALLOWED_DOMAINS):
            raise HTTPException(status_code=400, detail="YouTube et TikTok uniquement.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="URL malformée.")
    return url

SECURITY = [Depends(rate_limit), Depends(check_ua)]

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

_raw_keys     = os.environ.get("RAPIDAPI_KEYS", os.environ.get("RAPIDAPI_KEY", ""))
RAPIDAPI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_rapi_idx     = 0

def _get_rapidapi_key():
    global _rapi_idx
    if not RAPIDAPI_KEYS: return None
    key = RAPIDAPI_KEYS[_rapi_idx % len(RAPIDAPI_KEYS)]
    _rapi_idx += 1
    return key

_raw_proxies = os.environ.get("PROXY_URLS", os.environ.get("PROXY_URL", ""))
PROXY_LIST   = [p.strip() for p in _raw_proxies.split(",") if p.strip()]
logger.info(f"Proxies: {len(PROXY_LIST)} | RapidAPI keys: {len(RAPIDAPI_KEYS)}")

# ── Helpers ───────────────────────────────────────────────────────────────────
def clean_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url.strip())
        params = urllib.parse.parse_qs(parsed.query)
        if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
            clean_params = {k: v for k, v in params.items() if k == "v"}
            new_query = urllib.parse.urlencode(clean_params, doseq=True)
            return parsed._replace(query=new_query).geturl()
        if "tiktok.com" in parsed.netloc or "vm.tiktok" in parsed.netloc:
            return parsed._replace(query="", fragment="").geturl()
    except Exception:
        pass
    return url.strip()

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u: return "tiktok"
    return "unknown"

def safe_filename(name: str) -> str:
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^\w\s\-.]', '_', name).strip() or "video"

def _extract_yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else url

def _save_stream(dl_url: str, title: str, ext: str) -> str:
    safe = re.sub(r'[^\w\-]', '_', title)[:60]
    path = os.path.join(DOWNLOAD_PATH, f"{safe}{ext}")
    with httpx.stream("GET", dl_url, timeout=120, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(8192): f.write(chunk)
    return path

# ── RapidAPI ──────────────────────────────────────────────────────────────────
def _rapi_download(url: str, platform: str, format_type: str):
    key = _get_rapidapi_key()
    if not key: return None, "media", False
    try:
        if platform == "youtube" and format_type == "mp3":
            r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl",
                params={"id": _extract_yt_id(url)},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                if d.get("link"):
                    return _save_stream(d["link"], d.get("title", "audio"), ".mp3"), d.get("title", "audio"), False
        elif platform == "youtube":
            r = httpx.get("https://yt-api.p.rapidapi.com/dl",
                params={"id": _extract_yt_id(url), "cgeo": "US"},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "yt-api.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                formats = d.get("formats", []) + d.get("adaptiveFormats", [])
                mp4s = [f for f in formats if f.get("mimeType", "").startswith("video/mp4") and f.get("url")]
                if mp4s:
                    best = sorted(mp4s, key=lambda x: x.get("height", 0), reverse=True)[0]
                    return best["url"], d.get("title", "video"), True
            r2 = httpx.get("https://youtube-mp36.p.rapidapi.com/dl",
                params={"id": _extract_yt_id(url), "format": "mp4"},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=30)
            if r2.status_code == 200:
                d2 = r2.json()
                dl_url = d2.get("link") or d2.get("url")
                if dl_url:
                    return dl_url, d2.get("title", "video"), True
        elif platform == "tiktok":
            r = httpx.get("https://tiktok-scraper7.p.rapidapi.com/video/info",
                params={"url": url, "hd": "1"},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json().get("data", {})
                dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                if dl_url:
                    ext = ".mp3" if format_type == "mp3" else ".mp4"
                    return _save_stream(dl_url, d.get("title", "tiktok"), ext), d.get("title", "tiktok"), False
    except Exception as e:
        logger.error(f"RapidAPI [{platform}]: {e}")
    return None, "media", False

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/info", dependencies=SECURITY)
async def get_info(url: str):
    url = validate_url(url)
    url = clean_url(url)
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Plateforme non supportée")

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumb = info.get("thumbnail") or info.get("thumbnails", [{}])[-1].get("url", "") if info.get("thumbnails") else info.get("thumbnail")
        # Pour YouTube, garantit toujours une miniature
        if platform == "youtube" and not thumb:
            vid = _extract_yt_id(url)
            thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        return {"title": info.get("title", "Vidéo"), "duration": info.get("duration", 0),
                "thumbnail": thumb, "uploader": info.get("uploader", ""), "platform": platform}
    except Exception:
        logger.warning(f"yt-dlp info [{platform}] failed → RapidAPI")

    key = _get_rapidapi_key()
    if key:
        try:
            if platform == "tiktok":
                r = httpx.get("https://tiktok-scraper7.p.rapidapi.com/video/info",
                    params={"url": url, "hd": "1"},
                    headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}, timeout=15)
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    thumb = d.get("cover") or d.get("origin_cover") or d.get("dynamic_cover") or ""
                    return {"title": d.get("title", "TikTok"), "duration": d.get("duration", 0),
                            "thumbnail": thumb, "uploader": d.get("author", {}).get("nickname", ""), "platform": platform}
            elif platform == "youtube":
                vid = _extract_yt_id(url)
                r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl",
                    params={"id": vid},
                    headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    return {"title": d.get("title", "YouTube"), "duration": int(d.get("duration", 0) or 0),
                            "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                            "uploader": "YouTube", "platform": platform}
        except Exception as e2:
            logger.error(f"RapidAPI info [{platform}]: {e2}")

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")


@app.get("/download", dependencies=SECURITY)
async def download(url: str, format: str = "mp4", quality: str = "720"):
    url = validate_url(url)
    url = clean_url(url)
    platform = detect_platform(url)

    if format not in ("mp4", "mp3"): format = "mp4"
    if quality not in ("360", "480", "720", "1080"): quality = "720"

    import glob as _glob, uuid as _uuid
    uid  = _uuid.uuid4().hex[:8]
    tpl  = os.path.join(DOWNLOAD_PATH, f"{uid}.%(ext)s")

    base_opts = {
        "outtmpl":           tpl,
        "quiet":             True,
        "no_warnings":       True,
        "restrictfilenames": False,
        "noplaylist":        True,
    }

    if format == "mp3":
        # MP3 : pas de ffmpeg nécessaire, on prend m4a directement
        opts = {**base_opts, "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"}
    else:
        # MP4 : UNIQUEMENT des formats qui ont vidéo+audio intégrés (pas de merge ffmpeg)
        # YouTube propose des formats "progressive" (vidéo+audio) jusqu'à 720p max
        qmap = {
            "1080": "best[height<=1080][ext=mp4][vcodec!=none][acodec!=none]/best[height<=1080][ext=mp4]/best[height<=720][ext=mp4]",
            "720":  "best[height<=720][ext=mp4][vcodec!=none][acodec!=none]/best[height<=720][ext=mp4]/best[height<=480][ext=mp4]",
            "480":  "best[height<=480][ext=mp4][vcodec!=none][acodec!=none]/best[height<=480][ext=mp4]/best[height<=360][ext=mp4]",
            "360":  "best[height<=360][ext=mp4][vcodec!=none][acodec!=none]/best[height<=360][ext=mp4]/best[ext=mp4]",
        }
        opts = {**base_opts, "format": qmap[quality]}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # Trouve le fichier réellement créé
        found = [f for f in _glob.glob(os.path.join(DOWNLOAD_PATH, f"{uid}*"))
                 if os.path.isfile(f) and os.path.getsize(f) > 1024]
        if found:
            file_path = max(found, key=os.path.getsize)
            ext       = os.path.splitext(file_path)[1]
            title     = safe_filename(info.get("title", "video"))
            dl_name   = f"{title}{ext}"
            logger.info(f"yt-dlp OK → {dl_name} ({os.path.getsize(file_path)} bytes)")
            return FileResponse(file_path, media_type="application/octet-stream", filename=dl_name,
                                headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})
        logger.warning(f"yt-dlp: aucun fichier trouvé (uid={uid})")
    except Exception as e:
        logger.warning(f"yt-dlp download [{platform}] failed: {e} → RapidAPI")

    file_path, title, is_redirect = _rapi_download(url, platform, format)
    if file_path:
        if is_redirect: return RedirectResponse(url=file_path)
        if os.path.exists(file_path):
            ext     = os.path.splitext(file_path)[1]
            dl_name = f"{safe_filename(title)}{ext}"
            return FileResponse(file_path, media_type="application/octet-stream", filename=dl_name,
                                headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})

    raise HTTPException(status_code=400, detail="Téléchargement impossible")


@app.get("/health")
async def health():
    return {"status": "ok", "rapidapi_keys": len(RAPIDAPI_KEYS)}
