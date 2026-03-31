"""
api.py — Backend FastAPI AlphaConvert
"""
import os, re, logging, unicodedata, httpx, urllib.parse, time, glob, uuid
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

import shutil as _shutil
FFMPEG_PATH = _shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
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

def _ydl_base(uid: str) -> dict:
    opts = {
        "outtmpl": os.path.join(DOWNLOAD_PATH, f"{uid}.%(ext)s"),
        "quiet": True, "no_warnings": True,
        "noplaylist": True, "restrictfilenames": False,
    }
    if FFMPEG_OK: opts["ffmpeg_location"] = FFMPEG_PATH
    if PROXY_URL: opts["proxy"] = PROXY_URL
    return opts

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

def _save_stream(dl_url: str, title: str, ext: str) -> str:
    path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex[:8]}{ext}")
    with httpx.stream("GET", dl_url, timeout=120, follow_redirects=True,
                      headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.tiktok.com/"}) as r:
        r.raise_for_status()
        with open(path,"wb") as f:
            for chunk in r.iter_bytes(65536): f.write(chunk)
    return path

def _tiktok_rapidapi(url: str, fmt: str):
    key = _get_rapidapi_key()
    if not key: return None, "tiktok"
    try:
        r = httpx.get("https://tiktok-scraper7.p.rapidapi.com/video/info",
            params={"url":url,"hd":"1"},
            headers={"X-RapidAPI-Key":key,"X-RapidAPI-Host":"tiktok-scraper7.p.rapidapi.com"},
            timeout=30)
        if r.status_code == 200:
            d     = r.json().get("data",{})
            title = d.get("title","tiktok")
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

# ── INFO ─────────────────────────────────────────────────────────────────────
@app.get("/info", dependencies=SECURITY)
async def get_info(url: str):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Plateforme non supportee")

    opts = {"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True}
    if PROXY_URL: opts["proxy"] = PROXY_URL
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumb = info.get("thumbnail","")
        if platform == "youtube" and not thumb:
            thumb = f"https://img.youtube.com/vi/{_extract_yt_id(url)}/hqdefault.jpg"
        return {"title":info.get("title","Video"),"duration":info.get("duration",0),
                "thumbnail":thumb,"uploader":info.get("uploader",""),"platform":platform}
    except Exception as e:
        logger.warning(f"yt-dlp info failed: {e}")

    if platform == "youtube":
        vid = _extract_yt_id(url)
        return {"title":"YouTube","duration":0,
                "thumbnail":f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
                "uploader":"YouTube","platform":platform}

    raise HTTPException(status_code=400, detail="Impossible d'analyser ce lien")

# ── DOWNLOAD ─────────────────────────────────────────────────────────────────
@app.get("/download", dependencies=SECURITY)
async def download(url: str, format: str = "mp4", quality: str = "720"):
    url      = validate_url(url)
    url      = clean_url(url)
    platform = detect_platform(url)

    if format  not in ("mp4","mp3"):               format  = "mp4"
    if quality not in ("360","480","720","1080"):   quality = "720"

    uid  = uuid.uuid4().hex[:8]
    base = _ydl_base(uid)

    if format == "mp3":
        if FFMPEG_OK:
            opts = {**base, "format":"bestaudio/best",
                    "postprocessors":[{"key":"FFmpegExtractAudio",
                                       "preferredcodec":"mp3","preferredquality":"192"}]}
        else:
            opts = {**base, "format":"bestaudio[ext=m4a]/bestaudio"}
    else:
        if FFMPEG_OK:
            fmts = {
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
                "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
                "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
                "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best",
            }
            opts = {**base, "format":fmts[quality], "merge_output_format":"mp4"}
        else:
            fmts = {
                "1080": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "720":  "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                "480":  "best[height<=480][ext=mp4]/best[ext=mp4]/best",
                "360":  "best[height<=360][ext=mp4]/best[ext=mp4]/best",
            }
            opts = {**base, "format":fmts[quality]}

    logger.info(f"DL start | platform={platform} format={format} quality={quality} ffmpeg={FFMPEG_OK} uid={uid}")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = _find_file(uid)
        if path:
            return _serve(path, info.get("title","video"))
        logger.warning(f"No file found uid={uid}")
    except Exception as e:
        logger.error(f"yt-dlp failed: {e}")

    # Fallback TikTok uniquement
    if platform == "tiktok":
        path, title = _tiktok_rapidapi(url, format)
        if path and os.path.exists(path):
            return _serve(path, title)

    raise HTTPException(status_code=500, detail="Telechargement impossible")

# ── HEALTH ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status":"ok","rapidapi_keys":len(RAPIDAPI_KEYS),
            "ffmpeg":FFMPEG_OK,"ffmpeg_path":FFMPEG_PATH}

# ── CHAT ─────────────────────────────────────────────────────────────────────
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

# ── STATIC (en dernier) ───────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory=".", html=True), name="static")
