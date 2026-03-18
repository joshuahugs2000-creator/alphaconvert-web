"""
api.py — Backend FastAPI AlphaConvert
— yt-dlp prioritaire + RapidAPI fallback YouTube, TikTok, Instagram
"""
import os, re, logging, unicodedata, base64, random, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AlphaConvert API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

DOWNLOAD_PATH = "/tmp/alphaconvert"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# ── Proxies ───────────────────────────────────────────────────────────────────
_raw_proxies = os.environ.get("PROXY_URLS", os.environ.get("PROXY_URL", ""))
PROXY_LIST = [p.strip() for p in _raw_proxies.split(",") if p.strip()]

def _get_proxy():
    return random.choice(PROXY_LIST) if PROXY_LIST else None

# ── RapidAPI ──────────────────────────────────────────────────────────────────
_raw_keys = os.environ.get("RAPIDAPI_KEYS", os.environ.get("RAPIDAPI_KEY", ""))
RAPIDAPI_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
_rapi_idx = 0

def _get_rapidapi_key():
    global _rapi_idx
    if not RAPIDAPI_KEYS: return None
    key = RAPIDAPI_KEYS[_rapi_idx % len(RAPIDAPI_KEYS)]
    _rapi_idx += 1
    return key

logger.info(f"Proxies: {len(PROXY_LIST)} | RapidAPI keys: {len(RAPIDAPI_KEYS)}")

# ── Cookies ───────────────────────────────────────────────────────────────────
def _write_cookie(env_var, filename):
    val = os.environ.get(env_var, "")
    if not val: return None
    try:
        path = f"/tmp/{filename}"
        with open(path, "wb") as f:
            f.write(base64.b64decode(val))
        return path
    except: return None

COOKIE_INSTAGRAM = _write_cookie("COOKIES_INSTAGRAM", "ig_cookies.txt")

# ── Helpers ───────────────────────────────────────────────────────────────────
def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "instagram.com" in u: return "instagram"
    if "tiktok.com" in u: return "tiktok"
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
    with httpx.stream("GET", dl_url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(8192):
                f.write(chunk)
    return path

def _apply_ig_opts(opts: dict) -> dict:
    opts["impersonate"] = ImpersonateTarget("chrome", "131")
    proxy = _get_proxy()
    if proxy: opts["proxy"] = proxy
    if COOKIE_INSTAGRAM: opts["cookiefile"] = COOKIE_INSTAGRAM
    return opts

# ── RapidAPI download ─────────────────────────────────────────────────────────
def _rapi_download(url: str, platform: str, format_type: str) -> tuple:
    key = _get_rapidapi_key()
    if not key: return None, "media"
    try:
        if platform == "youtube" and format_type == "mp3":
            r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl",
                params={"id": _extract_yt_id(url)},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                if d.get("link"):
                    return _save_stream(d["link"], d.get("title", "audio"), ".mp3"), d.get("title", "audio")

        elif platform == "youtube":
            r = httpx.get("https://yt-api.p.rapidapi.com/dl",
                params={"id": _extract_yt_id(url), "cgeo": "US"},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "yt-api.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                formats = d.get("adaptiveFormats", []) + d.get("formats", [])
                mp4s = [f for f in formats if f.get("mimeType", "").startswith("video/mp4")]
                if mp4s:
                    best = sorted(mp4s, key=lambda x: x.get("height", 0), reverse=True)[0]
                    if best.get("url"):
                        return _save_stream(best["url"], d.get("title", "video"), ".mp4"), d.get("title", "video")

        elif platform == "tiktok":
            r = httpx.get("https://tiktok-scraper7.p.rapidapi.com/video/info",
                params={"url": url, "hd": "1"},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"}, timeout=30)
            if r.status_code == 200:
                d = r.json().get("data", {})
                title = d.get("title", "tiktok")
                dl_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                if dl_url:
                    ext = ".mp3" if format_type == "mp3" else ".mp4"
                    return _save_stream(dl_url, title, ext), title

        elif platform == "instagram":
            r = httpx.get("https://instagram120.p.rapidapi.com/api/instagram/hls",
                params={"url": url},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "instagram120.p.rapidapi.com"}, timeout=30)
            logger.info(f"Instagram120 hls: {r.status_code} | {r.text[:300]}")
            if r.status_code == 200:
                d = r.json()
                def _find_video(obj):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k in ("video_url", "url", "src", "link") and isinstance(v, str) and "http" in v:
                                return v
                            if k == "video_versions" and isinstance(v, list) and v:
                                return v[0].get("url")
                            r2 = _find_video(v)
                            if r2: return r2
                    elif isinstance(obj, list):
                        for item in obj:
                            r2 = _find_video(item)
                            if r2: return r2
                    return None
                video_url = _find_video(d)
                if video_url:
                    ext = ".mp3" if format_type == "mp3" else ".mp4"
                    return _save_stream(video_url, "instagram_video", ext), "Instagram"

    except Exception as e:
        logger.error(f"RapidAPI download [{platform}]: {e}")
    return None, "media"

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/info")
async def get_info(url: str):
    platform = detect_platform(url)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if platform == "instagram":
        opts = _apply_ig_opts(opts)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"title": info.get("title", "Vidéo"), "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail"), "uploader": info.get("uploader", ""), "platform": platform}
    except Exception as e:
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
                    return {"title": d.get("title", "TikTok"), "duration": d.get("duration", 0),
                            "thumbnail": d.get("cover"), "uploader": d.get("author", {}).get("nickname", ""),
                            "platform": platform}
            elif platform == "instagram":
                return {"title": "Vidéo Instagram", "duration": 0,
                        "thumbnail": None, "uploader": "Instagram", "platform": platform}
            elif platform == "youtube":
                r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl",
                    params={"id": _extract_yt_id(url)},
                    headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"}, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    return {"title": d.get("title", "YouTube"), "duration": int(d.get("duration", 0) or 0),
                            "thumbnail": None, "uploader": "YouTube", "platform": platform}
        except Exception as e2:
            logger.error(f"RapidAPI info [{platform}]: {e2}")

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")


