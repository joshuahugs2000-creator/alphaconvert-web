"""
api.py — Backend FastAPI AlphaConvert
YouTube MP4 : 3 APIs en cascade (ytstream → youtube-media-downloader → yt-dlp+ffmpeg)
TikTok : RapidAPI tiktok-scraper7 (inchangé, marche déjà)
"""
import os, re, logging, unicodedata, httpx, urllib.parse, time, glob, uuid, shutil
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"])

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

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
    ua = request.headers.get("user-agent","").lower()
    for p in ["sqlmap","nikto","nmap","masscan","scrapy","dirbuster"]:
        if p in ua: raise HTTPException(status_code=403, detail="Acces refuse.")

ALLOWED_DOMAINS = ["youtube.com","www.youtube.com","youtu.be","m.youtube.com",
                   "tiktok.com","www.tiktok.com","vm.tiktok.com","vt.tiktok.com"]
DANGEROUS = ["javascript:","data:","file://","../","..\\","127.0.0.1","0.0.0.0","169.254.","192.168.","10.0."]

def validate_url(url: str) -> str:
    if not url or len(url) > 500: raise HTTPException(status_code=400, detail="URL invalide.")
    url = url.strip()
    if not url.startswith(("https://","http://")): raise HTTPException(status_code=400, detail="URL invalide.")
    for d in DANGEROUS:
        if d in url.lower(): raise HTTPException(status_code=400, detail="URL non autorisee.")
    try:
        parsed = urllib.parse.urlparse(url)
        if not any(domain in parsed.netloc.lower() for domain in ALLOWED_DOMAINS):
            raise HTTPException(status_code=400, detail="YouTube et TikTok uniquement.")
    except HTTPException: raise
    except Exception: raise HTTPException(status_code=400, detail="URL malformee.")
    return url

SECURITY = [Depends(rate_limit), Depends(check_ua)]

DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

_raw_keys     = os.environ.get("RAPIDAPI_KEYS", os.environ.get("RAPIDAPI_KEY",""))
RAPIDAPI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_rapi_idx     = 0

def _get_rapidapi_key():
    global _rapi_idx
    if not RAPIDAPI_KEYS: return None
    key = RAPIDAPI_KEYS[_rapi_idx % len(RAPIDAPI_KEYS)]
    _rapi_idx += 1
    return key

PROXY_URL = os.environ.get("PROXY_URL","")

FFMPEG_PATH = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
FFMPEG_OK   = os.path.isfile(FFMPEG_PATH)
logger.info(f"ffmpeg={FFMPEG_PATH} exists={FFMPEG_OK}")

def clean_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url.strip())
        params = urllib.parse.parse_qs(parsed.query)
        if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
            cp = {k:v for k,v in params.items() if k=="v"}
            return parsed._replace(query=urllib.parse.urlencode(cp,doseq=True)).geturl()
        if "tiktok.com" in parsed.netloc or "vm.tiktok" in parsed.netloc:
            return parsed._replace(query="",fragment="").geturl()
    except Exception: pass
    return url.strip()

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "tiktok.com" in u or "vm.tiktok" in u: return "tiktok"
    return "unknown"

def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD",name).encode("ascii","ignore").decode("ascii")
    return re.sub(r"[^\w\s\-.]","_",name).strip()[:60] or "video"

def _extract_yt_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",url)
    return m.group(1) if m else url

def _find_file(uid: str):
    files = [f for f in glob.glob(os.path.join(DOWNLOAD_PATH,f"{uid}*"))
             if os.path.isfile(f) and os.path.getsize(f) > 1024]
    return max(files, key=os.path.getsize) if files else None

def _serve(path: str, title: str) -> FileResponse:
    ext     = os.path.splitext(path)[1]
    dl_name = f"{safe_filename(title)}{ext}"
    logger.info(f"Serving {dl_name} ({os.path.getsize(path):,} bytes)")
    return FileResponse(path, media_type="application/octet-stream", filename=dl_name,
                        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})

