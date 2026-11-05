import asyncio
import glob
import html
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import yt_dlp
from telethon import TelegramClient, events, utils, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.sessions import MemorySession, StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, GroupCallConfig

# ==========================================
# KONFIGURASI (semua dari env / GitHub Secrets)
# ==========================================
def _env(name, default=""):
    return os.environ.get(name) or default

def _need(name):
    v = _env(name)
    if not v:
        raise SystemExit(f"❌ Env {name} belum diisi. Tambahkan di GitHub: Settings > Secrets and variables > Actions.")
    return v

API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
BOT_TOKEN = _need("BOT_TOKEN")
OWNER_ID = int(_need("OWNER_ID"))
SESSION_STRING = _env("SESSION_STRING")
LK21_API_BASE = _env("LK21_API_BASE", "https://api-lk21.vercel.app").rstrip("/")

SILENT_AUDIO = "https://github.com/anars/blank-audio/raw/master/1-second-of-silence.mp3"
COOKIES = "cookies.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# ---- FILTER LAGU ----
FILTER_ON = True
MIN_DUR = 60
MAX_DUR = 600
MAX_RESULTS = 5
BLOCKED_WORDS = [
    "reaction", "react", "karaoke", "tutorial", "review", "trailer",
    "interview", "podcast", "full album", "1 hour", "10 hours",
    "compilation", "playlist", "gameplay", "teaser",
]

# ---- VIDEO SETTINGS ----
VIDEO_HEIGHT = 720
HEMAT_HEIGHT = 360
HEMAT_ON = False
PREV_HEIGHT = 720
VIDEO_SIZES = {
    144: (256, 144, 15),
    240: (426, 240, 20),
    360: (640, 360, 24),
    480: (854, 480, 24),
    720: (1280, 720, 30),
}

# ---- LAIN-LAIN ----
FIRST_PLAY_RETRY = True
AUDIO_PLAIN = True
LOOP = False
START_TIME = time.time()
manager_bot = TelegramClient(MemorySession(), API_ID, API_HASH)

# ==========================================
# STATE
# ==========================================
userbot_app = None
userbot_call = None
TARGET_VC_ID = None
queues = {}
current_songs = {}
timers = {}
PENDING = {}
REQUESTS = {}
ASKED = set()
PLAY_ERRORS = {}
DATA = {"admins": {}}
DATA_DIRTY = False
DATA_TAG = "MUSICBOT_DATA "

# Cinema State
CINEMA_ITEMS = {}
CINEMA_BASE = {}
CINEMA_VIEW = {}
CINEMA_LOOKUPS = {}
CINEMA_PAGE_SIZE = 8
CINEMA_ENDPOINTS = {
    "latest": ("movies", "Film terbaru"), "popular": ("popular/movies", "Film populer"),
    "release": ("recent-release/movies", "Rilis terbaru"), "top": ("top-rated/movies", "Rating tertinggi"),
    "series": ("series", "Serial terbaru"), "popseries": ("popular/series", "Serial populer"),
    "seriesrelease": ("recent-release/series", "Rilis serial terbaru"), "seriestop": ("top-rated/series", "Rating serial tertinggi"),
}
LOOKUP_PATHS = {"genre": "genres", "country": "countries", "year": "years"}

def get_queue(chat_id): return queues.setdefault(chat_id, [])
def silent_song(): return {"url": SILENT_AUDIO, "title": "Silent", "duration": 0}
def clean(text): return re.sub(r"[*_\[\]~`]", "", str(text or ""))
def fmt_dur(sec):
    if not sec: return "?"
    sec = int(sec)
    if sec >= 3600: return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"
def quality_label(): return "suara saja" if VIDEO_HEIGHT == 0 else f"{VIDEO_HEIGHT}p"
def is_owner(uid): return uid == OWNER_ID
def is_admin(uid): return uid == OWNER_ID or str(uid) in DATA["admins"]

# ==========================================
# PENYIMPANAN
# ==========================================
def snapshot():
    return {
        "admins": DATA["admins"], "filter_on": FILTER_ON, "video_height": VIDEO_HEIGHT,
        "hemat_on": HEMAT_ON, "prev_height": PREV_HEIGHT, "loop": LOOP,
    }

async def save_data():
    global DATA_DIRTY
    DATA_DIRTY = True
    if not userbot_app: return
    payload = DATA_TAG + json.dumps(snapshot())
    try:
        msgs = await userbot_app.get_messages("me", search=DATA_TAG.strip(), limit=10)
        mine = next((m for m in msgs if m.out and (m.raw_text or "").startswith(DATA_TAG)), None)
        if mine:
            if (mine.raw_text or "") != payload:
                await userbot_app.edit_message("me", mine, payload)
        else:
            await userbot_app.send_message("me", payload)
        DATA_DIRTY = False
    except Exception as e:
        print(f"Gagal simpan data: {e}")

async def load_data():
    global FILTER_ON, VIDEO_HEIGHT, HEMAT_ON, PREV_HEIGHT, LOOP
    if not userbot_app: return
    try:
        msgs = await userbot_app.get_messages("me", search=DATA_TAG.strip(), limit=10)
        mine = next((m for m in msgs if m.out and (m.raw_text or "").startswith(DATA_TAG)), None)
        if not mine: return
        stored = json.loads(mine.raw_text[len(DATA_TAG):])
    except Exception:
        return
    DATA["admins"] = {**stored.get("admins", {}), **DATA["admins"]}
    if not DATA_DIRTY:
        FILTER_ON = stored.get("filter_on", FILTER_ON)
        VIDEO_HEIGHT = stored.get("video_height", VIDEO_HEIGHT)
        HEMAT_ON = stored.get("hemat_on", HEMAT_ON)
        PREV_HEIGHT = stored.get("prev_height", PREV_HEIGHT)
        LOOP = stored.get("loop", LOOP)

# ==========================================
# PY-TGCALLS HELPER
# ==========================================
def _ignore_flag():
    flags = getattr(MediaStream, "Flags", None)
    if flags is not None and hasattr(flags, "IGNORE"): return flags.IGNORE
    return getattr(MediaStream, "IGNORE", None)

def audio_stream(url):
    flag = _ignore_flag()
    if flag is not None:
        try: return MediaStream(url, video_flags=flag)
        except TypeError: pass
    return MediaStream(url)

def _video_quality_candidates():
    out = []
    w, h, fps = VIDEO_SIZES.get(VIDEO_HEIGHT, VIDEO_SIZES[720])
    try:
        from pytgcalls.types import VideoParameters
        out.append(VideoParameters(width=w, height=h, frame_rate=fps))
    except Exception: pass
    try:
        from pytgcalls.types import VideoQuality
        name = {144: "SD_480p", 240: "SD_480p", 360: "SD_480p", 480: "SD_480p", 720: "HD_720p"}.get(VIDEO_HEIGHT)
        q = getattr(VideoQuality, name, None) if name else None
        if q is not None: out.append(q)
    except Exception: pass
    return out

def _mk_stream(url, headers=None, **kw):
    if headers:
        try: return MediaStream(url, headers=headers, **kw)
        except TypeError: pass
    return MediaStream(url, **kw)

def video_stream(url, headers=None):
    for q in _video_quality_candidates():
        try: return _mk_stream(url, headers, video_parameters=q)
        except Exception: continue
    return _mk_stream(url, headers)

def make_stream(song):
    url = song["url"]
    if song.get("force_audio"): return audio_stream(url)
    if song.get("video"): return video_stream(url, song.get("headers"))
    if AUDIO_PLAIN: return MediaStream(url)
    return audio_stream(url)

async def call_any(names, *args, **kwargs):
    for n in names:
        fn = getattr(userbot_call, n, None)
        if fn is not None: return await fn(*args, **kwargs)
    raise AttributeError(f"Method {names} tidak ada di py-tgcalls ini")