@app.get("/download")
async def download(url: str, format: str = "mp4", quality: str = "720"):
    platform = detect_platform(url)
    tpl = os.path.join(DOWNLOAD_PATH, "%(id)s.%(ext)s")
    base_opts = {"outtmpl": tpl, "quiet": False, "no_warnings": False, "restrictfilenames": True}

    if platform == "instagram":
        base_opts = _apply_ig_opts(base_opts)

    if format == "mp3":
        opts = {**base_opts, "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"}
    else:
        qmap = {"1080": "best[height<=1080][ext=mp4]/best[height<=1080]/best",
                "720":  "best[height<=720][ext=mp4]/best[height<=720]/best",
                "480":  "best[height<=480][ext=mp4]/best[height<=480]/best",
                "360":  "best[height<=360][ext=mp4]/best[height<=360]/best"}
        fmt = "best[ext=mp4]/best" if platform == "instagram" else qmap.get(quality, qmap["720"])
        opts = {**base_opts, "format": fmt}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            vid = info.get("id", "video")
            for ext in [".mp4", ".mp3", ".mkv", ".webm", ".m4a"]:
                candidate = os.path.join(DOWNLOAD_PATH, f"{vid}{ext}")
                if os.path.exists(candidate):
                    title = safe_filename(info.get("title", "video"))
                    dl_name = f"{title}{ext}"
                    return FileResponse(candidate, media_type="application/octet-stream", filename=dl_name,
                                        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})
    except Exception as e:
        logger.warning(f"yt-dlp download [{platform}] failed → RapidAPI")

    file_path, title = _rapi_download(url, platform, format)
    if file_path and os.path.exists(file_path):
        ext = os.path.splitext(file_path)[1]
        dl_name = f"{safe_filename(title)}{ext}"
        return FileResponse(file_path, media_type="application/octet-stream", filename=dl_name,
                            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})

    raise HTTPException(status_code=400, detail="Téléchargement impossible")


@app.get("/health")
async def health():
    return {"status": "ok", "proxies": len(PROXY_LIST), "rapidapi_keys": len(RAPIDAPI_KEYS)}