def _save_stream(dl_url: str, title: str, ext: str, referer: str = "") -> str:
    path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex[:8]}{ext}")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    if referer:
        headers["Referer"] = referer
    with httpx.stream("GET", dl_url, timeout=180, follow_redirects=True, headers=headers) as r:
        r.raise_for_status()
        with open(path,"wb") as f:
            for chunk in r.iter_bytes(65536): f.write(chunk)
    size = os.path.getsize(path)
    if size < 1024:
        os.remove(path)
        raise ValueError(f"Fichier trop petit: {size} bytes")
    return path

# ══════════════════════════════════════════════════════════════════════════════
# MÉTHODE 1 : YTStream RapidAPI  (ytstream-download-youtube-videos)
# Souscrire sur : https://rapidapi.com/ytjar/api/ytstream-download-youtube-videos
# Plan BASIC gratuit : 500 req/mois
# ══════════════════════════════════════════════════════════════════════════════
def _ytstream_download(url: str, fmt: str, quality: str):
    """Retourne (path, title) ou lève une exception."""
    key = _get_rapidapi_key()
    if not key:
        raise ValueError("Pas de clé RapidAPI")

    vid = _extract_yt_id(url)
    q_map = {"1080": "1080", "720": "720", "480": "480", "360": "360"}
    res = q_map.get(quality, "720")

    if fmt == "mp3":
        endpoint = "https://ytstream-download-youtube-videos.p.rapidapi.com/dl"
        params   = {"id": vid}
    else:
        endpoint = "https://ytstream-download-youtube-videos.p.rapidapi.com/dl"
        params   = {"id": vid}

    headers = {
        "X-RapidAPI-Key":  key,
        "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com"
    }
    r = httpx.get(endpoint, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    data  = r.json()
    title = data.get("title", "video")

    if fmt == "mp3":
        dl_url = data.get("a") or data.get("adaptiveFormats", [{}])[0].get("url")
        ext    = ".mp3"
    else:
        # formats is a dict: {"720": url, "360": url, ...}
        formats = data.get("formats") or {}
        dl_url  = (formats.get(res)
                   or formats.get("720")
                   or formats.get("480")
                   or formats.get("360"))
        if not dl_url:
            # adaptiveFormats fallback
            af = data.get("adaptiveFormats", [])
            vids = [f for f in af if f.get("mimeType","").startswith("video/mp4")]
            if vids:
                dl_url = vids[0].get("url")
        ext = ".mp4"

    if not dl_url:
        raise ValueError(f"YTStream: pas d'URL de téléchargement. data={list(data.keys())}")

    logger.info(f"YTStream OK title={title} ext={ext}")
    path = _save_stream(dl_url, title, ext)
    return path, title

# ══════════════════════════════════════════════════════════════════════════════
# MÉTHODE 2 : YouTube Media Downloader RapidAPI
# Souscrire sur : https://rapidapi.com/DataFanatic/api/youtube-media-downloader
# Plan BASIC gratuit
# ══════════════════════════════════════════════════════════════════════════════
def _ytmedia_download(url: str, fmt: str, quality: str):
    key = _get_rapidapi_key()
    if not key:
        raise ValueError("Pas de clé RapidAPI")

    vid = _extract_yt_id(url)
    headers = {
        "X-RapidAPI-Key":  key,
        "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"
    }

    # Étape 1 : récupérer les infos
    r = httpx.get("https://youtube-media-downloader.p.rapidapi.com/v2/video/details",
                  params={"videoId": vid}, headers=headers, timeout=30)
    r.raise_for_status()
    data  = r.json()
    title = data.get("title", "video")

    if fmt == "mp3":
        audios = data.get("audios", [])
        if not audios:
            raise ValueError("YTMedia: pas d'audio")
        dl_url = audios[0].get("url")
        ext    = ".mp3"
    else:
        videos = data.get("videos", [])
        q_int  = int(quality)
        # Trier par résolution décroissante, prendre la meilleure ≤ qualité demandée
        mp4s = [v for v in videos if "mp4" in v.get("extension","").lower()
                and v.get("width") and v.get("height")]
        mp4s.sort(key=lambda v: v.get("height", 0), reverse=True)
        chosen = next((v for v in mp4s if v.get("height",0) <= q_int), None) or (mp4s[0] if mp4s else None)
        if not chosen:
            raise ValueError("YTMedia: pas de vidéo MP4")
        dl_url = chosen.get("url")
        ext    = ".mp4"

    if not dl_url:
        raise ValueError("YTMedia: URL vide")

    logger.info(f"YTMedia OK title={title} ext={ext}")
    path = _save_stream(dl_url, title, ext)
    return path, title

# ══════════════════════════════════════════════════════════════════════════════
# MÉTHODE 3 : yt-dlp local (avec ffmpeg si dispo)
# ══════════════════════════════════════════════════════════════════════════════
def _ytdlp_download(url: str, fmt: str, quality: str):
    uid  = uuid.uuid4().hex[:8]
    opts = {
        "outtmpl":        os.path.join(DOWNLOAD_PATH, f"{uid}.%(ext)s"),
        "quiet":          True,
        "no_warnings":    True,
        "noplaylist":     True,
        "restrictfilenames": False,
    }
    if FFMPEG_OK:
        opts["ffmpeg_location"] = FFMPEG_PATH
    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    if fmt == "mp3":
        if FFMPEG_OK:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key":"FFmpegExtractAudio",
                                        "preferredcodec":"mp3","preferredquality":"192"}]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio"
    else:
        fmts_ff = {
            "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
            "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
            "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
            "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best",
        }
        fmts_noff = {
            "1080": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
            "720":  "best[height<=720][ext=mp4]/best[ext=mp4]/best",
            "480":  "best[height<=480][ext=mp4]/best[ext=mp4]/best",
            "360":  "best[height<=360][ext=mp4]/best[ext=mp4]/best",
        }
        opts["format"] = fmts_ff[quality] if FFMPEG_OK else fmts_noff[quality]
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    path = _find_file(uid)
    if not path:
        raise ValueError("yt-dlp: fichier introuvable après téléchargement")
    return path, info.get("title","video")

