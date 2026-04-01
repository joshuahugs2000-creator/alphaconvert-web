"""
api.py — Backend FastAPI AlphaConvert
Fixes:
  - yt-dlp : format simplifié sans ffmpeg (best[ext=mp4] au lieu de bestvideo+bestaudio)
  - ytstream : formats est une LISTE, pas un dict — parser corrigé
  - TikTok vm. : résolution du redirect avant appel API
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
    """Résout les redirections vm.tiktok.com → URL longue avec vrai ID."""
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

def _tiktok_rapidapi(url: str, fmt: str):
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
        logger.info(f"TikTok scraper7 → {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            if body.get("code", 0) != 0:
                logger.error(f"TikTok API erreur: {body.get('msg', '')}")
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
        logger.error(f"TikTok RapidAPI: {e}")
    return None, "tiktok"


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


# ── YOUTUBE FALLBACK MP3 ──────────────────────────────────────────────────────
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
            d = r.json()
            link = d.get("link")
            if link:
                return _save_stream(link, d.get("title", "video"), ".mp3"), d.get("title", "video")
    except Exception as e:
        logger.warning(f"youtube-mp36 failed: {e}")
    return None, None


# ── YOUTUBE FALLBACK MP4 via ytstream ─────────────────────────────────────────
def _youtube_ytstream(url: str, quality: str) -> tuple:
    """
    ytstream retourne formats comme une LISTE d'objets :
    [{"itag":"18","url":"...","mimeType":"video/mp4","quality":"medium",...}, ...]
    """
    key = _get_rapidapi_key()
    if not key:
        return None, None
    vid = _extract_yt_id(url)
    if not vid:
        return None, None
    try:
        r = httpx.get(
            "https://ytstream-download-youtube-videos.p.rapidapi.com/dl",
            params={"id": vid},
            headers={"X-RapidAPI-Key": key,
                     "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com"},
            timeout=30,
        )
        logger.info(f"ytstream → {r.status_code}")
        if r.status_code != 200:
            return None, None

        data  = r.json()
        title = data.get("title", "video")

        # formats peut être une LISTE ou un DICT selon la version de l'API
        raw_formats = data.get("formats", [])
        if isinstance(raw_formats, dict):
            formats_list = list(raw_formats.values())
        elif isinstance(raw_formats, list):
            formats_list = raw_formats
        else:
            logger.warning(f"ytstream: formats type inattendu: {type(raw_formats)}")
            return None, None

        logger.info(f"ytstream formats count: {len(formats_list)}")

        target_h = int(quality)

        # Priorité : formats muxés MP4 (ont de l'audio) sous la qualité demandée
        # itag 22 = 720p muxé, itag 18 = 360p muxé — les seuls avec vidéo+audio garanti
        priority_itags = ["22", "18"] if target_h <= 720 else ["137", "22", "18"]

        # D'abord chercher par itag prioritaire
        itag_map = {str(f.get("itag", "")): f for f in formats_list if f.get("url")}
        for itag in priority_itags:
            if itag in itag_map:
                f = itag_map[itag]
                logger.info(f"ytstream: using itag={itag} quality={f.get('qualityLabel','?')}")
                path = _save_stream(f["url"], title, ".mp4")
                return path, title

        # Fallback : meilleur MP4 avec audio dispo
        mp4_with_audio = [
            f for f in formats_list
            if f.get("url")
            and "video/mp4" in str(f.get("mimeType", ""))
            and f.get("audioQuality")  # non-null = a de l'audio
        ]
        if mp4_with_audio:
            # Trier par hauteur décroissante, garder sous target_h
            under = [f for f in mp4_with_audio
                     if (f.get("height") or 9999) <= target_h]
            pool  = under if under else mp4_with_audio
            best  = sorted(pool, key=lambda x: x.get("height") or 0, reverse=True)[0]
            logger.info(f"ytstream: fallback muxed height={best.get('height')}")
            path = _save_stream(best["url"], title, ".mp4")
            return path, title

        # Dernier recours : n'importe quel MP4
        any_mp4 = [f for f in formats_list if f.get("url") and "video/mp4" in str(f.get("mimeType", ""))]
        if any_mp4:
            best = sorted(any_mp4, key=lambda x: x.get("height") or 0, reverse=True)[0]
            logger.info(f"ytstream: last resort height={best.get('height')}")
            path = _save_stream(best["url"], title, ".mp4")
            return path, title

    except Exception as e:
        logger.warning(f"ytstream failed: {e}")
    return None, None


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

    # ── YouTube ────────────────────────────────────────────────────────────────
    uid  = uuid.uuid4().hex[:8]
    base = _ydl_base(uid)

    if format == "mp3":
        if FFMPEG_OK:
            opts = {**base, "format": "bestaudio/best",
                    "postprocessors": [{"key": "FFmpegExtractAudio",
                                        "preferredcodec": "mp3", "preferredquality": "192"}]}
        else:
            # Sans ffmpeg : m4a direct
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
            # FIX : sans ffmpeg on ne peut pas merger → format progressif muxé uniquement
            # best[ext=mp4] = meilleur format MP4 avec vidéo+audio dans un seul fichier
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

    # Tentative 2 : ytstream fallback (MP4)
    if format == "mp4":
        logger.info("yt-dlp failed → trying ytstream")
        path, title = _youtube_ytstream(url, quality)
        if path and os.path.exists(path):
            return _serve(path, title)

    # Tentative 3 : youtube-mp36 (MP3 seulement)
    if format == "mp3":
        logger.info("yt-dlp failed → trying youtube-mp36 (MP3)")
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