async def probe_duration(url, headers=None):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd.append(url)
    p = None
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 25)
        return int(float(out.decode().strip()))
    except Exception:
        if p and p.returncode is None:
            try: p.kill()
            except Exception: pass
        return 0

# ==========================================
# MULTI-PLATFORM RESOLVER (YT-DLP)
# ==========================================
DIRECT_STREAM_RE = re.compile(r"^https?://[^\s]+\.(?:m3u8|mp4|webm|mkv|mov|flv|ts)(?:\?|$)", re.I)

def detect_source(url: str) -> str:
    low = (url or "").lower()
    if "youtube.com" in low or "youtu.be" in low: return "YouTube"
    if "soundcloud.com" in low: return "SoundCloud"
    if "vimeo.com" in low: return "Vimeo"
    if DIRECT_STREAM_RE.match(url or ""): return "Direct"
    return "Web"

def _opts(use_cookies: bool, video: bool = False) -> dict:
    h = VIDEO_HEIGHT or 720
    if video:
        fmt = f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/bv*[height<={h}]+ba/b[height<={h}]/b"
    else:
        fmt = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
    o = {
        "format": fmt, "noplaylist": True, "quiet": True, "no_warnings": True,
        "nocheckcertificate": True, "geo_bypass": True,
        "js_runtimes": {"node": {}}, "remote_components": ["ejs:github"],
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if use_cookies and os.path.exists(COOKIES): o["cookiefile"] = COOKIES
    return o

def _flat_search(prefix: str, label: str, query: str, n: int) -> list[dict]:
    opts = _opts(use_cookies=(prefix == "ytsearch"))
    opts["extract_flat"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    out = []
    for v in (info or {}).get("entries") or []:
        if not v: continue
        url = v.get("url") or v.get("webpage_url")
        if prefix == "ytsearch" and v.get("id") and not (url or "").startswith("http"):
            url = f"https://www.youtube.com/watch?v={v['id']}"
        if not url: continue
        thumb = None
        if prefix == "ytsearch" and v.get("id"):
            thumb = f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"
        else:
            t = v.get("thumbnail")
            if not t and v.get("thumbnails"): t = (v["thumbnails"][-1] or {}).get("url")
            if t and re.search(r"\.(jpe?g|png)(\?|$)", t, re.I): thumb = t
        out.append({"page": url, "title": v.get("title") or "Unknown", "duration": int(v.get("duration") or 0),
                    "channel": v.get("channel") or v.get("uploader") or "", "source": label, "thumb": thumb})
    return out

def passes_filter(item: dict) -> bool:
    d = item.get("duration") or 0
    if d and not (MIN_DUR <= d <= MAX_DUR): return False
    title = item["title"].lower()
    return not any(re.search(rf"\b{re.escape(w.lower())}\b", title) for w in BLOCKED_WORDS)

def search_candidates(query: str) -> tuple[list[dict] | None, str | None]:
    errors = []
    for prefix, label in (("ytsearch", "YouTube"), ("scsearch", "SoundCloud")):
        try:
            n = 15 if FILTER_ON else MAX_RESULTS
            items = _flat_search(prefix, label, query, n)
            if FILTER_ON: items = [i for i in items if passes_filter(i)]
            if items: return items[:MAX_RESULTS], None
            errors.append(f"{label}: tidak ada hasil yang lolos filter")
        except Exception as e:
            errors.append(f"{label}: {str(e)[:100]}")
    return None, "Lagu tidak ditemukan.\n" + "\n".join(errors)

def _song_from_info(v: dict, label: str, video: bool = False) -> dict:
    url = v.get("url")
    if not url and v.get("requested_formats"): url = v["requested_formats"][0].get("url")
    if not url and v.get("formats"):
        best = max((f for f in v["formats"] if f.get("url")), key=lambda x: x.get("tbr") or x.get("abr") or 0, default=None)
        if best: url = best["url"]
    if not url: raise RuntimeError("Link stream tidak tersedia.")
    return {"url": url, "page": v.get("webpage_url") or v.get("original_url"), "title": v.get("title", "Unknown"),
            "duration": int(v.get("duration") or 0), "source": label, "channel": v.get("channel") or v.get("uploader") or "",
            "thumb": v.get("thumbnail"), "video": video, "fmt": f"{v.get('ext', '?')}/{v.get('protocol', '?')}",
            "headers": v.get("http_headers") if label == "Web" else None}

def extract_stream(page_url: str, video: bool = False) -> tuple[dict | None, str | None]:
    label = detect_source(page_url)
    if label == "Direct" or DIRECT_STREAM_RE.match(page_url):
        return {"url": page_url, "page": page_url, "title": "Direct Stream", "duration": 0, "source": "Direct", "video": video}, None
    try:
        with yt_dlp.YoutubeDL(_opts("youtube" in label.lower(), video)) as ydl:
            info = ydl.extract_info(page_url, download=False)
        if info.get("entries"): info = info["entries"][0]
        return _song_from_info(info, label, video), None
    except Exception as e:
        return None, str(e)

def resolve_song(item: dict) -> tuple[dict | None, str | None]:
    page = item.get("page") or item.get("url")
    if not page: return None, "URL tidak valid."
    video = bool(item.get("video"))
    song, err = extract_stream(page, video=video)
    if song:
        song.setdefault("title", item.get("title") or "Unknown")
        song.setdefault("duration", int(item.get("duration") or 0))
        song.setdefault("thumb", item.get("thumb"))
        song["page"] = page
    return song, err

# ==========================================
# LK21 CINEMA CENTER (FILM & SERIES)
# ==========================================
class CinemaError(Exception): pass

def _cinema_http(path):
    req = urllib.request.Request(LK21_API_BASE + "/" + path.lstrip("/"), headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise CinemaError(f"API HTTP {e.code} untuk /{path.lstrip('/')[:60]}")
    except json.JSONDecodeError:
        raise CinemaError("API membalas bukan JSON (kemungkinan API mati/diblokir).")
    except Exception as e:
        raise CinemaError(f"API Error: {e}")

async def cinema_get(path): return await asyncio.to_thread(_cinema_http, path)

def cinema_pick(obj, *keys):
    if not isinstance(obj, dict): return None
    for k in keys:
        if obj.get(k) not in (None, "", [], {}): return obj[k]
    return None

def cinema_text(value, limit=300):
    if value is None: return ""
    value = html.unescape(str(value))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", re.sub(r"[*_`\[\]~]", "", value)).strip()[:limit]

def cinema_items(payload):
    if isinstance(payload, list): return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict): return []
    for k in ("data", "results", "movies", "series", "items", "result"):
        v = payload.get(k)
        if isinstance(v, list): return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            found = cinema_items(v)
            if found: return found
    return []

def cinema_id(item):
    v = cinema_pick(item, "id", "movieId", "movie_id", "seriesId", "series_id", "slug", "_id")
    return str(v).strip() if v is not None else ""

def cinema_title(item): return cinema_text(cinema_pick(item, "title", "name", "movieTitle", "seriesTitle") or "Tanpa judul", 90)

def cinema_url(item):
    v = cinema_pick(item, "detail_url", "movie_url", "series_url", "permalink", "web_url", "page_url", "link", "url")
    if not isinstance(v, str): return None
    p = urllib.parse.urlparse(v.strip())
    if p.scheme not in ("http", "https") or not p.netloc: return None
    if re.search(r"\.(m3u8|mp4|mkv|webm|mov)(\?|$)", p.path, re.I): return None
    return v.strip()

async def cinema_edit(target, text, buttons=None):
    try: return await target.edit(text, buttons=buttons, link_preview=False)
    except Exception:
        if hasattr(target, "reply"): return await target.reply(text, buttons=buttons, link_preview=False)
        raise

def cinema_home_buttons():
    return [
        [Button.inline("🆕 Film terbaru", b"cin:list:latest:1"), Button.inline("🔥 Film populer", b"cin:list:popular:1")],
        [Button.inline("🎞 Rilis terbaru", b"cin:list:release:1"), Button.inline("⭐ Rating tertinggi", b"cin:list:top:1")],
        [Button.inline("📺 Serial terbaru", b"cin:list:series:1"), Button.inline("📈 Serial populer", b"cin:list:popseries:1")],
        [Button.inline("🎭 Genre", b"cin:lookup:genre"), Button.inline("🔎 Cara mencari", b"cin:search")],
        [Button.inline("🔄 Refresh", b"cin:home")]
    ]

def cinema_home_text():
    return "🎬 **LK21 CINEMA CENTER**\n━━━━━━━━━━━━━━━━━━\nJelajahi katalog film dan serial.\n\n`/cinema` - Beranda\n`/csearch judul` - Cari film/serial\n\nPilih kategori melalui tombol di bawah."

async def cinema_list(target, chat_id, kind, page=1, query=""):
    page = max(1, int(page))
    if kind in CINEMA_ENDPOINTS:
        endpoint, label = CINEMA_ENDPOINTS[kind]
        path = f"{endpoint}?{urllib.parse.urlencode({'page': page})}"
    elif kind == "search":
        label = f"Hasil pencarian: {query}"
        path = "search/" + urllib.parse.quote(query, safe="")
    elif kind in LOOKUP_PATHS:
        label = f"{kind.title()}: {query}"
        path = f"{LOOKUP_PATHS[kind]}/{urllib.parse.quote(query, safe='')}?page={page}"
    else: raise CinemaError("Kategori tidak dikenal.")

    payload = await cinema_get(path)
    items = cinema_items(payload)
    CINEMA_ITEMS[chat_id] = items[:CINEMA_PAGE_SIZE]
    CINEMA_BASE[chat_id] = len(CINEMA_ITEMS[chat_id])
    CINEMA_VIEW[chat_id] = {"kind": kind, "page": page, "query": query, "label": label}

    if not items: return await cinema_edit(target, f"🎬 **{label}**\n\nTidak ada hasil.", [[Button.inline("🏠 Cinema", b"cin:home")]])

    lines = [f"🎬 **{label}**", f"Halaman {page}", "━━━━━━━━━━━━━━━━━━"]
    rows = []
    for i, item in enumerate(CINEMA_ITEMS[chat_id]):
        title = cinema_title(item)
        year = cinema_text(cinema_pick(item, "year", "release_year") or "", 12)
        rating = cinema_text(cinema_pick(item, "rating", "imdb_rating") or "", 12)
        meta = " • ".join(x for x in (year, f"★ {rating}" if rating else "") if x)
        lines.append(f"**{i + 1}. {title}**" + (f"\n   {meta}" if meta else ""))
        rows.append([Button.inline(f"{i + 1}. {title[:35]}", f"cin:detail:{i}".encode())])

    nav = []
    if page > 1: nav.append(Button.inline("◀ Sebelumnya", f"cin:page:{page - 1}".encode()))
    if kind != "search" and len(items) >= CINEMA_PAGE_SIZE: nav.append(Button.inline("Berikutnya ▶", f"cin:page:{page + 1}".encode()))
    if nav: rows.append(nav)
    rows.append([Button.inline("🏠 Cinema", b"cin:home")])
    lines.append("\nPilih judul untuk melihat detail.")
    return await cinema_edit(target, "\n".join(lines), rows)

async def cinema_lookup(target, chat_id, kind):
    payload = await cinema_get(LOOKUP_PATHS[kind])
    options = payload if isinstance(payload, list) else cinema_items(payload)
    if not options: raise CinemaError(f"Daftar {kind} kosong.")
    CINEMA_LOOKUPS.setdefault(chat_id, {})[kind] = options
    rows = []
    for i, item in enumerate(options[:60]):
        name = cinema_pick(item, "name", "title", "value", "year") if isinstance(item, dict) else item
        rows.append([Button.inline(cinema_text(name or f"Pilihan {i+1}", 30), f"cin:filter:{kind}:{i}".encode())])
    rows.append([Button.inline("🏠 Cinema", b"cin:home")])
    return await cinema_edit(target, f"**Pilih {kind}**\n\nTersedia {len(options)} pilihan.", rows)

# ---- Pencari link stream film ----
PLAY_KEYS = ("stream", "player", "embed", "watch", "iframe", "video", "server")
MEDIA_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>\\]*)?", re.I)
IFRAME_RE = re.compile(r"""<iframe[^>]+(?:src|data-src)=["']([^"']+)["']""", re.I)
DATA_URL_RE = re.compile(r"""data-(?:url|src|iframe|embed)=["']([^"']+)["']""", re.I)

def uniq(seq):
    out = []
    for x in seq:
        if x and x not in out: out.append(x)
    return out

def collect_play_urls(obj, key="", out=None, depth=0):
    if out is None: out = []
    if depth > 4 or len(out) >= 8: return out
    if isinstance(obj, str):
        if obj.startswith("http") and "trailer" not in key and any(x in key for x in PLAY_KEYS) and obj.strip() not in out:
            out.append(obj.strip())
    elif isinstance(obj, dict):
        for k, v in obj.items(): collect_play_urls(v, f"{key}.{str(k).lower()}", out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:12]: collect_play_urls(v, key, out, depth + 1)
    return out

def _http_text(url, referer=None, limit=2_000_000):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer: h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15) as r:
        return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", errors="replace")

def _unesc(text): return html.unescape(text).replace("\\/", "/")

def _scrape_stream(page_url, depth=2, seen=None):
    seen = seen if seen is not None else set()
    if page_url in seen or len(seen) > 8: return None
    seen.add(page_url)
    try: text = _unesc(_http_text(page_url, referer=page_url))
    except Exception: return None
    media = MEDIA_RE.findall(text)
    if media:
        m3 = [m for m in media if ".m3u8" in m.lower()]
        return (m3 or media)[0], page_url
    if depth <= 0: return None
    for c in IFRAME_RE.findall(text) + DATA_URL_RE.findall(text):
        link = urllib.parse.urljoin(page_url, c.strip())
        if not link.startswith("http"): continue
        res = _scrape_stream(link, depth - 1, seen)
        if res: return res
    return None

def _resolve_lk21_sync(pages):
    errs = []
    for page in pages:
        if DIRECT_STREAM_RE.match(page): return page, {"User-Agent": UA}, None
        song, e = extract_stream(page, video=True)
        if song and song.get("url"):
            hdr = song.get("headers") or {"User-Agent": UA, "Referer": page}
            return song["url"], hdr, None
        if e: errs.append(cinema_text(e, 80))
        res = _scrape_stream(page)
        if res: return res[0], {"User-Agent": UA, "Referer": res[1]}, None
    return None, None, ("; ".join(errs)[:200] or "Link video tidak ditemukan di halaman/player.")

async def resolve_lk21_stream(item):
    pages = item.get("pages") or ([item["page"]] if item.get("page") else [])
    if not pages: return None, None, "URL halaman tidak ditemukan."
    try: return await asyncio.wait_for(asyncio.to_thread(_resolve_lk21_sync, pages), 70)
    except asyncio.TimeoutError: return None, None, "Timeout saat mengambil link video."
    except Exception as e: return None, None, str(e)[:200]

def make_play_item(src, title, fallback_url, poster):
    pages = uniq(collect_play_urls(src) + ([fallback_url] if fallback_url else []))
    if not pages: return None
    return {"pages": pages, "page": pages[0], "title": title, "video": True, "thumb": poster}

async def cinema_detail(target, chat_id, index):
    items = CINEMA_ITEMS.get(chat_id, [])
    base = CINEMA_BASE.get(chat_id, len(items))
    if not (0 <= index < base): raise CinemaError("Daftar kedaluwarsa.")
    del items[base:]
    item = items[index]
    kind = CINEMA_VIEW.get(chat_id, {}).get("kind", "latest")
    is_series = "series" in kind or "series" in str(cinema_pick(item, "type", "kind", "media_type") or "").lower()
    ident = cinema_id(item)
    if not ident: raise CinemaError("ID detail tidak ditemukan.")

    detail = await cinema_get(f"{'series' if is_series else 'movies'}/{urllib.parse.quote(ident, safe='')}")
    if isinstance(detail, dict) and isinstance(detail.get("data"), dict): detail = detail["data"]
    if not isinstance(detail, dict): detail = item

    title = cinema_title(detail)
    synopsis = cinema_text(cinema_pick(detail, "synopsis", "description", "overview", "plot") or "Sinopsis belum tersedia.", 1000)
    lines = [f"🎬 **{title}**", "━━━━━━━━━━━━━━━━━━"]
    for lbl, keys in (("Tahun", ("year", "release_year")), ("Genre", ("genre", "genres")), ("Negara", ("country",)), ("Durasi", ("duration",)), ("Rating", ("rating",))):
        v = cinema_pick(detail, *keys)
        if v: lines.append(f"**{lbl}:** {cinema_text(', '.join(cinema_text(x) for x in v[:3]) if isinstance(v, list) else v, 120)}")
    lines.extend(["", f"**Sinopsis**\n{synopsis}"])

    poster = cinema_pick(detail, "poster", "poster_url", "thumbnail", "image")
    poster = poster if isinstance(poster, str) and poster.startswith("http") else None
    buttons = []
    page_url = cinema_url(detail) or cinema_url(item)

    if is_series:
        episodes = []
        raw_eps = cinema_pick(detail, "episodes", "episode", "list_episode")
        if isinstance(raw_eps, list):
            episodes = raw_eps
        elif isinstance(detail.get("seasons"), list):
            for season in detail["seasons"]:
                if isinstance(season, dict) and isinstance(season.get("episodes"), list):
                    episodes.extend(season["episodes"])
        episodes = [e for e in episodes if isinstance(e, dict)]

        if episodes:
            lines.append("\n🎬 **Pilih Episode:**")
            for ep_idx, ep in enumerate(episodes[:20]):
                ep_title = cinema_text(cinema_pick(ep, "title", "name", "episode") or f"Episode {ep_idx + 1}", 20)
                play = make_play_item(ep, f"{title} - {ep_title}", cinema_url(ep), poster)
                if play:
                    items.append(play)
                    buttons.append(Button.inline(f"▶ {ep_title}", f"cin:play:{len(items) - 1}".encode()))
                else:
                    buttons.append(Button.inline(f"❌ {ep_title} (No Link)", b"cin:no_link"))
        else:
            lines.append("\n*(Daftar episode tidak tersedia dari API)*")
            play = make_play_item(detail, title, page_url, poster)
            if play:
                items.append(play)
                buttons.append(Button.inline("▶ Coba putar", f"cin:play:{len(items) - 1}".encode()))
        if page_url: buttons.append(Button.url("🌐 Buka di Web", page_url))
    else:
        play = make_play_item(detail, title, page_url, poster)
        if play:
            items.append(play)
            buttons.append(Button.inline("▶ Putar di VC", f"cin:play:{len(items) - 1}".encode()))
        if page_url: buttons.append(Button.url("🌐 Buka di Web", page_url))

    if poster: buttons.append(Button.url("🖼 Poster", poster))
    buttons.append(Button.inline("⬅️ Kembali", b"cin:back"))
    buttons.append(Button.inline("🏠 Cinema", b"cin:home"))

    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return await cinema_edit(target, "\n".join(lines), keyboard)

async def cinema_play(event, chat_id, index):
    if not is_admin(event.sender_id): return await event.answer("⛔ Playback khusus admin.", alert=True)
    err = need_vc()
    if err: return await event.answer(toast(err), alert=True)
    items = CINEMA_ITEMS.get(chat_id, [])
    if not (0 <= index < len(items)) or "pages" not in items[index]:
        return await event.answer("Daftar kedaluwarsa, buka detail lagi.", alert=True)
    item = items[index]
    await event.answer("⏳ Mengambil link video... tunggu sekitar 15 detik")

    stream_url, headers, err = await resolve_lk21_stream(item)
    if not stream_url:
        return await manager_bot.send_message(
            chat_id, f"❌ Gagal ambil link video.\n{clean(err)}\n\nCoba buka lewat web, atau putar link embed/m3u8 langsung pakai /vplayurl.")

    dur = await probe_duration(stream_url, headers)
    song = {
        "url": stream_url, "title": item.get("title") or "LK21 Stream", "duration": dur,
        "source": "LK21", "video": True, "page": item.get("page"), "thumb": item.get("thumb"),
        "headers": headers, "fmt": "m3u8" if ".m3u8" in stream_url.lower() else "mp4",
    }
    text = await do_enqueue(song)
    await manager_bot.send_message(chat_id, text, buttons=ctrl_buttons())

async def cinema_callback(event):
    data = event.data.decode("utf-8", "ignore")
    chat_id = event.chat_id
    try:
        if data.startswith("cin:play:"):
            return await cinema_play(event, chat_id, int(data.split(":")[2]))
        await event.answer()
        if data == "cin:no_link": return
        if data == "cin:home": return await cinema_edit(event, cinema_home_text(), cinema_home_buttons())
        if data == "cin:search": return await cinema_edit(event, "🔎 Gunakan `/csearch judul`", [[Button.inline("🏠 Cinema", b"cin:home")]])
        if data.startswith("cin:list:"):
            _, _, kind, page = data.split(":", 3)
            return await cinema_list(event, chat_id, kind, int(page))
        if data.startswith("cin:page:"):
            view = CINEMA_VIEW.get(chat_id, {})
            return await cinema_list(event, chat_id, view.get("kind", "latest"), int(data.split(":")[2]), view.get("query", ""))
        if data.startswith("cin:lookup:"): return await cinema_lookup(event, chat_id, data.split(":")[2])
        if data.startswith("cin:filter:"):
            _, _, kind, index = data.split(":", 3)
            opts = CINEMA_LOOKUPS.get(chat_id, {}).get(kind, [])
            if not (0 <= int(index) < len(opts)): raise CinemaError("Filter kedaluwarsa.")
            o = opts[int(index)]
            val = cinema_pick(o, "slug", "id", "name", "title", "value", "year") if isinstance(o, dict) else o
            return await cinema_list(event, chat_id, kind, 1, str(val))
        if data.startswith("cin:detail:"): return await cinema_detail(event, chat_id, int(data.split(":")[2]))
        if data == "cin:back":
            v = CINEMA_VIEW.get(chat_id, {})
            if v.get("kind") in (None, "home"): return await cinema_edit(event, cinema_home_text(), cinema_home_buttons())
            return await cinema_list(event, chat_id, v.get("kind", "latest"), int(v.get("page", 1)), v.get("query", ""))
    except Exception as e:
        try: await event.edit(f"⚠️ Cinema: {cinema_text(e, 250)}", buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])
        except Exception: pass

# ==========================================
# PLAYER
# ==========================================
def cancel_timer(chat_id):
    t = timers.pop(chat_id, None)
    if t: t.cancel()

async def auto_next(chat_id, delay):
    try:
        await asyncio.sleep(delay)
        timers.pop(chat_id, None)
        cur = current_songs.get(chat_id)
        if LOOP and cur and cur["title"] != "Silent": get_queue(chat_id).insert(0, dict(cur))
        await play_next(chat_id)
    except asyncio.CancelledError: pass

def schedule_next(chat_id, song, delay):
    cancel_timer(chat_id)
    song["t0"] = time.time()
    song["left"] = delay
    timers[chat_id] = asyncio.create_task(auto_next(chat_id, delay))

def elapsed(song, chat_id):
    dur = song.get("duration") or 0
    if not dur or "left" not in song: return 0
    left = song["left"]
    if chat_id in timers and "t0" in song: left -= time.time() - song["t0"]
    return int(min(dur, max(0, dur + 2 - left)))

async def play_next(chat_id):
    cancel_timer(chat_id)
    queue = get_queue(chat_id)
    if not queue:
        current_songs[chat_id] = silent_song()
        try: await userbot_call.play(chat_id, audio_stream(SILENT_AUDIO))
        except Exception as e: print(f"Error silent: {e}")
        return
    song = queue.pop(0)
    prev = current_songs.get(chat_id)
    was_idle = (not prev) or prev.get("title") == "Silent"
    current_songs[chat_id] = song
    try:
        await userbot_call.play(chat_id, make_stream(song))
        if was_idle and not song.get("video") and FIRST_PLAY_RETRY:
            await asyncio.sleep(2)
            await userbot_call.play(chat_id, make_stream(song))
        try: await call_any(["unmute", "unmute_stream"], chat_id)
        except Exception: pass
        print(f"▶ Memutar: {song['title']} | {song.get('source')} {song.get('fmt')}")
        if song.get("duration"): schedule_next(chat_id, song, song["duration"] + 2)
    except Exception as e:
        print(f"Error playing: {e}")
        PLAY_ERRORS[chat_id] = str(e)[:150]
        await play_next(chat_id)

async def do_enqueue(song, now=False):
    chat_id = TARGET_VC_ID
    q = get_queue(chat_id)
    cur = current_songs.get(chat_id)
    idle = (not cur) or cur["title"] == "Silent"
    tag = f" _(via {song.get('source', '?')}, {song.get('fmt', '?')})_"
    if song.get("video"): tag = " 🎬" + tag
    if now or idle:
        PLAY_ERRORS.pop(chat_id, None)
        q.insert(0, song)
        await play_next(chat_id)
        perr = PLAY_ERRORS.pop(chat_id, None)
        if perr: return f"❌ Gagal memutar {clean(song['title'])}: {clean(perr)}"
        return f"▶ **Memutar:** {clean(song['title'])}{tag}"
    q.append(song)
    return f"➕ **Antrean #{len(q)}:** {clean(song['title'])}{tag}"

# ==========================================
# AKSI KONTROL
# ==========================================
NO_USERBOT = "❌ Userbot belum online."
NO_VC = "❌ VC belum diset. Owner: /setvc @usernamegrup."
def need_vc(): return NO_USERBOT if not userbot_call else (NO_VC if not TARGET_VC_ID else None)

async def act_pause():
    e = need_vc()
    if e: return e
    try:
        await call_any(["pause", "pause_stream"], TARGET_VC_ID)
        song = current_songs.get(TARGET_VC_ID)
        if song and TARGET_VC_ID in timers and "t0" in song:
            song["left"] = max(1, song["left"] - (time.time() - song["t0"]))
            cancel_timer(TARGET_VC_ID)
        return "⏸ Dijeda."
    except Exception as ex: return f"❌ Gagal: {ex}"

async def act_resume():
    e = need_vc()
    if e: return e
    try:
        await call_any(["resume", "resume_stream"], TARGET_VC_ID)
        song = current_songs.get(TARGET_VC_ID)
        if song and song.get("duration") and "left" in song and TARGET_VC_ID not in timers: schedule_next(TARGET_VC_ID, song, song["left"])
        return "▶ Dilanjutkan."
    except Exception as ex: return f"❌ Gagal: {ex}"

async def act_skip():
    e = need_vc()
    if e: return e
    await play_next(TARGET_VC_ID)
    return "⏭ Di-skip."

async def act_stop():
    e = need_vc()
    if e: return e
    cancel_timer(TARGET_VC_ID)
    queues[TARGET_VC_ID] = []
    current_songs[TARGET_VC_ID] = silent_song()
    try: await userbot_call.play(TARGET_VC_ID, audio_stream(SILENT_AUDIO))
    except Exception: pass
    return "⏹ Dihentikan (tetap di VC)."

async def act_loop():
    global LOOP
    LOOP = not LOOP
    await save_data()
    return f"🔁 Loop {'ON' if LOOP else 'OFF'}."

async def act_filter():
    global FILTER_ON
    FILTER_ON = not FILTER_ON
    await save_data()
    return f"🧹 Filter {'ON' if FILTER_ON else 'OFF'}."

async def act_hemat(arg=""):
    global VIDEO_HEIGHT, HEMAT_ON, PREV_HEIGHT
    if HEMAT_ON:
        HEMAT_ON = False; VIDEO_HEIGHT = PREV_HEIGHT; await save_data()
        return f"💡 Hemat OFF. Kualitas kembali {quality_label()}."
    target = int(arg) if arg in ("144", "240", "360") else HEMAT_HEIGHT
    PREV_HEIGHT = VIDEO_HEIGHT; VIDEO_HEIGHT = target; HEMAT_ON = True; await save_data()
    return f"💡 Hemat ON ({target}p)."

async def act_quality(arg):
    global VIDEO_HEIGHT, HEMAT_ON
    arg = (arg or "").strip().lower()
    if arg not in ("audio", "144", "240", "360", "480", "720"):
        return f"📺 Kualitas sekarang: {quality_label()}\nUbah: /quality 144-720 atau audio."
    VIDEO_HEIGHT = 0 if arg == "audio" else int(arg); HEMAT_ON = False; await save_data()
    return f"📺 Kualitas diset {quality_label()}."

async def act_remove(n):
    e = need_vc()
    if e: return e
    q = get_queue(TARGET_VC_ID)
    if not (1 <= n <= len(q)): return "❌ Nomor tidak ada di antrean."
    return f"🗑 Dihapus: {clean(q.pop(n - 1)['title'])}"

async def act_clear():
    e = need_vc()
    if e: return e
    queues[TARGET_VC_ID] = []
    return "🧹 Antrean dikosongkan."

async def act_shuffle():
    e = need_vc()
    if e: return e
    random.shuffle(get_queue(TARGET_VC_ID))
    return "🔀 Antrean diacak."

# ==========================================
# TAMPILAN
# ==========================================
def bar(pos, total, n=10):
    if not total: return "▱" * n
    k = int(n * pos / total)
    return "▰" * k + "▱" * (n - k)

def panel_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    now = clean(cur["title"]) if cur and cur["title"] != "Silent" else "—"
    return (f"🎵 **Music Panel**\n\nUserbot: {'🟢 Online' if userbot_call else '🔴 Offline'}\nVC: {TARGET_VC_ID or 'belum diset'}\n"
            f"Sedang main: {now}\nAntrean: {len(get_queue(TARGET_VC_ID)) if TARGET_VC_ID else 0} lagu\n\n"
            f"🧹 Filter: {'ON' if FILTER_ON else 'OFF'}   🔁 Loop: {'ON' if LOOP else 'OFF'}\n"
            f"📺 Video: {quality_label()}{' 💡hemat' if HEMAT_ON else ''}\n\n"
            "Cari lagu: /play judul\nNonton: /vplay judul atau /vplay link\nCinema: /cinema")

def ctrl_buttons():
    return [
        [Button.inline("⏸", b"pause"), Button.inline("▶", b"resume"), Button.inline("⏭", b"skip"), Button.inline("⏹", b"stop")],
        [Button.inline("🎶 Now", b"np"), Button.inline("📜 Antrean", b"queue"), Button.inline("🔁 Loop", b"loop")],
    ]

def panel_buttons(uid):
    return ctrl_buttons() + [
        [Button.inline("🧹 Filter", b"filter"), Button.inline("💡 Hemat", b"hemat"), Button.inline("🔄 Refresh", b"refresh")],
        [Button.inline("🎬 Cinema Center", b"cin:home")],
        [Button.inline("👥 Admin", b"admins")],
    ]

def queue_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    now = (f"🎶 **Sedang diputar:** {clean(cur['title'])}\n\n" if cur and cur["title"] != "Silent" else "")
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    if not q: return now + "📜 **Antrean kosong.**"
    lines = [f"{i + 1}. {clean(s['title'])}" for i, s in enumerate(q[:15])]
    more = f"\n… dan {len(q) - 15} lagi" if len(q) > 15 else ""
    return now + "📜 **Antrean:**\n" + "\n".join(lines) + more

def queue_buttons():
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    rows = []
    nums = [Button.inline(f"🗑{i + 1}", f"rm:{i + 1}".encode()) for i in range(min(len(q), 10))]
    for i in range(0, len(nums), 5): rows.append(nums[i:i + 5])
    rows.append([Button.inline("🔀 Acak", b"shuf"), Button.inline("🧹 Kosongkan", b"clr"), Button.inline("⏭ Skip", b"skip")])
    return rows

def admins_text():
    lines = [f"👑 Owner: {OWNER_ID}"] + [f"• {clean(n)} ({u})" for u, n in DATA["admins"].items()]
    if len(lines) == 1: lines.append("(belum ada admin tambahan)")
    lines.append("\nTambah: /addadmin ID atau minta orangnya kirim /start ke bot ini.")
    return "👥 **Daftar Admin**\n\n" + "\n".join(lines)

def admins_buttons(uid):
    if not is_owner(uid): return None
    return [[Button.inline(f"🗑 {n[:20]}", f"deladm:{u}".encode())] for u, n in list(DATA["admins"].items())[:10]] or None

async def send_np(chat_id):
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    if not cur or cur["title"] == "Silent": return await manager_bot.send_message(chat_id, "🎶 Tidak ada yang diputar.", buttons=ctrl_buttons())
    dur = cur.get("duration") or 0
    pos = elapsed(cur, TARGET_VC_ID)
    cap = (f"🎶 **Sedang diputar**\n**{clean(cur['title'])}**\n{clean(cur.get('channel', ''))} • {cur.get('source', '?')}\n\n"
           f"{bar(pos, dur)}  {fmt_dur(pos)} / {fmt_dur(dur)}\n🔁 Loop {'ON' if LOOP else 'OFF'}")
    if cur.get("thumb"):
        try: return await manager_bot.send_file(chat_id, cur["thumb"], caption=cap, buttons=ctrl_buttons())
        except Exception: pass
    return await manager_bot.send_message(chat_id, cap, buttons=ctrl_buttons())

async def clear_cards(chat_id):
    st = PENDING.pop(chat_id, None)
    if st and st.get("mids"):
        try: await manager_bot.delete_messages(chat_id, st["mids"])
        except Exception: pass

async def show_results(chat_id, query, results, video):
    await clear_cards(chat_id)
    mids = [(await manager_bot.send_message(chat_id, f"{'🎬' if video else '🔎'} **Hasil untuk:** {clean(query)}")).id]
    for i, r in enumerate(results):
        ch = f"{clean(r['channel'])} • " if r.get("channel") else ""
        cap = f"**{i + 1}. {clean(r['title'])}**\n{ch}{fmt_dur(r['duration'])} • {r['source']}"
        btns = [[Button.inline("▶ Putar", f"pick:{i}".encode()), Button.inline("➕ Antre", f"add:{i}".encode())]]
        msg = None
        if r.get("thumb"):
            try: msg = await manager_bot.send_file(chat_id, r["thumb"], caption=cap, buttons=btns)
            except Exception: pass
        if not msg: msg = await manager_bot.send_message(chat_id, cap, buttons=btns)
        mids.append(msg.id)
    mids.append((await manager_bot.send_message(chat_id, "Pilih salah satu 👆", buttons=[[Button.inline("❌ Tutup", b"close")]])).id)
    PENDING[chat_id] = {"results": results, "mids": mids}

# ==========================================
# USERBOT: NYALA / MATI
# ==========================================
async def resolve_chat(app, raw):
    raw = raw.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w-]+)", raw)
    if m:
        try: return utils.get_peer_id((await app(ImportChatInviteRequest(m.group(1)))).chats[0])
        except UserAlreadyParticipantError: return utils.get_peer_id((await app(CheckChatInviteRequest(m.group(1)))).chat)
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z]\w{3,})", raw)
    uname = m.group(1) if m else (raw[1:] if raw.startswith("@") else None)
    if uname: return utils.get_peer_id(await app.get_entity(uname))
    chat_id = int(raw)
    try: await app.get_entity(chat_id)
    except Exception:
        await app.get_dialogs()
        await app.get_entity(chat_id)
    return chat_id

async def shutdown_userbot():
    global userbot_app, userbot_call, TARGET_VC_ID
    for cid in list(current_songs.keys()):
        cancel_timer(cid)
        try:
            if userbot_call: await call_any(["leave_call", "leave_group_call"], cid)
        except Exception: pass
    current_songs.clear(); queues.clear(); PENDING.clear(); TARGET_VC_ID = None
    if userbot_app:
        try: await userbot_app.disconnect()
        except Exception: pass
    userbot_app = None; userbot_call = None

async def boot_userbot(session):
    global userbot_app, userbot_call
    await shutdown_userbot()
    app = TelegramClient(session, API_ID, API_HASH)
    await app.connect()
    if not await app.is_user_authorized():
        await app.disconnect()
        raise RuntimeError("Session tidak valid.")
    await app.get_dialogs()
    call = PyTgCalls(app)
    await call.start()
    userbot_app, userbot_call = app, call
    await load_data()
    if DATA_DIRTY: await save_data()

# ==========================================
# PERINTAH
# ==========================================
def arg_of(event):
    parts = (event.raw_text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""

async def deny(event):
    await event.reply(f"⛔ **Kamu belum jadi admin.**\nID kamu: {event.sender_id}",
                      buttons=[[Button.inline("🙋 Minta akses ke owner", b"req")]])

def cmd(pattern, owner=False):
    def deco(fn):
        async def wrapper(event):
            uid = event.sender_id
            if is_owner(uid) or (not owner and is_admin(uid)): return await fn(event)
            if is_admin(uid): return await event.reply("⛔ Perintah ini khusus owner.")
            await deny(event)
        manager_bot.on(events.NewMessage(pattern=pattern, func=lambda e: e.is_private))(wrapper)
        return wrapper
    return deco

@manager_bot.on(events.NewMessage(pattern=r"^/cinema(?:\s|$)", func=lambda e: e.is_private))
async def cinema_command(event):
    CINEMA_VIEW[event.chat_id] = {"kind": "home", "page": 1}
    await event.reply(cinema_home_text(), buttons=cinema_home_buttons(), link_preview=False)

@manager_bot.on(events.NewMessage(pattern=r"^/csearch(?:\s|$)", func=lambda e: e.is_private))
async def cinema_search_command(event):
    parts = (event.raw_text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return await event.reply("Cara mencari: `/csearch judul film`", buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])
    status = await event.reply("🔎 Mencari katalog...")
    try: await cinema_list(status, event.chat_id, "search", 1, parts[1].strip())
    except Exception as e: await status.edit(f"⚠️ Error: {cinema_text(e, 250)}", buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])

@cmd(r"^/(start|panel|menu)(?:\s|$)")
async def c_start(event): await event.reply(panel_text(), buttons=panel_buttons(event.sender_id))

@cmd(r"^/help(?:\s|$)")
async def c_help(event):
    await event.reply("📖 **Perintah**\n\n/play judul\n/fast judul\n/vplay judul|link\n/playurl url\n/vplayurl url\n/np /queue /pause /resume /skip /stop\n/loop /shuffle /clear /remove N\n/filter /hemat /quality\n/cinema /csearch\n/admins /status\n\n**Owner:** /setvc @grup /unsetvc /addadmin ID /deladmin ID /getsession")

@cmd(r"^/setvc(?:\s|$)", owner=True)
async def c_setvc(event):
    global TARGET_VC_ID
    if not userbot_app: return await event.reply(NO_USERBOT)
    raw = arg_of(event)
    if not raw: return await event.reply("**Usage:** /setvc @usernamegrup atau link.")
    m = await event.reply("⏳ Mencoba masuk VC...")
    try:
        chat_id = await resolve_chat(userbot_app, raw)
        if TARGET_VC_ID and TARGET_VC_ID != chat_id:
            try: await call_any(["leave_call", "leave_group_call"], TARGET_VC_ID)
            except Exception: pass
        await userbot_call.play(chat_id, audio_stream(SILENT_AUDIO), config=GroupCallConfig(auto_start=True))
        TARGET_VC_ID = chat_id
        current_songs[chat_id] = silent_song()
        await m.edit(f"✅ **Target VC diset!**\nID: {chat_id}", buttons=[[Button.inline("🎛 Panel", b"refresh")]])
    except Exception as e: await m.edit(f"❌ Gagal masuk: {e}")

@cmd(r"^/unsetvc(?:\s|$)", owner=True)
async def c_unsetvc(event):
    global TARGET_VC_ID
    if TARGET_VC_ID:
        cancel_timer(TARGET_VC_ID)
        try: await call_any(["leave_call", "leave_group_call"], TARGET_VC_ID)
        except Exception: pass
        queues[TARGET_VC_ID] = []; current_songs.pop(TARGET_VC_ID, None)
    TARGET_VC_ID = None
    await event.reply("✅ Target VC dilepas.")

@cmd(r"^/play(?:\s|$)")
async def c_play(event):
    err = need_vc()
    if err: return await event.reply(err)
    q = arg_of(event)
    if not q: return await event.reply("**Usage:** /play judul atau link")
    m = await event.reply("🔎 Mencari...")
    if q.startswith("http"):
        song, e = await asyncio.to_thread(resolve_song, {"page": q, "title": q, "source": detect_source(q)})
        if not song: return await m.edit(f"❌ Gagal: {clean(e)}")
        return await m.edit(await do_enqueue(song), buttons=ctrl_buttons())
    results, e = await asyncio.to_thread(search_candidates, q)
    if not results: return await m.edit(f"❌ {clean(e)}")
    await m.delete(); await show_results(event.chat_id, q, results, False)

@cmd(r"^/fast(?:\s|$)")
async def c_fast(event):
    err = need_vc()
    if err: return await event.reply(err)
    q = arg_of(event)
    if not q: return await event.reply("**Usage:** /fast judul")
    m = await event.reply("🔎 Mencari...")
    results, e = await asyncio.to_thread(search_candidates, q)
    if not results: return await m.edit(f"❌ {clean(e)}")
    song, e = await asyncio.to_thread(resolve_song, results[0])
    if not song: return await m.edit(f"❌ Gagal: {clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())

@cmd(r"^/vplay(?:\s|$)")
async def c_vplay(event):
    err = need_vc()
    if err: return await event.reply(err)
    q = arg_of(event)
    if not q: return await event.reply("**Usage:** /vplay judul atau link")
    m = await event.reply("🔎 Mencari video...")
    if q.startswith("http"):
        song, e = await asyncio.to_thread(resolve_song, {"page": q, "title": q, "source": detect_source(q), "video": True})
        if not song: return await m.edit(f"❌ Gagal: {clean(e)}")
        return await m.edit(await do_enqueue(song), buttons=ctrl_buttons())
    try:
        results = await asyncio.to_thread(_flat_search, "ytsearch", "YouTube", q, MAX_RESULTS)
    except Exception as ex: return await m.edit(f"❌ Gagal: {clean(str(ex)[:200])}")
    if not results: return await m.edit("❌ Video tidak ditemukan.")
    for r in results: r["video"] = True
    await m.delete(); await show_results(event.chat_id, q, results, True)

@cmd(r"^/playurl(?:\s|$)")
async def c_playurl(event):
    err = need_vc()
    if err: return await event.reply(err)
    url = arg_of(event)
    if not url.startswith("http"): return await event.reply("**Usage:** /playurl https://...")
    m = await event.reply("🔎 Mengekstrak stream...")
    song, e = await asyncio.to_thread(resolve_song, {"page": url, "title": url, "source": detect_source(url)})
    if not song: return await m.edit(f"❌ Gagal: {clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())

@cmd(r"^/vplayurl(?:\s|$)")
async def c_vplayurl(event):
    err = need_vc()
    if err: return await event.reply(err)
    url = arg_of(event)
    if not url.startswith("http"): return await event.reply("**Usage:** /vplayurl https://...")
    m = await event.reply("🔎 Mengekstrak video...")
    song, e = await asyncio.to_thread(resolve_song, {"page": url, "title": url, "source": detect_source(url), "video": True})
    if not song: return await m.edit(f"❌ Gagal: {clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())

@cmd(r"^/np(?:\s|$)")
async def c_np(event): await send_np(event.chat_id)

@cmd(r"^/queue(?:\s|$)")
async def c_queue(event): await event.reply(queue_text(), buttons=queue_buttons())

@cmd(r"^/pause(?:\s|$)")
async def c_pause(event): await event.reply(await act_pause(), buttons=ctrl_buttons())

@cmd(r"^/resume(?:\s|$)")
async def c_resume(event): await event.reply(await act_resume(), buttons=ctrl_buttons())

@cmd(r"^/skip(?:\s|$)")
async def c_skip(event): await event.reply(await act_skip(), buttons=ctrl_buttons())

@cmd(r"^/stop(?:\s|$)")
async def c_stop(event): await event.reply(await act_stop())

@cmd(r"^/loop(?:\s|$)")
async def c_loop(event): await event.reply(await act_loop())

@cmd(r"^/shuffle(?:\s|$)")
async def c_shuffle(event): await event.reply(await act_shuffle())

@cmd(r"^/clear(?:\s|$)")
async def c_clear(event): await event.reply(await act_clear())

@cmd(r"^/remove(?:\s|$)")
async def c_remove(event):
    a = arg_of(event)
    if not a.isdigit(): return await event.reply("**Usage:** /remove 2")
    await event.reply(await act_remove(int(a)))

@cmd(r"^/filter(?:\s|$)")
async def c_filter(event): await event.reply(await act_filter())

@cmd(r"^/hemat(?:\s|$)")
async def c_hemat(event): await event.reply(await act_hemat(arg_of(event)))

@cmd(r"^/quality(?:\s|$)")
async def c_quality(event): await event.reply(await act_quality(arg_of(event)))

@cmd(r"^/admins(?:\s|$)")
async def c_admins(event): await event.reply(admins_text(), buttons=admins_buttons(event.sender_id))

@cmd(r"^/addadmin(?:\s|$)", owner=True)
async def c_addadmin(event):
    a = arg_of(event)
    if not a: return await event.reply("**Usage:** /addadmin 123456789")
    try:
        if a.lstrip("-").isdigit():
            uid = int(a); name = f"ID {uid}"
            try: name = utils.get_display_name(await manager_bot.get_entity(uid)) or name
            except Exception: pass
        else:
            ent = await manager_bot.get_entity(a); uid = ent.id; name = utils.get_display_name(ent) or a
    except Exception as e: return await event.reply(f"❌ Tidak ditemukan: {e}")
    if uid == OWNER_ID: return await event.reply("Itu owner.")
    DATA["admins"][str(uid)] = name; await save_data()
    await event.reply(f"✅ **{clean(name)}** sekarang admin.")
    try: await manager_bot.send_message(uid, "✅ Kamu sekarang admin Music Bot. Ketik /start.")
    except Exception: pass

@cmd(r"^/deladmin(?:\s|$)", owner=True)
async def c_deladmin(event):
    a = arg_of(event)
    if not a.lstrip("-").isdigit() or str(int(a)) not in DATA["admins"]: return await event.reply("**Usage:** /deladmin ID")
    name = DATA["admins"].pop(str(int(a))); await save_data()
    await event.reply(f"🗑 {clean(name)} dihapus.")

@cmd(r"^/getsession(?:\s|$)", owner=True)
async def c_getsession(event):
    if not userbot_app: return await event.reply(NO_USERBOT)
    s = StringSession.save(userbot_app.session)
    m = await event.reply(f"🔑 **SESSION_STRING**\n\n{s}\n\nPesan terhapus dalam 90 detik.")
    await asyncio.sleep(90)
    try: await m.delete()
    except Exception: pass

@cmd(r"^/status(?:\s|$)")
async def c_status(event):
    up = int(time.time() - START_TIME)
    await event.reply(f"📊 **Status**\nUptime: {up // 3600}j {up % 3600 // 60}m\nUserbot: {'online' if userbot_call else 'offline'}\nVC: {TARGET_VC_ID or 'belum diset'}\nAdmin: {len(DATA['admins'])} + owner\nCookies: {'ada' if os.path.exists(COOKIES) else 'tidak ada'}\nAPI film: {LK21_API_BASE}")

# ==========================================
# CALLBACKS
# ==========================================
def toast(text): return re.sub(r"[*_]", "", text)[:190]

async def refresh_panel(event):
    try: await event.edit(panel_text(), buttons=panel_buttons(event.sender_id))
    except Exception: pass

async def handle_pick(event, idx, now):
    st = PENDING.get(event.chat_id)
    if not st or not (0 <= idx < len(st["results"])): return await event.answer("Daftar kedaluwarsa.", alert=True)
    err = need_vc()
    if err: return await event.answer(toast(err), alert=True)
    await event.answer("⏳ Mengambil...")
    song, e = await asyncio.to_thread(resolve_song, st["results"][idx])
    if not song: return await manager_bot.send_message(event.chat_id, f"❌ Gagal: {clean(e)}")
    text = await do_enqueue(song, now=now)
    if now: await clear_cards(event.chat_id)
    await manager_bot.send_message(event.chat_id, text, buttons=ctrl_buttons())

@manager_bot.on(events.CallbackQuery())
async def on_callback(event):
    data = event.data.decode("utf-8", "ignore")
    uid = event.sender_id

    if data == "req":
        if is_admin(uid): return await event.answer("Kamu sudah admin.", alert=True)
        if uid in ASKED: return await event.answer("Tunggu owner.", alert=True)
        sender = await event.get_sender()
        name = utils.get_display_name(sender) or f"ID {uid}"
        ASKED.add(uid); REQUESTS[uid] = name
        try:
            await manager_bot.send_message(OWNER_ID, f"🙋 Minta admin\nNama: {clean(name)}\nID: {uid}",
                buttons=[[Button.inline("✅ Setujui", f"appr:{uid}".encode()), Button.inline("❌ Tolak", f"rej:{uid}".encode())]])
        except Exception as e:
            ASKED.discard(uid); return await event.answer(f"Gagal: {e}"[:190], alert=True)
        return await event.answer("✅ Dikirim ke owner.", alert=True)

    # Cinema: browsing terbuka untuk semua, playback dicek admin di dalamnya
    if data.startswith("cin:"): return await cinema_callback(event)

    if not is_admin(uid): return await event.answer("⛔ Kamu bukan admin.", alert=True)

    if data.startswith(("appr:", "rej:", "deladm:")):
        if not is_owner(uid): return await event.answer("⛔ Khusus owner.", alert=True)
        kind, target = data.split(":", 1); tid = int(target)
        if kind == "appr":
            name = REQUESTS.pop(tid, f"ID {tid}"); DATA["admins"][str(tid)] = name; ASKED.discard(tid)
            await save_data(); await event.edit(f"✅ {clean(name)} disetujui.")
            try: await manager_bot.send_message(tid, "✅ Kamu sekarang admin. Ketik /start.")
            except Exception: pass
        elif kind == "rej":
            REQUESTS.pop(tid, None); ASKED.discard(tid); await event.edit("❌ Ditolak.")
            try: await manager_bot.send_message(tid, "❌ Ditolak owner.")
            except Exception: pass
        else:
            name = DATA["admins"].pop(str(tid), None); await save_data()
            await event.answer(f"Dihapus: {name}" if name else "Tidak ada.")
            try: await event.edit(admins_text(), buttons=admins_buttons(uid))
            except Exception: pass
        return

    if data.startswith("pick:"): return await handle_pick(event, int(data[5:]), now=True)
    if data.startswith("add:"): return await handle_pick(event, int(data[4:]), now=False)
    if data == "close": await event.answer(); return await clear_cards(event.chat_id)

    simple = {"pause": act_pause, "resume": act_resume, "skip": act_skip, "stop": act_stop, "loop": act_loop}
    if data in simple: return await event.answer(toast(await simple[data]()))
    if data == "np": await event.answer(); return await send_np(event.chat_id)
    if data == "queue": await event.answer(); return await manager_bot.send_message(event.chat_id, queue_text(), buttons=queue_buttons())

    if data in ("shuf", "clr") or data.startswith("rm:"):
        text = await act_shuffle() if data == "shuf" else (await act_clear() if data == "clr" else await act_remove(int(data[3:])))
        await event.answer(toast(text))
        try: await event.edit(queue_text(), buttons=queue_buttons())
        except Exception: pass
        return

    if data == "filter": await event.answer(toast(await act_filter())); return await refresh_panel(event)
    if data == "hemat": await event.answer(toast(await act_hemat(""))); return await refresh_panel(event)
    if data == "refresh": await event.answer("🔄"); return await refresh_panel(event)
    if data == "admins": await event.answer(); return await manager_bot.send_message(event.chat_id, admins_text(), buttons=admins_buttons(uid))
    await event.answer()

# ==========================================
# UPLOAD SESSION & JALANKAN
# ==========================================
@manager_bot.on(events.NewMessage(func=lambda e: e.is_private and e.document))
async def on_document(event):
    if event.sender_id != OWNER_ID: return
    name = next((a.file_name for a in event.document.attributes if hasattr(a, "file_name")), "")
    if not name.lower().endswith(".zip"): return await event.reply("❌ Kirim file .zip berisi .session.")
    msg = await event.reply("⏳ Memproses file...")
    zip_path = await event.download_media()
    try:
        extract_dir = "/tmp/session_extract"
        if os.path.exists(extract_dir): shutil.rmtree(extract_dir)
        os.makedirs(extract_dir)
        with zipfile.ZipFile(zip_path, "r") as z: z.extractall(extract_dir)
        os.remove(zip_path)
        found = glob.glob(f"{extract_dir}/**/*.session", recursive=True)
        if not found: return await msg.edit("❌ File .session tidak ditemukan!")
        for f in glob.glob("/tmp/userbot_session.session*"): os.remove(f)
        shutil.move(found[0], "/tmp/userbot_session.session")
        await msg.edit("✅ Session ditemukan.\n⏳ Menyalakan userbot...")
        await boot_userbot("/tmp/userbot_session")
        await msg.edit("🚀 Userbot online!\n1. /setvc @usernamegrup\n2. /play judul", buttons=[[Button.inline("🎛 Panel", b"refresh")]])
    except Exception as e: await msg.edit(f"❌ Gagal:\n{e}")

async def notify_owner(text):
    try: await manager_bot.send_message(OWNER_ID, text)
    except Exception: pass

async def main():
    print("Memulai Manager Bot...")
    await manager_bot.start(bot_token=BOT_TOKEN)
    print("✅ Manager Bot siap!")
    if SESSION_STRING:
        try:
            await boot_userbot(StringSession(SESSION_STRING))
            await notify_owner("🚀 Bot restart. Userbot online otomatis.\n/start untuk panel, lalu /setvc.")
        except Exception as e: await notify_owner(f"❌ Auto-start gagal: {e}\nKirim zip session manual.")
    else:
        await notify_owner("🤖 Bot nyala. Kirim file zip session untuk menyalakan userbot.")
    await manager_bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot dihentikan.")
