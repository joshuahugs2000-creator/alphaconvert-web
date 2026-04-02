"""
api.py — Backend FastAPI AlphaConvert
Fixes:
  - yt-dlp : format simplifié sans ffmpeg (best[ext=mp4] au lieu de bestvideo+bestaudio)
  - ytstream : formats est une LISTE, pas un dict — parser corrigé
  - TikTok vm. : résolution du redirect avant appel API
  - YouTube Media Downloader : endpoint /streams au lieu de /videos (404 fix)
  - ytstream : proxy stream côté serveur au lieu de RedirectResponse 302 (403 fix)
"""
import os, re, logging, unicodedata, httpx, urllib.parse, time, glob, uuid, shutil
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
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

def _find_ffmpeg() -> str:
    candidates = [shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                  "/nix/var/nix/profiles/default/bin/ffmpeg"]
    candidates.extend(glob.glob("/nix/store/*/bin/ffmpeg"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""

FFMPEG_PATH = _find_ffmpeg()
FFMPEG_OK   = bool(FFMPEG_PATH)
logger.info(f"ffmpeg={'FOUND: ' + FFMPEG_PATH if FFMPEG_OK else 'NOT FOUND'}")

# ── COOKIES YOUTUBE ───────────────────────────────────────────────────────────
_COOKIES_CONTENT = """\
# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
# This is a generated file! Do not edit.

.youtube.com\tTRUE\t/\tFALSE\t1808138874\tSID\tg.a0007wgM397FSAgzW4-A2EB6JMHwt7MmqVdrPkyjTkt0m_3u7rEnLdaB0crZrS4vKbOoBUg8RQACgYKAR8SARMSFQHGX2Mio6GeRdSFz3DUmiM-DaZyQBoVAUF8yKplc1lrx1EeC_vbSTTAlWea0076
.youtube.com\tTRUE\t/\tTRUE\t1808138874\t__Secure-1PSID\tg.a0007wgM397FSAgzW4-A2EB6JMHwt7MmqVdrPkyjTkt0m_3u7rEnYkA27YX0YIwpMcCVgROaVgACgYKAZ4SARMSFQHGX2MiB6nP2JSSsPg7BsA14mh_wBoVAUF8yKpbvPhkxL_r3ipbK68NE-V60076
.youtube.com\tTRUE\t/\tTRUE\t1808138874\t__Secure-3PSID\tg.a0007wgM397FSAgzW4-A2EB6JMHwt7MmqVdrPkyjTkt0m_3u7rEnm_vVR6WGMDLz3uV6ZaLvYAACgYKAasSARMSFQHGX2MiH8dAVV8r1TiZcEeeOCTS1BoVAUF8yKoOL5-r_mO6MLJjHInupdhb0076
.youtube.com\tTRUE\t/\tFALSE\t1808138874\tHSID\tAkyQS2IjJmAr-DZDA
.youtube.com\tTRUE\t/\tTRUE\t1808138874\tSSID\tAH-aAHzL3hBv4cmJ9
.youtube.com\tTRUE\t/\tFALSE\t1808138874\tAPISID\tWAkeoC2BTbjaBQLP/AHmiB4XdCQ9tZ6D8w
.youtube.com\tTRUE\t/\tTRUE\t1808138874\tSAPISID\tPDjqryWlBviLOhUk/AqbPKxAB5iD9D0KMx
.youtube.com\tTRUE\t/\tTRUE\t1808138874\t__Secure-1PAPISID\tPDjqryWlBviLOhUk/AqbPKxAB5iD9D0KMx
.youtube.com\tTRUE\t/\tTRUE\t1808138874\t__Secure-3PAPISID\tPDjqryWlBviLOhUk/AqbPKxAB5iD9D0KMx
.youtube.com\tTRUE\t/\tTRUE\t1808138876\tLOGIN_INFO\tAFmmF2swRQIgdbygpXJABvWbFwLGduiH24AeN5uvGM8ch7GiJr_GJc0CIQCSZB8r6oG0s2QMlcr-lvKmeGYzJ6GuQBEsapMSBRdMZg:QUQ3MjNmeG0wLWpDZWlFVnZ1V2JHaGdibXhncHNCdVo3dEtWeUJEVW9KSVNJaGI5LWI2aUpSRUpRSXp6S2NRRVE5MnNrcFhTUVVOR0pmLVFpYnN6R3ZRSzFOdU9sOEJHUXFxUVo1Vk5INU0wRlVnbW1YdkQ4R3Y0NXVYbGw3bFd1ODQ3Tms5dEFQY1ZnNHM4Nlo0NlRCRThiQTUtNzdaNG1n
.youtube.com\tTRUE\t/\tTRUE\t1809544770\tPREF\tf4=4000000&tz=Africa.Sao_Tome
.youtube.com\tTRUE\t/\tFALSE\t1806520773\tSIDCC\tAKEyXzWnpTd1IvmNoYaPtUZV3BbSodKZ6KXU15aiounsBpMM5tz07TisXWRcfY2Ngm0Cvo9_rQ
.youtube.com\tTRUE\t/\tTRUE\t1806520773\t__Secure-1PSIDCC\tAKEyXzWr6U6ZnuEpWq4AEnZcmHwXzv4SVvQRb2xgckz1QEIDTlPP9Ol8_u_-QcOESmCYBjsA
.youtube.com\tTRUE\t/\tTRUE\t1806520773\t__Secure-3PSIDCC\tAKEyXzVDu5E9RSmxSJ4wYs3o5q7NtRP1qkI2S4cdLVT3QbeoHzDVG2fOhbKN385XeBq9d80nPQ
.youtube.com\tTRUE\t/\tTRUE\t1790536768\tVISITOR_INFO1_LIVE\tftuwYM3zThM
.youtube.com\tTRUE\t/\tTRUE\t1790536768\tVISITOR_PRIVACY_METADATA\tCgJURxIEGgAgJw%3D%3D
.youtube.com\tTRUE\t/\tTRUE\t0\tYSC\toWxWEtB0hPU
.youtube.com\tTRUE\t/\tTRUE\t1790536751\t__Secure-ROLLOUT_TOKEN\tCI6W3tO1n-PCPhDOnuOW-KGTAxiSram97cqTAw%3D%3D
"""

COOKIES_FILE = "/tmp/yt_cookies.txt"

def _setup_cookies():
    env_cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
    content = env_cookies if env_cookies else _COOKIES_CONTENT.strip()
    with open(COOKIES_FILE, "w") as f:
        f.write(content + "\n")
    source = "ENV" if env_cookies else "hardcoded"
    logger.info(f"YouTube cookies ecrits depuis {source} ({len(content)} chars)")

_setup_cookies()

def _clean_proxy_env():
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
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""

def _ydl_base(uid: str) -> dict:
    opts = {
        "outtmpl":           os.path.join(DOWNLOAD_PATH, f"{uid}.%(ext)s"),
        "quiet":             True,
        "no_warnings":       True,
        "noplaylist":        True,
        "restrictfilenames": False,
        "proxy":             "",
        "cookiefile":        COOKIES_FILE,
        "extractor_args":    {"youtube": {"player_client": ["web", "android"]}},
    }
    if FFMPEG_OK:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts

def _find_file(uid: str):
    files = [f for f in glob.glob(os.path.join(DOWNLOAD_PATH, f"{uid}*"))
             if os.path.isfile(f) and os.path.getsize(f) > 1024]
    return max(files, key=os.path.getsize) if files else None

def _serve(path: str, title: str) -> FileResponse:
    ext     = os.path.splitext(path)[1]
    dl_name = f"{safe_filename(title)}{ext}"
    size    = os.path.getsize(path)
    logger.info(f"Serving: {dl_name} ({size:,} bytes)")
    return FileResponse(path, media_type="application/octet-stream", filename=dl_name,
                        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'})

def _save_stream(dl_url: str, title: str, ext: str) -> str:
    path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex[:8]}{ext}")
    is_yt = "googlevideo.com" in dl_url or "youtube" in dl_url
    hdrs = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "*/*",
        "Accept-Encoding": "identity",
        "Range":           "bytes=0-",
        "Referer":         "https://www.youtube.com/" if is_yt else "https://www.tiktok.com/",
        "Origin":          "https://www.youtube.com" if is_yt else "https://www.tiktok.com",
    }
    with httpx.stream("GET", dl_url, timeout=300, follow_redirects=True, headers=hdrs) as r:
        if r.status_code not in (200, 206):
            raise RuntimeError(f"CDN refuse : HTTP {r.status_code}")
        with open(path, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)
    size = os.path.getsize(path)
    if size == 0:
        raise RuntimeError("Fichier vide (0 octet)")
    logger.info(f"_save_stream OK: {os.path.basename(path)} ({size:,} bytes)")
    return path


# ── TIKTOK ────────────────────────────────────────────────────────────────────
def _resolve_tiktok_url(url: str) -> str:
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            r = httpx.get(url, follow_redirects=True, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0"})
            resolved = str(r.url)
            logger.info(f"TikTok redirect: {url} → {resolved}")
            return resolved
        except Exception as e:
            logger.warning(f"TikTok redirect failed: {e}")
    return url

def _tiktok_scraptik(url: str, fmt: str):
    url = _resolve_tiktok_url(url)
    key = _get_rapidapi_key()
    if not key:
        return None, "tiktok"
    m = re.search(r"/video/(\d+)", url)
    if not m:
        return None, "tiktok"
    video_id = m.group(1)
    try:
        r = httpx.get(
            "https://scraptik.p.rapidapi.com/get-post",
            params={"aweme_id": video_id},
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "scraptik.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"TikTok ScrapTik -> {r.status_code}")
        if r.status_code == 200:
            body  = r.json()
            post  = body.get("aweme_detail") or body.get("aweme_list", [{}])[0] if body else {}
            title = (post.get("desc") or "tiktok")[:60]
            video = post.get("video", {})
            if fmt == "mp3":
                music  = post.get("music", {})
                dl_url = (music.get("play_url") or {}).get("uri") or (music.get("play_url") or {}).get("url_list", [None])[0]
                ext    = ".mp3"
            else:
                play_addr = video.get("play_addr_h264") or video.get("download_addr") or video.get("play_addr") or {}
                url_list  = play_addr.get("url_list", [])
                dl_url    = url_list[0] if url_list else None
                ext       = ".mp4"
            if dl_url:
                return _save_stream(dl_url, title, ext), title
    except Exception as e:
        logger.error(f"TikTok ScrapTik: {e}")
    return None, "tiktok"


def _tiktok_scraper2(url: str, fmt: str):
    url = _resolve_tiktok_url(url)
    key = _get_rapidapi_key()
    if not key:
        return None, "tiktok"
    try:
        endpoint = "/video/no-watermark" if fmt == "mp4" else "/video/music"
        r = httpx.get(
            f"https://tiktok-scraper2.p.rapidapi.com{endpoint}",
            params={"video_url": url, "fresh": "1"},
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper2.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"TikTok scraper2 -> {r.status_code}")
        if r.status_code == 200:
            body  = r.json()
            title = body.get("title", "tiktok")
            if fmt == "mp3":
                dl_url = body.get("music", {}).get("url") or body.get("url")
                ext    = ".mp3"
            else:
                dl_url = body.get("video", {}).get("noWatermark") or body.get("url") or body.get("nwm_video_url")
                ext    = ".mp4"
            if dl_url:
                return _save_stream(dl_url, title, ext), title
    except Exception as e:
        logger.error(f"TikTok scraper2: {e}")
    return None, "tiktok"


def _tiktok_scraper7(url: str, fmt: str):
    url = _resolve_tiktok_url(url)
    key = _get_rapidapi_key()
    if not key:
        return None, "tiktok"
    try:
        r = httpx.get(
            "https://tiktok-scraper7.p.rapidapi.com/video/info",
            params={"url": url, "hd": "1"},
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"TikTok scraper7 -> {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            if body.get("code", 0) != 0:
                return None, "tiktok"
            d     = body.get("data") or body
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
        logger.error(f"TikTok scraper7: {e}")
    return None, "tiktok"


def _tiktok_rapidapi(url: str, fmt: str):
    path, title = _tiktok_scraptik(url, fmt)
    if path:
        return path, title
    logger.info("ScrapTik echoue -> essai scraper2")
    path, title = _tiktok_scraper2(url, fmt)
    if path:
        return path, title
    logger.info("Scraper2 echoue -> essai scraper7")
    return _tiktok_scraper7(url, fmt)


# ── YOUTUBE INFO FALLBACK ─────────────────────────────────────────────────────
def _youtube_rapidapi_info(url: str) -> dict | None:
    key = _get_rapidapi_key()
    if not key:
        return None
    vid = _extract_yt_id(url)
    if not vid:
        return None
    try:
        r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl", params={"id": vid},
                      headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"},
                      timeout=15)
        if r.status_code == 200:
            d = r.json()
            return {"title": d.get("title", "YouTube Video"),
                    "duration": int(d.get("duration", 0) or 0),
                    "thumbnail": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                    "uploader": "YouTube", "platform": "youtube"}
    except Exception as e:
        logger.error(f"YouTube RapidAPI info: {e}")
    return None


# ── YOUTUBE FALLBACK MP3 via youtube-mp36 ─────────────────────────────────────
def _youtube_mp36_mp3(url: str) -> tuple:
    key = _get_rapidapi_key()
    if not key:
        return None, None
    vid = _extract_yt_id(url)
    if not vid:
        return None, None
    try:
        r = httpx.get("https://youtube-mp36.p.rapidapi.com/dl", params={"id": vid},
                      headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com"},
                      timeout=40)
        logger.info(f"youtube-mp36 → {r.status_code}")
        if r.status_code == 200:
            d    = r.json()
            link = d.get("link")
            if link:
                return _save_stream(link, d.get("title", "video"), ".mp3"), d.get("title", "video")
    except Exception as e:
        logger.warning(f"youtube-mp36 failed: {e}")
    return None, None


# ── YOUTUBE MEDIA DOWNLOADER ──────────────────────────────────────────────────
def _youtube_media_downloader(url: str, quality: str, fmt: str) -> tuple:
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
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"YT-MediaDownloader details -> {r.status_code}")
        if r.status_code != 200:
            return None, None
        title = r.json().get("title", "video")

        if fmt == "mp3":
            r2 = httpx.get(
                "https://youtube-media-downloader.p.rapidapi.com/v2/video/audios",
                params={"videoId": vid},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"},
                timeout=30,
            )
            logger.info(f"YT-MediaDownloader audios -> {r2.status_code}")
            if r2.status_code == 200:
                audios = r2.json().get("items", [])
                if audios:
                    dl_url = audios[0].get("url")
                    if dl_url:
                        return _save_stream(dl_url, title, ".mp3"), title
        else:
            # /streams est l'endpoint correct (pas /videos qui retourne 404)
            r2 = httpx.get(
                "https://youtube-media-downloader.p.rapidapi.com/v2/video/streams",
                params={"videoId": vid},
                headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "youtube-media-downloader.p.rapidapi.com"},
                timeout=30,
            )
            logger.info(f"YT-MediaDownloader streams -> {r2.status_code}")
            if r2.status_code == 200:
                data     = r2.json()
                videos   = data.get("videos") or data.get("items") or []
                target_h = int(quality)
                mp4_vids = [v for v in videos
                            if v.get("url") and (
                                v.get("extension") == "mp4"
                                or "mp4" in str(v.get("mimeType", "")).lower()
                                or "mp4" in str(v.get("container", "")).lower()
                            )]
                if mp4_vids:
                    under = [v for v in mp4_vids if (v.get("height") or 9999) <= target_h]
                    pool  = under if under else mp4_vids
                    best  = sorted(pool, key=lambda x: x.get("height") or 0, reverse=True)[0]
                    logger.info(f"YT-MediaDownloader streams: height={best.get('height')}")
                    return _save_stream(best["url"], title, ".mp4"), title
                any_vid = [v for v in videos if v.get("url")]
                if any_vid:
                    best = sorted(any_vid, key=lambda x: x.get("height") or 0, reverse=True)[0]
                    logger.info(f"YT-MediaDownloader streams (any): height={best.get('height')}")
                    return _save_stream(best["url"], title, ".mp4"), title
    except Exception as e:
        logger.warning(f"YT-MediaDownloader: {e}")
    return None, None


# ── YOUTUBE FALLBACK MP4 via ytstream (proxy stream côté serveur) ─────────────
def _youtube_ytstream_get_url(url: str, quality: str) -> tuple:
    """
    Retourne (cdn_url, title) depuis ytstream.
    L'URL CDN Google est signée avec l'IP du serveur Railway qui a fait la requête.
    → On streame côté serveur Railway (même IP) vers le client via _youtube_ytstream_proxy.
    → Ne jamais faire un RedirectResponse (le navigateur a une IP différente → 403).
    """
    key = _get_rapidapi_key()
    if not key:
        return None, ""
    vid = _extract_yt_id(url)
    if not vid:
        return None, ""
    try:
        r = httpx.get(
            "https://ytstream-download-youtube-videos.p.rapidapi.com/dl",
            params={"id": vid},
            headers={"X-RapidAPI-Key": key,
                     "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"ytstream -> {r.status_code}")
        if r.status_code != 200:
            return None, ""
        data  = r.json()
        title = data.get("title", "video")
        raw_formats = data.get("formats", [])
        if isinstance(raw_formats, dict):
            formats_list = list(raw_formats.values())
        elif isinstance(raw_formats, list):
            formats_list = raw_formats
        else:
            return None, ""
        logger.info(f"ytstream formats count: {len(formats_list)}")
        target_h       = int(quality)
        priority_itags = ["22", "18"] if target_h <= 720 else ["137", "22", "18"]
        itag_map       = {str(f.get("itag", "")): f for f in formats_list if f.get("url")}
        for itag in priority_itags:
            if itag in itag_map:
                f = itag_map[itag]
                logger.info(f"ytstream: itag={itag} quality={f.get('qualityLabel','?')}")
                return f["url"], title
        mp4_with_audio = [
            f for f in formats_list
            if f.get("url") and "video/mp4" in str(f.get("mimeType", "")) and f.get("audioQuality")
        ]
        if mp4_with_audio:
            under = [f for f in mp4_with_audio if (f.get("height") or 9999) <= target_h]
            pool  = under if under else mp4_with_audio
            best  = sorted(pool, key=lambda x: x.get("height") or 0, reverse=True)[0]
            return best["url"], title
        any_mp4 = [f for f in formats_list if f.get("url") and "video/mp4" in str(f.get("mimeType", ""))]
        if any_mp4:
            best = sorted(any_mp4, key=lambda x: x.get("height") or 0, reverse=True)[0]
            return best["url"], title
    except Exception as e:
        logger.warning(f"ytstream failed: {e}")
    return None, ""


def _youtube_ytstream_proxy(cdn_url: str, title: str) -> StreamingResponse:
    """
    Proxy stream : Railway télécharge depuis CDN Google (même IP qui a signé l'URL)
    et pipe les bytes directement vers le navigateur client.
    Résout le 403 causé par l'IP-lock des URLs signées Google Video.
    """
    safe_title = safe_filename(title)
    hdrs = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "*/*",
        "Accept-Encoding": "identity",
        "Referer":         "https://www.youtube.com/",
        "Origin":          "https://www.youtube.com",
    }

    def iter_content():
        with httpx.stream("GET", cdn_url, timeout=300, follow_redirects=True, headers=hdrs) as r:
            if r.status_code not in (200, 206):
                logger.error(f"ytstream proxy CDN refuse: HTTP {r.status_code}")
                return
            logger.info(f"ytstream proxy: streaming vers client ({r.headers.get('content-length', '?')} bytes)")
            for chunk in r.iter_bytes(65536):
                yield chunk

    # Récupérer Content-Length pour que le navigateur affiche la progression
    response_headers = {"Content-Disposition": f'attachment; filename="{safe_title}.mp4"'}
    try:
        head = httpx.head(cdn_url, timeout=10, follow_redirects=True, headers=hdrs)
        if "content-length" in head.headers:
            response_headers["Content-Length"] = head.headers["content-length"]
    except Exception:
        pass

    return StreamingResponse(
        iter_content(),
        media_type="video/mp4",
        headers=response_headers,
    )


# ── THUMBNAIL PROXY ───────────────────────────────────────────────────────────
@app.get("/thumbnail-proxy")
async def thumbnail_proxy(url: str):
    allowed = ["img.youtube.com", "i.ytimg.com", "p16-sign.tiktokcdn.com",
               "p19-sign.tiktokcdn.com", "p16-sign-va.tiktokcdn.com", "p77-sign.tiktokcdn.com"]
    try:
        parsed = urllib.parse.urlparse(url)
        if not any(d in parsed.netloc for d in allowed):
            raise HTTPException(status_code=400, detail="Domaine non autorise")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="URL invalide")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return StreamingResponse(iter([r.content]),
                                     media_type=r.headers.get("content-type", "image/jpeg"),
                                     headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        logger.error(f"thumbnail-proxy error: {e}")
        raise HTTPException(status_code=502, detail="Impossible de recuperer la miniature")


# ── INFO ──────────────────────────────────────────────────────────────────────
@app.get("/info", dependencies=SECURITY)
async def get_info(url: str):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Plateforme non supportee")

    saved = _clean_proxy_env()
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "proxy": "", "cookiefile": COOKIES_FILE,
                "extractor_args": {"youtube": {"player_client": ["web", "android"]}}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            thumb = info.get("thumbnail", "")
            if not thumb and platform == "youtube":
                vid   = _extract_yt_id(url)
                thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else ""
            return {"title": info.get("title", "Video"), "duration": info.get("duration", 0),
                    "thumbnail": thumb, "uploader": info.get("uploader", ""), "platform": platform}
        except Exception as e:
            logger.warning(f"yt-dlp info failed: {e}")
    finally:
        _restore_proxy_env(saved)

    if platform == "youtube":
        rapi_info = _youtube_rapidapi_info(url)
        if rapi_info:
            return rapi_info
        vid   = _extract_yt_id(url)
        thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else ""
        return {"title": "YouTube Video", "duration": 0, "thumbnail": thumb,
                "uploader": "YouTube", "platform": platform}

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")


# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
@app.get("/download", dependencies=SECURITY)
async def download(url: str, format: str = "mp4", quality: str = "720"):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if format  not in ("mp4", "mp3"):                format  = "mp4"
    if quality not in ("360", "480", "720", "1080"): quality = "720"

    # ── TikTok ────────────────────────────────────────────────────────────────
    if platform == "tiktok":
        path, title = _tiktok_rapidapi(url, format)
        if path and os.path.exists(path):
            return _serve(path, title)
        raise HTTPException(status_code=500, detail="Telechargement impossible")

    # ── YouTube ───────────────────────────────────────────────────────────────
    uid  = uuid.uuid4().hex[:8]
    base = _ydl_base(uid)

    if format == "mp3":
        if FFMPEG_OK:
            opts = {**base, "format": "bestaudio/best",
                    "postprocessors": [{"key": "FFmpegExtractAudio",
                                        "preferredcodec": "mp3", "preferredquality": "192"}]}
        else:
            opts = {**base, "format": "bestaudio[ext=m4a]/bestaudio/best"}
    else:
        if FFMPEG_OK:
            fmt_map = {
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            }
            opts = {**base, "format": fmt_map[quality], "merge_output_format": "mp4"}
        else:
            fmt_map = {
                "1080": "best[ext=mp4][height<=720]/best[ext=mp4]/best",
                "720":  "best[ext=mp4][height<=720]/best[ext=mp4]/best",
                "480":  "best[ext=mp4][height<=480]/best[ext=mp4]/best",
                "360":  "best[ext=mp4][height<=360]/best[ext=mp4]/best",
            }
            opts = {**base, "format": fmt_map[quality]}

    logger.info(f"DL | platform={platform} fmt={format} q={quality} ffmpeg={FFMPEG_OK} uid={uid}")

    # Tentative 1 : yt-dlp avec cookies
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

    # Tentative 2 : YouTube Media Downloader (endpoint /streams, hébergé, pas IP-locked)
    logger.info("yt-dlp failed → trying youtube-media-downloader")
    path, title = _youtube_media_downloader(url, quality, format)
    if path and os.path.exists(path):
        return _serve(path, title)

    # Tentative 3 : ytstream → proxy stream côté serveur Railway
    # (l'URL CDN est signée avec l'IP Railway → on streame depuis Railway, pas redirect navigateur)
    if format == "mp4":
        logger.info("youtube-media-downloader failed → trying ytstream proxy")
        cdn_url, yt_title = _youtube_ytstream_get_url(url, quality)
        if cdn_url:
            logger.info("ytstream: proxy stream cote serveur vers client")
            return _youtube_ytstream_proxy(cdn_url, yt_title)

    # Tentative 4 : youtube-mp36 (MP3 seulement)
    if format == "mp3":
        logger.info("yt-dlp/media-downloader failed → trying youtube-mp36 (MP3)")
        path, title = _youtube_mp36_mp3(url)
        if path and os.path.exists(path):
            return _serve(path, title)

    raise HTTPException(status_code=500, detail="Telechargement impossible")


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "rapidapi_keys": len(RAPIDAPI_KEYS),
            "ffmpeg": FFMPEG_OK, "ffmpeg_path": FFMPEG_PATH,
            "cookies": os.path.exists(COOKIES_FILE)}


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
            r   = await c.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                               json={"chat_id": cid, "text": text})
            res = r.json()
            return {"success": True} if res.get("ok") else {"error": res.get("description")}
        except Exception as e:
            return {"error": str(e)}


# ── STATIC ────────────────────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=".", html=True), name="static")