# ══════════════════════════════════════════════════════════════════════════════
# TikTok
# ══════════════════════════════════════════════════════════════════════════════
def _tiktok_download(url: str, fmt: str):
    key = _get_rapidapi_key()
    if not key:
        raise ValueError("Pas de clé RapidAPI")
    r = httpx.get("https://tiktok-scraper7.p.rapidapi.com/video/info",
        params={"url": url, "hd": "1"},
        headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"},
        timeout=30)
    r.raise_for_status()
    d     = r.json().get("data", {})
    title = d.get("title","tiktok")
    if fmt == "mp3":
        dl_url = (d.get("music_info") or {}).get("play") or d.get("wmplay") or d.get("play")
        ext    = ".mp3"
    else:
        dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
        ext    = ".mp4"
    if not dl_url:
        raise ValueError("TikTok: pas d'URL")
    path = _save_stream(dl_url, title, ext, referer="https://www.tiktok.com/")
    return path, title

# ══════════════════════════════════════════════════════════════════════════════
# INFO helpers
# ══════════════════════════════════════════════════════════════════════════════
def _ytstream_info(url: str) -> dict:
    """Récupère titre + durée via YTStream RapidAPI."""
    key = _get_rapidapi_key()
    if not key: raise ValueError("Pas de clé")
    vid = _extract_yt_id(url)
    r = httpx.get("https://ytstream-download-youtube-videos.p.rapidapi.com/dl",
                  params={"id": vid},
                  headers={"X-RapidAPI-Key": key,
                           "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com"},
                  timeout=20)
    r.raise_for_status()
    d = r.json()
    return {
        "title":    d.get("title", "Video"),
        "duration": d.get("lengthSeconds", 0),
        "thumbnail": (d.get("thumbnail") or {}).get("thumbnails", [{}])[-1].get("url","")
                     or f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
        "uploader": d.get("author", "YouTube"),
        "platform": "youtube"
    }

def _ytmedia_info(url: str) -> dict:
    """Récupère titre + durée via YouTube Media Downloader RapidAPI."""
    key = _get_rapidapi_key()
    if not key: raise ValueError("Pas de clé")
    vid = _extract_yt_id(url)
    r = httpx.get("https://youtube-media-downloader.p.rapidapi.com/v2/video/details",
                  params={"videoId": vid},
                  headers={"X-RapidAPI-Key": key,
                           "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"},
                  timeout=20)
    r.raise_for_status()
    d = r.json()
    thumb = ""
    thumbs = d.get("thumbnails", [])
    if thumbs: thumb = thumbs[-1].get("url","")
    if not thumb: thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
    return {
        "title":    d.get("title", "Video"),
        "duration": d.get("lengthSeconds", 0),
        "thumbnail": thumb,
        "uploader": d.get("author", "YouTube"),
        "platform": "youtube"
    }

# ══════════════════════════════════════════════════════════════════════════════
# INFO endpoint
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/info", dependencies=SECURITY)
async def get_info(url: str):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Plateforme non supportee")

    # ── YouTube : essai yt-dlp, puis YTStream, puis YTMedia ──────────────────
    if platform == "youtube":
        # 1) yt-dlp (rapide si Railway ne bloque pas)
        opts = {"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True}
        if PROXY_URL: opts["proxy"] = PROXY_URL
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            thumb = info.get("thumbnail","")
            if not thumb:
                thumb = f"https://img.youtube.com/vi/{_extract_yt_id(url)}/hqdefault.jpg"
            title = info.get("title","")
            if title and title.lower() not in ("youtube","video",""):
                logger.info(f"yt-dlp info OK: {title}")
                return {"title":title,"duration":info.get("duration",0),
                        "thumbnail":thumb,"uploader":info.get("uploader","YouTube"),
                        "platform":platform}
        except Exception as e:
            logger.warning(f"yt-dlp info failed: {e}")

        # 2) YTStream RapidAPI
        try:
            result = _ytstream_info(url)
            logger.info(f"YTStream info OK: {result['title']}")
            return result
        except Exception as e:
            logger.warning(f"YTStream info failed: {e}")

        # 3) YouTube Media Downloader
        try:
            result = _ytmedia_info(url)
            logger.info(f"YTMedia info OK: {result['title']}")
            return result
        except Exception as e:
            logger.warning(f"YTMedia info failed: {e}")

        # 4) Fallback minimal — au moins la miniature
        vid = _extract_yt_id(url)
        return {"title":"Vidéo YouTube","duration":0,
                "thumbnail":f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                "uploader":"YouTube","platform":platform}

    # ── TikTok ────────────────────────────────────────────────────────────────
    opts = {"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True}
    if PROXY_URL: opts["proxy"] = PROXY_URL
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumb = info.get("thumbnail","")
        if not thumb:
            thumbs = info.get("thumbnails",[])
            if thumbs: thumb = thumbs[-1].get("url","")
        return {"title":info.get("title","TikTok"),"duration":info.get("duration",0),
                "thumbnail":thumb,"uploader":info.get("uploader",""),"platform":platform}
    except Exception as e:
        logger.warning(f"yt-dlp TikTok info failed: {e}")

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")


# ══════════════════════════════════════════════════════════════════════════════
# DEBUG endpoint (à supprimer après que tout marche)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/debug-yt")
async def debug_yt(url: str = "https://youtu.be/dQw4w9WgXcQ"):
    url = clean_url(validate_url(url))
    vid = _extract_yt_id(url)
    key = _get_rapidapi_key()
    results = {"vid": vid, "has_key": bool(key), "ffmpeg": FFMPEG_OK}

    # Test YTStream
    try:
        r = httpx.get("https://ytstream-download-youtube-videos.p.rapidapi.com/dl",
                      params={"id": vid},
                      headers={"X-RapidAPI-Key": key or "",
                               "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com"},
                      timeout=15)
        results["ytstream_status"] = r.status_code
        if r.status_code == 200:
            d = r.json()
            results["ytstream_title"] = d.get("title","?")
            results["ytstream_formats"] = list((d.get("formats") or {}).keys())
    except Exception as e:
        results["ytstream_error"] = str(e)

    # Test YTMedia
    try:
        r2 = httpx.get("https://youtube-media-downloader.p.rapidapi.com/v2/video/details",
                       params={"videoId": vid},
                       headers={"X-RapidAPI-Key": key or "",
                                "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"},
                       timeout=15)
        results["ytmedia_status"] = r2.status_code
        if r2.status_code == 200:
            d2 = r2.json()
            results["ytmedia_title"] = d2.get("title","?")
            results["ytmedia_video_count"] = len(d2.get("videos",[]))
    except Exception as e:
        results["ytmedia_error"] = str(e)

    return results

# ══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD endpoint
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/download", dependencies=SECURITY)
async def download(url: str, format: str = "mp4", quality: str = "720"):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if format  not in ("mp4","mp3"): format  = "mp4"
    if quality not in ("360","480","720","1080"): quality = "720"

    logger.info(f"DL start | platform={platform} format={format} quality={quality} ffmpeg={FFMPEG_OK}")

    # ── TikTok ────────────────────────────────────────────────────────────────
    if platform == "tiktok":
        try:
            path, title = _tiktok_download(url, format)
            return _serve(path, title)
        except Exception as e:
            logger.error(f"TikTok failed: {e}")
            raise HTTPException(status_code=500, detail="Telechargement TikTok impossible")

    # ── YouTube : cascade de 3 méthodes ──────────────────────────────────────
    errors = []

    # Méthode 1 : YTStream RapidAPI
    try:
        logger.info("YouTube: essai méthode 1 (YTStream)")
        path, title = _ytstream_download(url, format, quality)
        return _serve(path, title)
    except Exception as e:
        errors.append(f"YTStream: {e}")
        logger.warning(f"YTStream échoué: {e}")

    # Méthode 2 : YouTube Media Downloader RapidAPI
    try:
        logger.info("YouTube: essai méthode 2 (YTMedia)")
        path, title = _ytmedia_download(url, format, quality)
        return _serve(path, title)
    except Exception as e:
        errors.append(f"YTMedia: {e}")
        logger.warning(f"YTMedia échoué: {e}")

    # Méthode 3 : yt-dlp local
    try:
        logger.info("YouTube: essai méthode 3 (yt-dlp)")
        path, title = _ytdlp_download(url, format, quality)
        return _serve(path, title)
    except Exception as e:
        errors.append(f"yt-dlp: {e}")
        logger.warning(f"yt-dlp échoué: {e}")

    logger.error(f"Toutes les méthodes ont échoué: {errors}")
    raise HTTPException(status_code=500, detail="Telechargement impossible")

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {"status":"ok","rapidapi_keys":len(RAPIDAPI_KEYS),
            "ffmpeg":FFMPEG_OK,"ffmpeg_path":FFMPEG_PATH}

# ══════════════════════════════════════════════════════════════════════════════
# CHAT (support Telegram)
# ══════════════════════════════════════════════════════════════════════════════
class ChatMessage(BaseModel):
    message: str
    user_email: str = "Anonyme"

@app.post("/chat")
async def send_chat(body: ChatMessage):
    tok = os.environ.get("SUPPORT_BOT_TOKEN","")
    cid = os.environ.get("SUPPORT_CHAT_ID","")
    if not tok or not cid: return {"error":"config_missing"}
    text = f"Message Support AlphaConvert\n\nDe: {body.user_email}\nMessage: {body.message}"
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                             json={"chat_id":cid,"text":text})
            res = r.json()
            return {"success":True} if res.get("ok") else {"error":res.get("description")}
        except Exception as e:
            return {"error":str(e)}

# ══════════════════════════════════════════════════════════════════════════════
# STATIC (doit rester en dernier)
# ══════════════════════════════════════════════════════════════════════════════
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=".", html=True), name="static")
