import asyncio
import glob
import hashlib
import html as _html
import io
import json
import math
import os
import random
import re
import shutil
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telethon import TelegramClient, events, utils, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.sessions import MemorySession, StringSession
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, GroupCallConfig

# ==========================================
# KONFIGURASI
# Dibaca dari Secrets/env dulu. Kalau kosong, pakai nilai di sini.
# (Repo PUBLIC? Kosongkan nilai bawaan di bawah dan isi lewat Secrets!)
# ==========================================
def _env(name, default=""):
    return os.environ.get(name) or default


API_ID = int(_env("API_ID", "32333215"))
API_HASH = _env("API_HASH", "3d9a1c2ab47d6a7b6fff1b452ab22452")
BOT_TOKEN = _env("BOT_TOKEN", "9183135814:AAFpb7Q9MZwu46edK1ME7sL5FR690ydIDwg")
OWNER_ID = int(_env("OWNER_ID", "8962307609"))
SESSION_STRING = _env("SESSION_STRING")

SILENT_AUDIO = "https://github.com/anars/blank-audio/raw/master/1-second-of-silence.mp3"
COOKIES = "cookies.txt"

# ---- HASIL PENCARIAN ----
MAX_RESULTS = 15      # total hasil yang dicari
PAGE_SIZE = 5         # hasil per halaman

# ---- FILTER LAGU ----
FILTER_ON = True
MIN_DUR = 60
MAX_DUR = 600
BLOCKED_WORDS = [
    "reaction", "react", "karaoke", "tutorial", "review", "trailer",
    "interview", "podcast", "full album", "1 hour", "10 hours",
    "compilation", "playlist", "gameplay", "teaser",
]

# ---- VIDEO ----
VIDEO_HEIGHT = 480
HEMAT_HEIGHT = 360
HEMAT_ON = False
PREV_HEIGHT = 480
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
SAVE_DELAY = 20       # detik jeda sebelum menyimpan perubahan
PLAYLIST_MAX = 25     # maksimal lagu dari satu playlist
FAV_MAX = 30
HIST_MAX = 15
QUEUE_SAVE_MAX = 30
VOLUME = 100
AUTO_CLEAN = True     # hapus perintah & balasan singkat supaya chat tidak penuh

START_TIME = time.time()
manager_bot = TelegramClient(MemorySession(), API_ID, API_HASH)
manager_bot.parse_mode = "html"

# ==========================================
# STATE
# ==========================================
userbot_app = None
userbot_call = None
TARGET_VC_ID = None
queues = {}
current_songs = {}
timers = {}
PENDING = {}        # chat_id -> state hasil pencarian
PANELS = {}         # chat_id -> id pesan panel yang aktif
VIEW = {}           # chat_id -> tampilan panel aktif
REQUESTS = {}
ASKED = set()
LISTPAGE = {}      # (uid, jenis) -> halaman favorit/riwayat
SLEEP = {"task": None, "until": 0}
LAST_SAVED = b""
_save_task = None
WAIT = {}          # chat_id -> jenis input yang ditunggu (play/vplay/fast/setvc/addadmin)
NOTICE = {}        # chat_id -> catatan sekali tampil di panel
DATA = {"admins": {}, "fav": {}, "hist": [], "vc_raw": None, "queue": []}
DATA_DIRTY = False
DATA_TAG = "MUSICBOT_DATA "


def get_queue(chat_id):
    return queues.setdefault(chat_id, [])


def silent_song():
    return {"url": SILENT_AUDIO, "title": "Silent", "duration": 0}


def esc(text):
    return _html.escape(str(text if text is not None else ""), quote=False)


clean = esc


def cut(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_dur(sec):
    if not sec:
        return "?"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def quality_label():
    return "suara saja" if VIDEO_HEIGHT == 0 else f"{VIDEO_HEIGHT}p"


def onoff(v):
    return "ON" if v else "OFF"


def is_owner(uid):
    return uid == OWNER_ID


def is_admin(uid):
    return uid == OWNER_ID or str(uid) in DATA["admins"]


def toast(text):
    return _html.unescape(re.sub(r"<[^>]+>", "", str(text)))[:190]


# ==========================================
# PENYIMPANAN (Saved Messages akun userbot, tahan restart)
# ==========================================
def snapshot():
    if TARGET_VC_ID:
        cur = current_songs.get(TARGET_VC_ID)
        items = [cur] if cur and cur.get("title") != "Silent" else []
        items += get_queue(TARGET_VC_ID)
        DATA["queue"] = [i for i in (mk_item(x) for x in items) if i][:QUEUE_SAVE_MAX]
    return {
        "admins": DATA["admins"], "fav": DATA["fav"], "hist": DATA["hist"],
        "vc_raw": DATA["vc_raw"], "queue": DATA["queue"],
        "filter_on": FILTER_ON, "video_height": VIDEO_HEIGHT, "hemat_on": HEMAT_ON,
        "prev_height": PREV_HEIGHT, "loop": LOOP, "volume": VOLUME,
    }


async def _data_messages():
    msgs = await userbot_app.get_messages("me", search=DATA_TAG.strip(), limit=10)
    return [m for m in msgs if m.out and (m.raw_text or "").startswith(DATA_TAG)]


async def save_data():
    global DATA_DIRTY, LAST_SAVED
    DATA_DIRTY = True
    if not userbot_app:
        return
    payload = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
    if payload == LAST_SAVED:
        DATA_DIRTY = False
        return
    try:
        old = await _data_messages()
        bio = io.BytesIO(payload)
        bio.name = "musicbot_data.json"
        await userbot_app.send_file("me", bio, caption=DATA_TAG + "v2", force_document=True, parse_mode=None)
        if old:
            await userbot_app.delete_messages("me", [m.id for m in old])
        LAST_SAVED = payload
        DATA_DIRTY = False
    except Exception as e:
        print(f"Gagal simpan data: {e}")


async def _delayed_save():
    await asyncio.sleep(SAVE_DELAY)
    await save_data()


def schedule_save():
    """Simpan perubahan dengan jeda (tidak menyimpan tiap detik)."""
    global _save_task
    if _save_task and not _save_task.done():
        return
    try:
        _save_task = asyncio.get_running_loop().create_task(_delayed_save())
    except RuntimeError:
        pass


async def load_data():
    global FILTER_ON, VIDEO_HEIGHT, HEMAT_ON, PREV_HEIGHT, LOOP, VOLUME, LAST_SAVED
    if not userbot_app:
        return
    try:
        msgs = await _data_messages()
        if not msgs:
            return
        m = msgs[0]
        if getattr(m, "media", None):
            raw = await userbot_app.download_media(m, file=bytes)
            stored = json.loads(raw.decode("utf-8"))
            LAST_SAVED = raw
        else:
            stored = json.loads(m.raw_text[len(DATA_TAG):])
    except Exception as e:
        print(f"Gagal baca data: {e}")
        return
    DATA["admins"] = {**stored.get("admins", {}), **DATA["admins"]}
    fav = stored.get("fav", {})
    for uid, lst in DATA["fav"].items():
        have = {iid(x) for x in lst}
        fav[uid] = (lst + [x for x in fav.get(uid, []) if iid(x) not in have])[:FAV_MAX]
    DATA["fav"] = fav
    DATA["hist"] = stored.get("hist", [])
    DATA["vc_raw"] = stored.get("vc_raw")
    DATA["queue"] = stored.get("queue", [])
    if not DATA_DIRTY:
        FILTER_ON = stored.get("filter_on", FILTER_ON)
        VIDEO_HEIGHT = stored.get("video_height", VIDEO_HEIGHT)
        HEMAT_ON = stored.get("hemat_on", HEMAT_ON)
        PREV_HEIGHT = stored.get("prev_height", PREV_HEIGHT)
        LOOP = stored.get("loop", LOOP)
        VOLUME = stored.get("volume", VOLUME)


# ==========================================
# ITEM LAGU, RIWAYAT, FAVORIT, TIMER, VOLUME, PLAYLIST, PULIH OTOMATIS
# ==========================================
def iid(item):
    return hashlib.md5(str(item.get("page") or item.get("title")).encode()).hexdigest()[:8]


def mk_item(s):
    """Data ringkas lagu (cukup untuk diputar ulang nanti). None kalau tidak ada link halaman."""
    page = s.get("page")
    if not page:
        return None
    return {"page": page, "title": s.get("title") or "Unknown", "duration": int(s.get("duration") or 0),
            "channel": s.get("channel", ""), "source": s.get("orig_source") or s.get("source") or "YouTube",
            "thumb": s.get("thumb"), "video": bool(s.get("video"))}


def lazy(item, by=None):
    """Item antrean yang baru diambil link audionya saat giliran diputar."""
    return dict(item, lazy=True, by=by)


def by_name(uid):
    if uid == OWNER_ID:
        return "Owner"
    return DATA["admins"].get(str(uid), f"ID {uid}")


def add_history(song):
    it = mk_item(song)
    if not it:
        return
    DATA["hist"] = ([it] + [h for h in DATA["hist"] if iid(h) != iid(it)])[:HIST_MAX]
    schedule_save()


def fav_add(uid, item):
    lst = DATA["fav"].setdefault(str(uid), [])
    if any(iid(x) == iid(item) for x in lst):
        return False
    lst.insert(0, item)
    del lst[FAV_MAX:]
    schedule_save()
    return True


def fav_del(uid, id_):
    lst = DATA["fav"].get(str(uid), [])
    n = len(lst)
    lst[:] = [x for x in lst if iid(x) != id_]
    if len(lst) != n:
        schedule_save()
    return len(lst) != n


def sleep_left():
    return max(0, int(SLEEP["until"] - time.time())) if SLEEP["until"] else 0


async def _sleep_worker(sec):
    try:
        await asyncio.sleep(sec)
    except asyncio.CancelledError:
        return
    SLEEP["until"] = 0
    SLEEP["task"] = None
    if TARGET_VC_ID:
        await act_stop()


def set_sleep(minutes):
    t = SLEEP.get("task")
    if t:
        t.cancel()
    if not minutes:
        SLEEP["task"] = None
        SLEEP["until"] = 0
        return "⏰ Timer tidur dimatikan."
    SLEEP["until"] = time.time() + minutes * 60
    SLEEP["task"] = asyncio.get_running_loop().create_task(_sleep_worker(minutes * 60))
    return f"⏰ Musik berhenti otomatis {minutes} menit lagi."


async def act_volume(delta=None, value=None):
    global VOLUME
    e = need_vc()
    if e:
        return e
    v = value if value is not None else VOLUME + (delta or 0)
    v = max(0, min(200, int(v)))
    try:
        await call_any(["change_volume_call", "change_volume"], TARGET_VC_ID, v)
    except AttributeError:
        return "❌ Versi py-tgcalls ini tidak mendukung pengaturan volume."
    except Exception as ex:
        return f"❌ Gagal: {esc(cut(ex, 100))}"
    VOLUME = v
    schedule_save()
    return f"🔊 Volume {v}%"


def is_playlist_url(u):
    return bool(re.search(r"youtube\.com/playlist\?|music\.youtube\.com/playlist|soundcloud\.com/[^/]+/sets/", u))


def expand_playlist(url, limit):
    src = "SoundCloud" if "soundcloud" in url else "YouTube"
    opts = _opts(use_cookies=(src == "YouTube"))
    opts.update({"extract_flat": True, "noplaylist": False, "playlistend": limit})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _items_from_info(info, src)[:limit]


async def do_enqueue_many(items, by=None):
    chat_id = TARGET_VC_ID
    q = get_queue(chat_id)
    cur = current_songs.get(chat_id)
    idle = (not cur) or cur["title"] == "Silent"
    for it in items:
        q.append(lazy(it, by))
    if idle:
        await play_next(chat_id)
    schedule_save()
    return f"📥 <b>{len(items)}</b> lagu masuk antrean."


async def restore_state():
    """Setelah restart: masuk lagi ke VC terakhir dan lanjutkan antrean."""
    raw = DATA.get("vc_raw")
    if not raw or not userbot_app:
        return None
    saved = list(DATA.get("queue") or [])
    notice = await do_setvc(raw)
    if not TARGET_VC_ID:
        return notice
    if saved:
        q = get_queue(TARGET_VC_ID)
        for it in saved:
            q.append(lazy(it, "♻️"))
        await play_next(TARGET_VC_ID)
        return f"♻️ <b>Dipulihkan:</b> VC <code>{TARGET_VC_ID}</code>, {len(saved)} lagu dilanjutkan."
    return f"♻️ VC <code>{TARGET_VC_ID}</code> dipulihkan."


# ==========================================
# HELPER PY-TGCALLS (tahan beda versi)
# ==========================================
def _ignore_flag():
    flags = getattr(MediaStream, "Flags", None)
    if flags is not None and hasattr(flags, "IGNORE"):
        return flags.IGNORE
    return getattr(MediaStream, "IGNORE", None)


def audio_stream(url):
    flag = _ignore_flag()
    if flag is not None:
        try:
            return MediaStream(url, video_flags=flag)
        except TypeError:
            pass
    return MediaStream(url)


def _video_quality_candidates():
    out = []
    w, h, fps = VIDEO_SIZES.get(VIDEO_HEIGHT, VIDEO_SIZES[480])
    try:
        from pytgcalls.types import VideoParameters
        out.append(VideoParameters(width=w, height=h, frame_rate=fps))
    except Exception:
        pass
    try:
        from pytgcalls.types import VideoQuality
        name = {144: "SD_480p", 240: "SD_480p", 360: "SD_480p",
                480: "SD_480p", 720: "HD_720p"}.get(VIDEO_HEIGHT)
        q = getattr(VideoQuality, name, None) if name else None
        if q is not None:
            out.append(q)
    except Exception:
        pass
    return out


def video_stream(url):
    for q in _video_quality_candidates():
        try:
            return MediaStream(url, video_parameters=q)
        except Exception:
            continue
    return MediaStream(url)


def make_stream(song):
    if song.get("force_audio"):
        return audio_stream(song["url"])
    if song.get("video"):
        return video_stream(song["url"])
    if AUDIO_PLAIN:
        return MediaStream(song["url"])
    return audio_stream(song["url"])


async def call_any(names, *args, **kwargs):
    for n in names:
        fn = getattr(userbot_call, n, None)
        if fn is not None:
            return await fn(*args, **kwargs)
    raise AttributeError(f"Method {names} tidak ada di versi py-tgcalls ini")


# ==========================================
# PENCARIAN, FILTER, AMBIL LINK
# ==========================================
def _opts(use_cookies, video=False):
    h = VIDEO_HEIGHT
    fmt = (f"best[height<={h}][vcodec!=none][acodec!=none]/best[height<={h}]/best"
           if video else "bestaudio[ext=m4a]/bestaudio/best")
    opts = {
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if use_cookies and os.path.exists(COOKIES):
        opts["cookiefile"] = COOKIES
    return opts


def _items_from_info(info, label):
    out = []
    for v in (info or {}).get("entries") or []:
        if not v:
            continue
        url = v.get("url") or v.get("webpage_url")
        if label == "YouTube" and v.get("id") and not (url or "").startswith("http"):
            url = f"https://www.youtube.com/watch?v={v['id']}"
        if not url:
            continue
        thumb = None
        if label == "YouTube" and v.get("id"):
            thumb = f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"
        else:
            t = v.get("thumbnail")
            if not t and v.get("thumbnails"):
                t = (v["thumbnails"][-1] or {}).get("url")
            if t and re.search(r"\.(jpe?g|png)(\?|$)", t, re.I):
                thumb = t
        out.append({
            "page": url,
            "title": v.get("title") or "Unknown",
            "duration": int(v.get("duration") or 0),
            "channel": v.get("channel") or v.get("uploader") or "",
            "source": label,
            "thumb": thumb,
        })
    return out


def _flat_search(prefix, label, query, n):
    opts = _opts(use_cookies=(prefix == "ytsearch"))
    opts["extract_flat"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    return _items_from_info(info, label)


def passes_filter(item):
    d = item.get("duration") or 0
    if d and not (MIN_DUR <= d <= MAX_DUR):
        return False
    title = item["title"].lower()
    for w in BLOCKED_WORDS:
        if re.search(rf"\b{re.escape(w.lower())}\b", title):
            return False
    return True


def search_candidates(query):
    errors = []
    for prefix, label in (("ytsearch", "YouTube"), ("scsearch", "SoundCloud")):
        try:
            n = MAX_RESULTS * 2 if FILTER_ON else MAX_RESULTS
            items = _flat_search(prefix, label, query, n)
            if FILTER_ON:
                items = [i for i in items if passes_filter(i)]
            if items:
                return items[:MAX_RESULTS], None
            errors.append(f"{label}: tidak ada hasil yang lolos filter")
        except Exception as e:
            print(f"Error Search [{label}]: {e}")
            errors.append(f"{label}: {str(e)[:100]}")
    return None, "Lagu tidak ditemukan.\n" + "\n".join(errors)


def _song_from_info(v, label, video=False):
    url = v.get("url")
    if not url and v.get("requested_formats"):
        url = v["requested_formats"][0].get("url")
    if not url:
        raise RuntimeError("Link audio tidak tersedia.")
    return {
        "url": url,
        "title": v.get("title", "Unknown"),
        "duration": int(v.get("duration") or 0),
        "source": label,
        "video": video,
        "fmt": f"{v.get('ext', '?')}/{v.get('protocol', '?')}",
    }


def _resolve_url(page, use_cookies, label, video=False):
    with yt_dlp.YoutubeDL(_opts(use_cookies, video)) as ydl:
        v = ydl.extract_info(page, download=False)
    if v.get("entries"):
        v = v["entries"][0]
    return _song_from_info(v, label, video)


def _resolve_song(item):
    src = item.get("source")
    video = bool(item.get("video"))
    audio_movie = video and VIDEO_HEIGHT == 0
    if audio_movie:
        video = False
    errors = []
    if item.get("video") and re.search(r"\.(mp4|mkv|webm|m3u8|mov)(\?|$)", item["page"], re.I):
        return {"url": item["page"], "title": item["title"], "duration": 0,
                "source": "Link", "video": video, "force_audio": audio_movie}, None
    try:
        song = _resolve_url(item["page"], src == "YouTube", src or "YouTube", video)
        song["force_audio"] = audio_movie
        return song, None
    except Exception as e:
        print(f"Error resolve [{src}]: {e}")
        errors.append(f"{src}: {str(e)[:120]}")

    if src != "SoundCloud" and not item.get("video"):
        try:
            alt = _flat_search("scsearch", "SoundCloud", item["title"], 3)
            if alt:
                return _resolve_url(alt[0]["page"], False, "SoundCloud"), None
            errors.append("SoundCloud: tidak ada hasil")
        except Exception as e:
            print(f"Error fallback SC: {e}")
            errors.append(f"SoundCloud: {str(e)[:120]}")
    return None, "\n".join(errors)


def resolve_song(item):
    song, err = _resolve_song(item)
    if song:
        song["thumb"] = item.get("thumb")
        song["channel"] = item.get("channel", "")
        song["page"] = item.get("page")
        song["orig_source"] = item.get("source")
    return song, err


# ==========================================
# KOLASE GAMBAR HASIL PENCARIAN (satu gambar, nomor di tiap thumbnail)
# ==========================================
def _fetch(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _font(size):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_collage(items, start_index=0):
    """Gabungkan thumbnail jadi satu gambar bernomor. None kalau Pillow tidak ada."""
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError:
        return None
    TW, TH, GAP, COLS = 320, 180, 8, 3
    n = len(items)
    if not n:
        return None
    rows = math.ceil(n / COLS)
    cols = min(COLS, n)
    W = cols * TW + (cols + 1) * GAP
    H = rows * TH + (rows + 1) * GAP
    canvas = Image.new("RGB", (W, H), (20, 20, 24))

    def load(it):
        if not it.get("thumb"):
            return None
        try:
            raw = _fetch(it["thumb"])
            return Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as e:
            print(f"Thumb gagal diunduh: {e}")
            return None

    with ThreadPoolExecutor(max_workers=5) as ex:
        imgs = list(ex.map(load, items))

    font = _font(30)
    for i, img in enumerate(imgs):
        r, c = divmod(i, COLS)
        x = GAP + c * (TW + GAP)
        y = GAP + r * (TH + GAP)
        tile = ImageOps.fit(img, (TW, TH)) if img else Image.new("RGB", (TW, TH), (52, 52, 60))
        canvas.paste(tile, (x, y))
        d = ImageDraw.Draw(canvas)
        cx, cy, rad = x + 30, y + 30, 22
        d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=(255, 255, 255), outline=(0, 0, 0), width=2)
        label = str(start_index + i + 1)
        bb = d.textbbox((0, 0), label, font=font)
        d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], cy - (bb[3] - bb[1]) / 2 - bb[1]),
               label, fill=(0, 0, 0), font=font)

    bio = io.BytesIO()
    canvas.save(bio, "JPEG", quality=82)
    bio.name = "hasil.jpg"
    bio.seek(0)
    return bio


# ==========================================
# PLAYER
# ==========================================
def cancel_timer(chat_id):
    t = timers.pop(chat_id, None)
    if t:
        t.cancel()


async def auto_next(chat_id, delay):
    try:
        await asyncio.sleep(delay)
        timers.pop(chat_id, None)
        cur = current_songs.get(chat_id)
        if LOOP and cur and cur["title"] != "Silent":
            get_queue(chat_id).insert(0, dict(cur))
        await play_next(chat_id)
    except asyncio.CancelledError:
        pass


def schedule_next(chat_id, song, delay):
    cancel_timer(chat_id)
    song["t0"] = time.time()
    song["left"] = delay
    timers[chat_id] = asyncio.create_task(auto_next(chat_id, delay))


def elapsed(song, chat_id):
    dur = song.get("duration") or 0
    if not dur or "left" not in song:
        return 0
    left = song["left"]
    if chat_id in timers and "t0" in song:
        left -= time.time() - song["t0"]
    return int(min(dur, max(0, dur + 2 - left)))


async def play_next(chat_id):
    cancel_timer(chat_id)
    queue = get_queue(chat_id)

    if not queue:
        current_songs[chat_id] = silent_song()
        try:
            await userbot_call.play(chat_id, audio_stream(SILENT_AUDIO))
        except Exception as e:
            print(f"Error silent: {e}")
        schedule_save()
        return

    song = queue.pop(0)
    if song.get("lazy"):
        by = song.get("by")
        resolved, err = await asyncio.to_thread(resolve_song, song)
        if not resolved:
            print(f"Lewati (gagal diambil): {song.get('title')} | {err}")
            return await play_next(chat_id)
        resolved["by"] = by
        song = resolved
    prev = current_songs.get(chat_id)
    was_idle = (not prev) or prev.get("title") == "Silent"
    current_songs[chat_id] = song
    try:
        await userbot_call.play(chat_id, make_stream(song))
        if was_idle and not song.get("video") and FIRST_PLAY_RETRY:
            await asyncio.sleep(2)
            await userbot_call.play(chat_id, make_stream(song))
        try:
            await call_any(["unmute", "unmute_stream"], chat_id)
        except Exception:
            pass
        print(f"▶️ Memutar: {song['title']} | {song.get('source')} {song.get('fmt')}")
        add_history(song)
        if song.get("duration"):
            schedule_next(chat_id, song, song["duration"] + 2)
    except Exception as e:
        print(f"Error playing: {e}")
        await play_next(chat_id)
        return
    schedule_save()


async def do_enqueue(song, now=False, by=None):
    chat_id = TARGET_VC_ID
    if by:
        song["by"] = by
    q = get_queue(chat_id)
    cur = current_songs.get(chat_id)
    idle = (not cur) or cur["title"] == "Silent"
    ico = "🎬 " if song.get("video") else ""
    title = f"{ico}<b>{esc(cut(song['title'], 60))}</b>"
    if now or idle:
        q.insert(0, song)
        await play_next(chat_id)
        return f"▶️ Memutar: {title}"
    q.append(song)
    schedule_save()
    return f"➕ Antrean #{len(q)}: {title}"


# ==========================================
# AKSI KONTROL (dipakai perintah dan tombol)
# ==========================================
NO_USERBOT = "❌ Userbot belum online. Owner: kirim file zip session ke bot ini."
NO_VC = "❌ VC belum diset. Owner: /setvc @usernamegrup"


def need_vc():
    if not userbot_call:
        return NO_USERBOT
    if not TARGET_VC_ID:
        return NO_VC
    return None


async def act_pause():
    e = need_vc()
    if e:
        return e
    try:
        await call_any(["pause", "pause_stream"], TARGET_VC_ID)
        song = current_songs.get(TARGET_VC_ID)
        if song and TARGET_VC_ID in timers and "t0" in song:
            song["left"] = max(1, song["left"] - (time.time() - song["t0"]))
            cancel_timer(TARGET_VC_ID)
        return "⏸ Dijeda."
    except Exception as ex:
        return f"❌ Gagal: {esc(ex)}"


async def act_resume():
    e = need_vc()
    if e:
        return e
    try:
        await call_any(["resume", "resume_stream"], TARGET_VC_ID)
        song = current_songs.get(TARGET_VC_ID)
        if song and song.get("duration") and "left" in song and TARGET_VC_ID not in timers:
            schedule_next(TARGET_VC_ID, song, song["left"])
        return "▶️ Dilanjutkan."
    except Exception as ex:
        return f"❌ Gagal: {esc(ex)}"


async def act_skip():
    e = need_vc()
    if e:
        return e
    await play_next(TARGET_VC_ID)
    return "⏭ Di-skip."


async def act_stop():
    e = need_vc()
    if e:
        return e
    cancel_timer(TARGET_VC_ID)
    queues[TARGET_VC_ID] = []
    current_songs[TARGET_VC_ID] = silent_song()
    try:
        await userbot_call.play(TARGET_VC_ID, audio_stream(SILENT_AUDIO))
    except Exception:
        pass
    schedule_save()
    return "⏹ Dihentikan (tetap di VC)."


async def act_loop():
    global LOOP
    LOOP = not LOOP
    await save_data()
    return f"🔁 Loop {onoff(LOOP)}."


async def act_filter():
    global FILTER_ON
    FILTER_ON = not FILTER_ON
    await save_data()
    return f"🧹 Filter {onoff(FILTER_ON)}."


async def act_hemat(arg=""):
    global VIDEO_HEIGHT, HEMAT_ON, PREV_HEIGHT
    if HEMAT_ON:
        HEMAT_ON = False
        VIDEO_HEIGHT = PREV_HEIGHT
        await save_data()
        return f"💡 Hemat OFF. Kualitas kembali {quality_label()}."
    target = int(arg) if arg in ("144", "240", "360") else HEMAT_HEIGHT
    PREV_HEIGHT = VIDEO_HEIGHT
    VIDEO_HEIGHT = target
    HEMAT_ON = True
    await save_data()
    return f"💡 Hemat ON ({target}p). Berlaku untuk video berikutnya."


async def act_quality(arg):
    global VIDEO_HEIGHT, HEMAT_ON
    arg = (arg or "").strip().lower()
    if arg not in ("audio", "144", "240", "360", "480", "720"):
        return (f"📺 Kualitas sekarang: {quality_label()}\n"
                "Ubah: /quality 144, 240, 360, 480, 720, atau audio.")
    VIDEO_HEIGHT = 0 if arg == "audio" else int(arg)
    HEMAT_ON = False
    await save_data()
    return f"📺 Kualitas diset {quality_label()} (video berikutnya)."


async def act_remove(n):
    e = need_vc()
    if e:
        return e
    q = get_queue(TARGET_VC_ID)
    if not (1 <= n <= len(q)):
        return "❌ Nomor tidak ada di antrean."
    s = q.pop(n - 1)
    schedule_save()
    return f"🗑 Dihapus: {esc(cut(s['title'], 50))}"


async def act_clear():
    e = need_vc()
    if e:
        return e
    queues[TARGET_VC_ID] = []
    schedule_save()
    return "🧹 Antrean dikosongkan."


async def act_shuffle():
    e = need_vc()
    if e:
        return e
    random.shuffle(get_queue(TARGET_VC_ID))
    schedule_save()
    return "🔀 Antrean diacak."


# ==========================================
# TAMPILAN PANEL (satu pesan yang diedit di tempat)
# ==========================================
def B(text, data):
    return Button.inline(text, data.encode())


def bar(pos, total, n=12):
    if not total:
        return "▱" * n
    k = int(n * pos / total)
    return "▰" * k + "▱" * (n - k)


def ctrl_row():
    return [B("⏸", "a:pause"), B("▶️", "a:resume"), B("⏭", "a:skip"), B("⏹", "a:stop")]


def back_row():
    return [B("◀️ Menu", "v:main")]


def panel_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    playing = cur and cur["title"] != "Silent"
    now = f"<b>{esc(cut(cur['title'], 45))}</b>" if playing else "<i>tidak ada</i>"
    ub = "🟢 online" if userbot_call else "🔴 offline"
    vc = f"<code>{TARGET_VC_ID}</code>" if TARGET_VC_ID else "<i>belum diset</i>"
    qn = len(get_queue(TARGET_VC_ID)) if TARGET_VC_ID else 0
    return (
        "<b>🎵 Music Panel</b>\n\n"
        f"<blockquote>👤 Userbot: {ub}\n📡 VC: {vc}\n🎶 Main: {now}\n"
        f"📜 Antrean: <b>{qn}</b> lagu</blockquote>\n"
        f"<i>🔁 Loop {onoff(LOOP)} • 🧹 Filter {onoff(FILTER_ON)} • 📺 {quality_label()}"
        f"{' 💡' if HEMAT_ON else ''}</i>\n"
        "<i>Kirim teks biasa kapan saja untuk mencari lagu.</i>"
    )


def main_buttons():
    return [
        ctrl_row(),
        [B("🔎 Cari", "i:play"), B("🎬 Nonton", "i:vplay"), B("⚡ Cepat", "i:fast")],
        [B("🎶 Now", "v:np"), B("📜 Antrean", "v:queue"), B("⚙️ Setelan", "v:settings")],
        [B("⭐ Favorit", "v:fav"), B("🕘 Riwayat", "v:hist"), B("📡 VC", "v:vc")],
        [B("👥 Admin", "v:admins"), B("📊", "v:status"), B("❓", "v:help"), B("🔄", "v:main")],
    ]


def np_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    if not cur or cur["title"] == "Silent":
        return "<b>🎶 Sedang Diputar</b>\n\n<i>Tidak ada yang diputar.</i>"
    dur = cur.get("duration") or 0
    pos = elapsed(cur, TARGET_VC_ID)
    by = f"\n👤 Diminta: {esc(cut(cur['by'], 20))}" if cur.get("by") else ""
    sl = f"\n⏰ Berhenti {sleep_left() // 60 + 1} menit lagi" if sleep_left() else ""
    return (
        "<b>🎶 Sedang Diputar</b>\n\n"
        f"<b>{esc(cut(cur['title'], 70))}</b>\n"
        f"<i>{esc(cut(cur.get('channel', ''), 30))}{' • ' if cur.get('channel') else ''}"
        f"{esc(cur.get('source', '?'))}</i>\n\n"
        f"<code>{bar(pos, dur)}  {fmt_dur(pos)} / {fmt_dur(dur)}</code>\n\n"
        f"<blockquote>🔁 Loop {onoff(LOOP)} • 🔊 {VOLUME}% • 📺 {quality_label()}{by}{sl}</blockquote>"
    )


def np_buttons():
    return [
        ctrl_row(),
        [B("⭐ Simpan", "n:save"), B(f"🔁 Loop: {onoff(LOOP)}", "a:loop"), B("🔄", "v:np")],
        [B("🔉 -10", "n:vol:-10"), B(f"{VOLUME}%", "noop"), B("🔊 +10", "n:vol:10")],
        back_row(),
    ]


def queue_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    head = "<b>📜 Antrean</b>\n\n"
    if cur and cur["title"] != "Silent":
        head += f"🎶 <b>{esc(cut(cur['title'], 50))}</b>\n\n"
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    if not q:
        return head + "<i>Antrean kosong.</i>"
    lines = []
    for i, s in enumerate(q[:15]):
        by = f" <i>· {esc(cut(s['by'], 12))}</i>" if s.get("by") else ""
        lines.append(f"{i + 1}. {esc(cut(s['title'], 45))}{by}")
    more = f"\n… dan {len(q) - 15} lagi" if len(q) > 15 else ""
    return head + "<blockquote>" + "\n".join(lines) + more + "</blockquote>"


def queue_buttons():
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    rows = []
    nums = [B(f"🗑{i + 1}", f"rm:{i + 1}") for i in range(min(len(q), 10))]
    for i in range(0, len(nums), 5):
        rows.append(nums[i:i + 5])
    rows.append([B("🔀 Acak", "a:shuf"), B("🧹 Kosongkan", "a:clr")])
    rows.append([B("⏭ Skip", "a:skip"), B("◀️ Menu", "v:main")])
    return rows


def settings_text():
    sl = f"{sleep_left() // 60 + 1} menit lagi" if sleep_left() else "mati"
    return (
        "<b>⚙️ Setelan</b>\n\n"
        f"<blockquote>🧹 Filter: <b>{onoff(FILTER_ON)}</b> "
        f"({fmt_dur(MIN_DUR)}-{fmt_dur(MAX_DUR)})\n"
        f"🔁 Loop: <b>{onoff(LOOP)}</b>\n"
        f"💡 Hemat kuota: <b>{onoff(HEMAT_ON)}</b>\n"
        f"📺 Kualitas video: <b>{quality_label()}</b>\n"
        f"⏰ Timer tidur: <b>{sl}</b></blockquote>\n"
        "<i>Kualitas berlaku untuk video berikutnya.</i>"
    )


def settings_buttons():
    qrow = [B(f"{'✔' if VIDEO_HEIGHT == q else ''}{q}", f"q:{q}") for q in (144, 240, 360, 480, 720)]
    qrow.append(B("✔🔈" if VIDEO_HEIGHT == 0 else "🔈", "q:audio"))
    return [
        [B(f"🧹 Filter: {onoff(FILTER_ON)}", "a:filter"), B(f"🔁 Loop: {onoff(LOOP)}", "a:loop")],
        [B(f"💡 Hemat: {onoff(HEMAT_ON)}", "a:hemat")],
        qrow,
        [B("⏰ 15m", "t:15"), B("⏰ 30m", "t:30"), B("⏰ 60m", "t:60"), B("⏰ Off", "t:0")],
        back_row(),
    ]


def list_data(kind, uid):
    return list(DATA["fav"].get(str(uid), [])) if kind == "fav" else list(DATA["hist"])


def _list_page(kind, uid, n_items):
    tp = max(1, math.ceil(n_items / PAGE_SIZE))
    p = min(max(0, LISTPAGE.get((uid, kind), 0)), tp - 1)
    LISTPAGE[(uid, kind)] = p
    return p, tp


def list_text(kind, uid):
    items = list_data(kind, uid)
    title = "⭐ Favorit" if kind == "fav" else "🕘 Riwayat"
    if not items:
        hint = ("Tekan ⭐ Simpan di layar Now, atau ⭐ di Riwayat." if kind == "fav"
                else "Lagu yang sudah diputar muncul di sini.")
        return f"<b>{title}</b>\n\n<i>Belum ada. {hint}</i>"
    page, tp = _list_page(kind, uid, len(items))
    start = page * PAGE_SIZE
    lines = [f"<b>{start + i + 1}.</b> {esc(cut(it['title'], 45))} <i>• {fmt_dur(it['duration'])}</i>"
             for i, it in enumerate(items[start:start + PAGE_SIZE])]
    return (f"<b>{title}</b>\n<i>Halaman {page + 1} dari {tp} • {len(items)} lagu</i>\n\n"
            "<blockquote>" + "\n".join(lines) + "</blockquote>\n"
            f"<i>▶️ = putar/antre • {'🗑 = hapus' if kind == 'fav' else '⭐ = simpan ke favorit'}</i>")


def list_buttons(kind, uid):
    items = list_data(kind, uid)
    rows = []
    if items:
        page, tp = _list_page(kind, uid, len(items))
        start = page * PAGE_SIZE
        sl = items[start:start + PAGE_SIZE]
        rows.append([B(f"▶️{start + i + 1}", f"l:play:{kind}:{iid(it)}") for i, it in enumerate(sl)])
        second = "del" if kind == "fav" else "save"
        icon = "🗑" if kind == "fav" else "⭐"
        rows.append([B(f"{icon}{start + i + 1}", f"l:{second}:{kind}:{iid(it)}") for i, it in enumerate(sl)])
        if tp > 1:
            rows.append([B("◀️", f"l:pg:{kind}:-1") if page > 0 else B("·", "noop"),
                         B(f"{page + 1}/{tp}", "noop"),
                         B("▶️", f"l:pg:{kind}:1") if page < tp - 1 else B("·", "noop")])
        rows.append([B("➕ Semua ke antrean", f"l:all:{kind}"), B("◀️ Menu", "v:main")])
    else:
        rows.append(back_row())
    return rows


def vc_text():
    ub = "🟢 online" if userbot_call else "🔴 offline"
    vc = f"<code>{TARGET_VC_ID}</code>" if TARGET_VC_ID else "<i>belum diset</i>"
    return (
        "<b>📡 Userbot dan VC</b>\n\n"
        f"<blockquote>👤 Userbot: {ub}\n📡 VC: {vc}\n"
        f"💾 Data: {'tersimpan' if not DATA_DIRTY else 'menunggu userbot'}</blockquote>\n"
        "<i>Admin & owner bisa memilih VC lewat tombol di bawah.\n"
        "Userbot offline? Owner kirim file zip session ke bot ini.</i>"
    )


def vc_buttons(uid):
    rows = []
    if is_admin(uid):
        rows.append([B("📡 Pilih / Ganti VC", "i:setvc"), B("🚪 Keluar VC", "a:unsetvc")])
    if is_owner(uid):
        rows.append([B("🔑 Session", "a:session")])
    rows.append(back_row())
    return rows


def admins_text():
    lines = [f"👑 Owner • <code>{OWNER_ID}</code>"]
    for uid, name in DATA["admins"].items():
        lines.append(f"👤 {esc(cut(name, 25))} • <code>{uid}</code>")
    if len(lines) == 1:
        lines.append("<i>belum ada admin tambahan</i>")
    return (
        "<b>👥 Admin</b>\n\n<blockquote>" + "\n".join(lines) + "</blockquote>\n"
        "<i>Orang lain bisa kirim /start ke bot ini lalu menekan Minta akses. "
        "Permintaannya muncul di chat owner dengan tombol Setujui / Tolak.</i>"
    )


def admins_buttons(uid):
    rows = []
    if is_owner(uid):
        rows.append([B("➕ Tambah Admin", "i:addadmin")])
        rows += [[B(f"🗑 {cut(n, 20)}", f"deladm:{u}")] for u, n in list(DATA["admins"].items())[:10]]
    rows.append(back_row())
    return rows


def status_text():
    up = int(time.time() - START_TIME)
    return (
        "<b>📊 Status</b>\n\n<blockquote>"
        f"⏱ Uptime: {up // 3600}j {up % 3600 // 60}m\n"
        f"👤 Userbot: {'online' if userbot_call else 'offline'}\n"
        f"📡 VC: {TARGET_VC_ID or 'belum diset'}\n"
        f"👥 Admin: {len(DATA['admins'])} + owner\n"
        f"🍪 Cookies: {'ada' if os.path.exists(COOKIES) else 'tidak ada'}\n"
        f"💾 Data: {'tersimpan' if not DATA_DIRTY else 'menunggu userbot'}</blockquote>"
    )


def help_text():
    return (
        "<b>📖 Bantuan</b>\n"
        "<i>Semua fitur ada di tombol /start. Perintah di bawah hanya alternatif.</i>\n\n"
        "<b>Putar</b>\n<blockquote>"
        "/play <i>judul, link, atau link playlist</i> — cari lagu (gambar + halaman)\n"
        "/fast <i>judul</i> — langsung putar hasil teratas\n"
        "/vplay <i>judul atau link</i> — nonton (share screen)\n"
        "Teks biasa tanpa / — cari lagu</blockquote>\n"
        "<b>Kontrol</b>\n<blockquote>"
        "/np — sedang diputar\n/queue — antrean\n/pause • /resume • /skip • /stop\n"
        "/loop — ulang lagu\n/shuffle — acak antrean\n/clear — kosongkan antrean\n"
        "/remove <i>N</i> — hapus nomor N dari antrean\n"
        "/volume <i>0-200</i> — atur volume\n/sleep <i>menit atau off</i> — timer tidur</blockquote>\n"
        "<b>Koleksi</b>\n<blockquote>"
        "/fav — daftar favorit\n/save — simpan lagu sekarang ke favorit\n"
        "/history — 15 lagu terakhir</blockquote>\n"
        "<b>Setelan</b>\n<blockquote>"
        "/settings — menu setelan\n/filter — filter lagu on/off\n"
        "/hemat <i>[144|240|360]</i> — hemat kuota\n"
        "/quality <i>[audio|144-720]</i> — kualitas video\n"
        "/amode — mode audio (admin & owner)\n"
        "/status • /admins • /panel • /help</blockquote>\n"
        "<b>VC (admin & owner)</b>\n<blockquote>"
        "/setvc <i>@grup, link, atau ID</i> — pilih VC\n/unsetvc — keluar VC</blockquote>\n"
        "<b>Owner</b>\n<blockquote>"
        "/addadmin <i>ID atau @user</i> • /deladmin <i>ID</i>\n/getsession — SESSION_STRING</blockquote>\n"
        "<b>Otomatis</b>\n<blockquote>"
        "Setelah bot restart, VC terakhir dan antrean dipulihkan sendiri.\n"
        "Hasil pencarian: tekan nomor • ◀️ ▶️ halaman • ⏩ Langsung ON = putar sekarang</blockquote>"
    )


PROMPTS = {
    "play": ("🔎 Cari Lagu", "Ketik <b>judul lagu</b> atau tempel <b>link</b>, lalu kirim."),
    "vplay": ("🎬 Nonton", "Ketik <b>judul video</b> atau tempel <b>link</b>, lalu kirim."),
    "fast": ("⚡ Putar Cepat", "Ketik <b>judul lagu</b>. Hasil teratas langsung diputar."),
    "setvc": ("📡 Pilih VC", "Kirim <code>@usernamegrup</code>, link grup, atau ID.\n"
                            "Grup privat: link undangan <code>t.me/+kode</code>."),
    "addadmin": ("➕ Tambah Admin", "Kirim <b>ID angka</b> atau <code>@username</code> orangnya."),
}

STABLE_VIEWS = {"main", "np", "queue", "settings", "admins", "vc", "help", "status", "input", "fav", "hist"}


def input_text(kind):
    title, body = PROMPTS.get(kind, PROMPTS["play"])
    return f"<b>{title}</b>\n\n{body}\n\n<i>Atau tekan Batal.</i>"


def render_view(view, uid):
    notice = NOTICE.pop(uid, None)
    btns = None
    if view == "np":
        text, btns = np_text(), np_buttons()
    elif view == "queue":
        text, btns = queue_text(), queue_buttons()
    elif view == "settings":
        text, btns = settings_text(), settings_buttons()
    elif view in ("fav", "hist"):
        text, btns = list_text(view, uid), list_buttons(view, uid)
    elif view == "admins":
        text, btns = admins_text(), admins_buttons(uid)
    elif view == "vc":
        text, btns = vc_text(), vc_buttons(uid)
    elif view == "help":
        text, btns = help_text(), [back_row()]
    elif view == "status":
        text, btns = status_text(), [back_row()]
    elif view == "input":
        text, btns = input_text(WAIT.get(uid, "play")), [[B("❌ Batal", "i:cancel")]]
    elif view == "busy":
        text = "<b>⏳ Mohon tunggu…</b>"
    else:
        text, btns = panel_text(), main_buttons()
    if notice:
        text += f"\n\n💬 {notice}"
    return text, btns


# ==========================================
# KELOLA PANEL (tanpa spam)
# ==========================================
async def show(event, view):
    """Edit pesan panel yang ditekan, tanpa mengirim pesan baru."""
    chat_id = event.chat_id
    if view not in STABLE_VIEWS:
        view = "main"
    VIEW[chat_id] = view
    PANELS[chat_id] = event.query.msg_id
    text, btns = render_view(view, event.sender_id)
    try:
        await event.edit(text, buttons=btns)
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"Edit panel gagal: {e}")


async def panel_to(chat_id, view="main", notice=None):
    """Perbarui panel yang sudah ada (edit di tempat). Kalau tidak ada, kirim baru."""
    VIEW[chat_id] = view
    if notice:
        NOTICE[chat_id] = notice
    text, btns = render_view(view, chat_id)
    mid = PANELS.get(chat_id)
    if mid:
        try:
            await manager_bot.edit_message(chat_id, mid, text, buttons=btns)
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    m = await manager_bot.send_message(chat_id, text, buttons=btns)
    PANELS[chat_id] = m.id


async def delete_panel(chat_id):
    mid = PANELS.pop(chat_id, None)
    if mid:
        try:
            await manager_bot.delete_messages(chat_id, [mid])
        except Exception:
            pass


async def bring_panel(chat_id, view="main", notice=None):
    """Hapus panel lama lalu kirim panel baru di paling bawah (selalu terlihat)."""
    await delete_panel(chat_id)
    VIEW[chat_id] = view
    if notice:
        NOTICE[chat_id] = notice
    text, btns = render_view(view, chat_id)
    m = await manager_bot.send_message(chat_id, text, buttons=btns)
    PANELS[chat_id] = m.id


async def cleanup_cmd(event):
    cid = getattr(event, "id", None)
    if AUTO_CLEAN and cid:
        try:
            await manager_bot.delete_messages(event.chat_id, [cid])
        except Exception:
            pass


async def _delete_later(chat_id, ids, ttl):
    await asyncio.sleep(ttl)
    try:
        await manager_bot.delete_messages(chat_id, ids)
    except Exception:
        pass


async def cmd_done(event, notice=None, view=None):
    """Akhir perintah ketik: hapus perintahnya, tampilkan panel terbaru + catatan."""
    await cleanup_cmd(event)
    v = view or VIEW.get(event.chat_id, "main")
    if v not in STABLE_VIEWS or v == "input":
        v = "main"
    await bring_panel(event.chat_id, v, notice)


# ==========================================
# HASIL PENCARIAN: satu pesan (kolase gambar) + halaman
# ==========================================
def page_items(st):
    s = st["page"] * PAGE_SIZE
    return st["results"][s:s + PAGE_SIZE]


def total_pages(st):
    return max(1, math.ceil(len(st["results"]) / PAGE_SIZE))


def results_caption(st):
    start = st["page"] * PAGE_SIZE
    lines = []
    for i, r in enumerate(page_items(st)):
        ch = esc(cut(r["channel"], 25)) + " • " if r.get("channel") else ""
        lines.append(
            f"<b>{start + i + 1}.</b> {esc(cut(r['title'], 55))}\n"
            f"      <i>{ch}{fmt_dur(r['duration'])} • {r['source']}</i>"
        )
    icon = "🎬" if st["video"] else "🔎"
    cap = (
        f"<b>{icon} {esc(cut(st['query'], 40))}</b>\n"
        f"<i>Halaman {st['page'] + 1} dari {total_pages(st)} • {len(st['results'])} hasil</i>\n\n"
        "<blockquote>" + "\n".join(lines) + "</blockquote>"
    )
    if st.get("status"):
        cap += f"\n\n{st['status']}"
    return cap


def results_buttons(st):
    start = st["page"] * PAGE_SIZE
    n = len(page_items(st))
    nums = [B(str(start + i + 1), f"pk:{start + i}") for i in range(n)]
    tp = total_pages(st)
    prev_b = B("◀️", "pg:-1") if st["page"] > 0 else B("·", "noop")
    next_b = B("▶️", "pg:1") if st["page"] < tp - 1 else B("·", "noop")
    return [
        nums,
        [prev_b, B(f"{st['page'] + 1}/{tp}", "noop"), next_b],
        [B(f"⏩ Langsung: {onoff(st['now'])}", "nowtoggle"), B("🔎 Cari lagi", "i:again"),
         B("🏠 Menu", "home")],
    ]


async def show_results(chat_id, query, results, video):
    old = PENDING.pop(chat_id, None)
    if old and old.get("mid"):
        try:
            await manager_bot.delete_messages(chat_id, [old["mid"]])
        except Exception:
            pass
    st = {"results": results, "page": 0, "query": query, "video": video,
          "now": False, "photo": False, "status": "", "busy": False, "mid": None}
    cap, btns = results_caption(st), results_buttons(st)
    collage = None
    try:
        collage = await asyncio.to_thread(make_collage, page_items(st), 0)
    except Exception as e:
        print(f"Kolase gagal: {e}")
    msg = None
    if collage:
        try:
            msg = await manager_bot.send_file(chat_id, collage, caption=cap, buttons=btns)
            st["photo"] = True
        except Exception as e:
            print(f"Kirim kolase gagal: {e}")
    if msg is None:
        msg = await manager_bot.send_message(chat_id, cap, buttons=btns)
    st["mid"] = msg.id
    PENDING[chat_id] = st


async def rerender_results(event, st, new_image):
    cap, btns = results_caption(st), results_buttons(st)
    try:
        if new_image and st["photo"]:
            collage = await asyncio.to_thread(make_collage, page_items(st), st["page"] * PAGE_SIZE)
            if collage:
                try:
                    return await event.edit(cap, file=collage, buttons=btns)
                except Exception as e:
                    if "not modified" in str(e).lower():
                        return
                    print(f"Ganti gambar gagal: {e}")
        await event.edit(cap, buttons=btns)
    except Exception as e:
        if "not modified" not in str(e).lower():
            print(f"Edit hasil gagal: {e}")


async def handle_pick(event, idx):
    st = PENDING.get(event.chat_id)
    if not st or not (0 <= idx < len(st["results"])):
        return await event.answer("Daftar kedaluwarsa, cari lagi.", alert=True)
    err = need_vc()
    if err:
        return await event.answer(toast(err), alert=True)
    if st["busy"]:
        return await event.answer("Sebentar, masih memproses…")
    st["busy"] = True
    await event.answer("⏳ Mengambil…")
    try:
        song, e = await asyncio.to_thread(resolve_song, st["results"][idx])
        if not song:
            st["status"] = f"❌ <i>Gagal ambil:</i> {esc(cut(e, 120))}"
        else:
            st["status"] = await do_enqueue(song, now=st["now"], by=by_name(event.sender_id))
    finally:
        st["busy"] = False
    await rerender_results(event, st, new_image=False)


# ==========================================
# RESOLVE TARGET GRUP (ID / @username / link)
# ==========================================
async def resolve_chat(app, raw):
    raw = raw.strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w-]+)", raw)
    if m:
        h = m.group(1)
        try:
            upd = await app(ImportChatInviteRequest(h))
            chat = upd.chats[0]
        except UserAlreadyParticipantError:
            chat = (await app(CheckChatInviteRequest(h))).chat
        return utils.get_peer_id(chat)

    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z]\w{3,})", raw)
    uname = m.group(1) if m else (raw[1:] if raw.startswith("@") else None)
    if uname:
        ent = await app.get_entity(uname)
        return utils.get_peer_id(ent)

    chat_id = int(raw)
    try:
        await app.get_entity(chat_id)
    except Exception:
        await app.get_dialogs()
        await app.get_entity(chat_id)
    return chat_id


# ==========================================
# USERBOT: NYALA / MATI
# ==========================================
async def shutdown_userbot():
    global userbot_app, userbot_call, TARGET_VC_ID
    for cid in list(current_songs.keys()):
        cancel_timer(cid)
        try:
            if userbot_call:
                await call_any(["leave_call", "leave_group_call"], cid)
        except Exception:
            pass
    current_songs.clear()
    queues.clear()
    PENDING.clear()
    TARGET_VC_ID = None
    if userbot_app:
        try:
            await userbot_app.disconnect()
        except Exception:
            pass
    userbot_app = None
    userbot_call = None


async def boot_userbot(session):
    global userbot_app, userbot_call
    await shutdown_userbot()
    app = TelegramClient(session, API_ID, API_HASH)
    await app.connect()
    if not await app.is_user_authorized():
        await app.disconnect()
        raise RuntimeError("Session tidak valid / sudah logout. Buat ulang file session.")
    await app.get_dialogs()
    call = PyTgCalls(app)
    await call.start()
    userbot_app, userbot_call = app, call
    await load_data()
    if DATA_DIRTY:
        await save_data()


# ==========================================
# AKSI BERSAMA (dipakai tombol DAN perintah)
# ==========================================
async def do_setvc(raw):
    global TARGET_VC_ID
    if not userbot_app:
        return NO_USERBOT
    try:
        chat_id = await resolve_chat(userbot_app, raw)
        if TARGET_VC_ID and TARGET_VC_ID != chat_id:
            try:
                await call_any(["leave_call", "leave_group_call"], TARGET_VC_ID)
            except Exception:
                pass
        await userbot_call.play(chat_id, audio_stream(SILENT_AUDIO),
                                config=GroupCallConfig(auto_start=True))
        TARGET_VC_ID = chat_id
        current_songs[chat_id] = silent_song()
        DATA["vc_raw"] = raw.strip()
        schedule_save()
        return f"✅ <b>VC diset</b> • <code>{chat_id}</code>"
    except Exception as e:
        hint = (
            "Akun belum mengenal grup ini. Pakai @usernamegrup atau link grup. "
            "Grup privat: link undangan t.me/+..."
            if "input entity" in str(e) or "Cannot find any entity" in str(e)
            else "Cek: VC grup sedang aktif, dan ID/username benar."
        )
        return f"❌ <b>Gagal masuk</b>\n<code>{esc(cut(e, 200))}</code>\n<i>{hint}</i>"


async def do_unsetvc():
    global TARGET_VC_ID
    if not TARGET_VC_ID:
        return "ℹ️ VC memang belum diset."
    cancel_timer(TARGET_VC_ID)
    try:
        await call_any(["leave_call", "leave_group_call"], TARGET_VC_ID)
    except Exception:
        pass
    queues[TARGET_VC_ID] = []
    current_songs.pop(TARGET_VC_ID, None)
    TARGET_VC_ID = None
    DATA["vc_raw"] = None
    DATA["queue"] = []
    schedule_save()
    return "✅ Keluar dari VC."


async def do_addadmin(raw):
    a = raw.strip()
    try:
        if a.lstrip("-").isdigit():
            uid = int(a)
            name = f"ID {uid}"
            try:
                name = utils.get_display_name(await manager_bot.get_entity(uid)) or name
            except Exception:
                pass
        else:
            ent = await manager_bot.get_entity(a)
            uid = ent.id
            name = utils.get_display_name(ent) or a
    except Exception as e:
        return f"❌ User tidak ditemukan: <code>{esc(cut(e, 100))}</code>\nPakai ID angka."
    if uid == OWNER_ID:
        return "ℹ️ Itu kamu sendiri, owner sudah punya semua akses."
    DATA["admins"][str(uid)] = name
    await save_data()
    try:
        await manager_bot.send_message(uid, "✅ Kamu sekarang admin Music Bot. Ketik /start.")
    except Exception:
        pass
    return f"✅ <b>{esc(name)}</b> (<code>{uid}</code>) sekarang admin."


async def send_session(chat_id):
    if not userbot_app:
        return NO_USERBOT
    s = StringSession.save(userbot_app.session)
    m = await manager_bot.send_message(
        chat_id,
        "🔑 <b>SESSION_STRING</b>\n<i>Rahasia, setara password akun!</i>\n\n"
        f"<code>{s}</code>\n\n"
        "Simpan di GitHub Secrets dengan nama <code>SESSION_STRING</code> agar userbot "
        "nyala otomatis tanpa upload zip.\n<i>Pesan ini terhapus otomatis dalam 90 detik.</i>")
    asyncio.create_task(_delete_later(chat_id, [m.id], 90))
    return "🔑 SESSION_STRING dikirim (terhapus otomatis 90 detik)."


async def run_search(chat_id, kind, q, by=None):
    """Cari lalu tampilkan hasil. Mengembalikan catatan (HTML) kalau tidak ada hasil untuk dipilih."""
    err = need_vc()
    if err:
        return err
    await panel_to(chat_id, "busy")
    video = kind == "vplay"
    if q.startswith("http"):
        if is_playlist_url(q):
            try:
                items = await asyncio.to_thread(expand_playlist, q, PLAYLIST_MAX)
            except Exception as ex:
                return f"❌ Gagal membaca playlist.\n<i>{esc(cut(ex, 150))}</i>"
            if not items:
                return "❌ Playlist kosong atau tidak terbaca."
            if video:
                for it in items:
                    it["video"] = True
            return await do_enqueue_many(items, by) + f" <i>(maks {PLAYLIST_MAX})</i>"
        src = "SoundCloud" if "soundcloud" in q else "YouTube"
        item = {"page": q, "title": q, "source": src}
        if video:
            item["video"] = True
        song, e = await asyncio.to_thread(resolve_song, item)
        return await do_enqueue(song, by=by) if song else f"❌ Gagal ambil.\n<i>{esc(cut(e, 150))}</i>"
    if kind == "fast":
        results, e = await asyncio.to_thread(search_candidates, q)
        if not results:
            return f"❌ {esc(cut(e, 200))}"
        song, e = await asyncio.to_thread(resolve_song, results[0])
        return await do_enqueue(song, by=by) if song else f"❌ Gagal ambil.\n<i>{esc(cut(e, 150))}</i>"
    e = ""
    if video:
        try:
            results = await asyncio.to_thread(_flat_search, "ytsearch", "YouTube", q, MAX_RESULTS)
        except Exception as ex:
            results, e = None, str(ex)
        if not results:
            return f"❌ Video tidak ditemukan.\n<i>{esc(cut(e, 120))}</i>"
        for r in results:
            r["video"] = True
    else:
        results, e = await asyncio.to_thread(search_candidates, q)
        if not results:
            return f"❌ {esc(cut(e, 200))}"
    await delete_panel(chat_id)
    await show_results(chat_id, q, results, video)
    return None


# ==========================================
# PERINTAH KETIK (alternatif, hasilnya tetap lewat panel)
# ==========================================
def arg_of(event):
    parts = (event.raw_text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def deny(event):
    await event.reply(
        f"⛔ <b>Kamu belum jadi admin.</b>\n\nID kamu: <code>{event.sender_id}</code>",
        buttons=[[B("🙋 Minta akses ke owner", "req")]],
    )


def cmd(pattern, owner=False):
    def deco(fn):
        async def wrapper(event):
            uid = event.sender_id
            if is_owner(uid) or (not owner and is_admin(uid)):
                return await fn(event)
            if is_admin(uid):
                return await event.reply("⛔ Perintah ini khusus owner.")
            await deny(event)

        manager_bot.on(events.NewMessage(pattern=pattern, func=lambda e: e.is_private))(wrapper)
        return wrapper
    return deco


async def search_cmd(event, kind):
    chat = event.chat_id
    q = arg_of(event)
    await cleanup_cmd(event)
    if not q:
        err = need_vc()
        if err:
            return await bring_panel(chat, "main", err)
        WAIT[chat] = kind
        return await bring_panel(chat, "input")
    await bring_panel(chat, "busy")
    notice = await run_search(chat, kind, q, by_name(event.sender_id))
    if notice:
        await panel_to(chat, "main", notice)


@cmd(r"^/(start|panel|menu)(?:\s|$)")
async def c_start(event):
    WAIT.pop(event.chat_id, None)
    await cmd_done(event, view="main")


@cmd(r"^/np(?:\s|$)")
async def c_np(event):
    await cmd_done(event, view="np")


@cmd(r"^/queue(?:\s|$)")
async def c_queue(event):
    await cmd_done(event, view="queue")


@cmd(r"^/settings(?:\s|$)")
async def c_settings(event):
    await cmd_done(event, view="settings")


@cmd(r"^/admins(?:\s|$)")
async def c_admins(event):
    await cmd_done(event, view="admins")


@cmd(r"^/help(?:\s|$)")
async def c_help(event):
    await cmd_done(event, view="help")


@cmd(r"^/status(?:\s|$)")
async def c_status(event):
    await cmd_done(event, view="status")


@cmd(r"^/play(?:\s|$)")
async def c_play(event):
    await search_cmd(event, "play")


@cmd(r"^/fast(?:\s|$)")
async def c_fast(event):
    await search_cmd(event, "fast")


@cmd(r"^/vplay(?:\s|$)")
async def c_vplay(event):
    await search_cmd(event, "vplay")


@cmd(r"^/setvc(?:\s|$)")
async def c_setvc(event):
    chat = event.chat_id
    raw = arg_of(event)
    await cleanup_cmd(event)
    if not raw:
        if not userbot_app:
            return await bring_panel(chat, "main", NO_USERBOT)
        WAIT[chat] = "setvc"
        return await bring_panel(chat, "input")
    await bring_panel(chat, "busy")
    await panel_to(chat, "vc", await do_setvc(raw))


@cmd(r"^/unsetvc(?:\s|$)")
async def c_unsetvc(event):
    await cmd_done(event, await do_unsetvc(), view="vc")


@cmd(r"^/addadmin(?:\s|$)", owner=True)
async def c_addadmin(event):
    chat = event.chat_id
    raw = arg_of(event)
    await cleanup_cmd(event)
    if not raw:
        WAIT[chat] = "addadmin"
        return await bring_panel(chat, "input")
    await bring_panel(chat, "admins", await do_addadmin(raw))


@cmd(r"^/deladmin(?:\s|$)", owner=True)
async def c_deladmin(event):
    a = arg_of(event)
    if not a.lstrip("-").isdigit() or str(int(a)) not in DATA["admins"]:
        return await cmd_done(event, "Cara pakai: /deladmin ID (lihat daftar di panel Admin).", view="admins")
    name = DATA["admins"].pop(str(int(a)))
    await save_data()
    await cmd_done(event, f"🗑 {esc(name)} dihapus dari admin.", view="admins")


@cmd(r"^/getsession(?:\s|$)", owner=True)
async def c_getsession(event):
    note = await send_session(event.chat_id)
    await cmd_done(event, note, view="vc")


@cmd(r"^/fav(?:\s|$)")
async def c_fav(event):
    await cmd_done(event, view="fav")


@cmd(r"^/history(?:\s|$)")
async def c_history(event):
    await cmd_done(event, view="hist")


@cmd(r"^/save(?:\s|$)")
async def c_save(event):
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    item = mk_item(cur) if cur and cur.get("title") != "Silent" else None
    if not item:
        return await cmd_done(event, "❌ Tidak ada lagu yang bisa disimpan.")
    note = "⭐ Disimpan ke favorit." if fav_add(event.sender_id, item) else "ℹ️ Sudah ada di favorit."
    await cmd_done(event, note)


@cmd(r"^/volume(?:\s|$)")
async def c_volume(event):
    a = arg_of(event)
    if not a.isdigit():
        return await cmd_done(event, f"🔊 Volume sekarang {VOLUME}%. Ubah: /volume 0-200")
    await cmd_done(event, await act_volume(value=int(a)))


@cmd(r"^/sleep(?:\s|$)")
async def c_sleep(event):
    a = arg_of(event).lower()
    if a in ("off", "0", "mati"):
        return await cmd_done(event, set_sleep(0), view="settings")
    if not a.isdigit():
        return await cmd_done(event, "Cara pakai: /sleep 30 (menit) atau /sleep off", view="settings")
    await cmd_done(event, set_sleep(min(int(a), 600)), view="settings")


@cmd(r"^/pause(?:\s|$)")
async def c_pause(event):
    await cmd_done(event, await act_pause())


@cmd(r"^/resume(?:\s|$)")
async def c_resume(event):
    await cmd_done(event, await act_resume())


@cmd(r"^/skip(?:\s|$)")
async def c_skip(event):
    await cmd_done(event, await act_skip())


@cmd(r"^/stop(?:\s|$)")
async def c_stop(event):
    await cmd_done(event, await act_stop())


@cmd(r"^/loop(?:\s|$)")
async def c_loop(event):
    await cmd_done(event, await act_loop())


@cmd(r"^/shuffle(?:\s|$)")
async def c_shuffle(event):
    await cmd_done(event, await act_shuffle())


@cmd(r"^/clear(?:\s|$)")
async def c_clear(event):
    await cmd_done(event, await act_clear())


@cmd(r"^/remove(?:\s|$)")
async def c_remove(event):
    a = arg_of(event)
    if not a.isdigit():
        return await cmd_done(event, "Cara pakai: /remove 2", view="queue")
    await cmd_done(event, await act_remove(int(a)), view="queue")


@cmd(r"^/filter(?:\s|$)")
async def c_filter(event):
    await cmd_done(event, await act_filter())


@cmd(r"^/hemat(?:\s|$)")
async def c_hemat(event):
    await cmd_done(event, await act_hemat(arg_of(event)), view="settings")


@cmd(r"^/quality(?:\s|$)")
async def c_quality(event):
    await cmd_done(event, await act_quality(arg_of(event)), view="settings")


@cmd(r"^/amode(?:\s|$)")
async def c_amode(event):
    global AUDIO_PLAIN
    AUDIO_PLAIN = not AUDIO_PLAIN
    await cmd_done(event, f"🔊 Mode audio: {'BIASA' if AUDIO_PLAIN else 'AUDIO-ONLY'}. Putar ulang untuk mencoba.")


# ==========================================
# TEKS BIASA (jawaban untuk tombol, atau langsung cari lagu)
# ==========================================
@manager_bot.on(events.NewMessage(
    func=lambda e: e.is_private and not e.document and (e.raw_text or "").strip()
    and not e.raw_text.startswith("/")))
async def on_text(event):
    uid = event.sender_id
    if not is_admin(uid):
        return await deny(event)
    chat = event.chat_id
    text = event.raw_text.strip()
    kind = WAIT.pop(chat, None) or "play"
    await cleanup_cmd(event)

    if kind == "addadmin" and not is_owner(uid):
        return await panel_to(chat, "main", "⛔ Khusus owner.")
    if kind == "setvc":
        await panel_to(chat, "busy")
        return await panel_to(chat, "vc", await do_setvc(text))
    if kind == "addadmin":
        return await panel_to(chat, "admins", await do_addadmin(text))

    notice = await run_search(chat, kind, text, by_name(uid))
    if notice:
        await panel_to(chat, "main", notice)


# ==========================================
# TOMBOL (CALLBACK)
# ==========================================
ACTIONS = {"a:pause": act_pause, "a:resume": act_resume, "a:skip": act_skip,
           "a:stop": act_stop, "a:loop": act_loop, "a:filter": act_filter,
           "a:shuf": act_shuffle, "a:clr": act_clear}


@manager_bot.on(events.CallbackQuery())
async def on_callback(event):
    data = event.data.decode("utf-8", "ignore")
    uid = event.sender_id
    chat = event.chat_id

    if data == "noop":
        return await event.answer()

    # --- minta akses admin (boleh non-admin) ---
    if data == "req":
        if is_admin(uid):
            return await event.answer("Kamu sudah admin.", alert=True)
        if uid in ASKED:
            return await event.answer("Permintaan sudah dikirim, tunggu owner.", alert=True)
        sender = await event.get_sender()
        name = utils.get_display_name(sender) or f"ID {uid}"
        uname = f"@{sender.username}" if getattr(sender, "username", None) else "-"
        ASKED.add(uid)
        REQUESTS[uid] = name
        try:
            await manager_bot.send_message(
                OWNER_ID,
                "🙋 <b>Permintaan akses admin</b>\n\n"
                f"<blockquote>Nama: {esc(name)}\nUsername: {esc(uname)}\nID: <code>{uid}</code></blockquote>",
                buttons=[[B("✅ Setujui", f"appr:{uid}"), B("❌ Tolak", f"rej:{uid}")]],
            )
        except Exception as e:
            ASKED.discard(uid)
            return await event.answer(f"Gagal kirim ke owner: {e}"[:190], alert=True)
        return await event.answer("✅ Permintaan dikirim ke owner.", alert=True)

    if not is_admin(uid):
        return await event.answer("⛔ Kamu bukan admin.", alert=True)

    # --- khusus owner: setujui / tolak / hapus admin ---
    if data.startswith(("appr:", "rej:", "deladm:")):
        if not is_owner(uid):
            return await event.answer("⛔ Khusus owner.", alert=True)
        kind, target = data.split(":", 1)
        tid = int(target)
        if kind == "appr":
            name = REQUESTS.pop(tid, f"ID {tid}")
            DATA["admins"][str(tid)] = name
            ASKED.discard(tid)
            await save_data()
            try:
                await event.edit(f"✅ <b>{esc(name)}</b> (<code>{tid}</code>) disetujui jadi admin.")
            except Exception:
                pass
            try:
                await manager_bot.send_message(tid, "✅ Kamu sekarang admin Music Bot. Ketik /start.")
            except Exception:
                pass
        elif kind == "rej":
            REQUESTS.pop(tid, None)
            ASKED.discard(tid)
            try:
                await event.edit(f"❌ Permintaan <code>{tid}</code> ditolak.")
            except Exception:
                pass
            try:
                await manager_bot.send_message(tid, "❌ Permintaan akses admin ditolak owner.")
            except Exception:
                pass
        else:
            name = DATA["admins"].pop(str(tid), None)
            await save_data()
            await event.answer(f"Dihapus: {name}" if name else "Tidak ditemukan.")
            await show(event, "admins")
        return

    # --- input lewat tombol (cari, nonton, set VC, tambah admin) ---
    if data.startswith("i:"):
        kind = data[2:]
        if kind == "cancel":
            WAIT.pop(chat, None)
            await event.answer()
            return await show(event, "main")
        if kind == "again":
            st = PENDING.pop(chat, None)
            kind = "vplay" if (st and st["video"]) else "play"
            await event.answer()
            try:
                await manager_bot.delete_messages(chat, [event.query.msg_id])
            except Exception:
                pass
            WAIT[chat] = kind
            return await bring_panel(chat, "input")
        if kind in ("play", "vplay", "fast"):
            err = need_vc()
            if err:
                return await event.answer(toast(err), alert=True)
        elif kind == "setvc":
            if not userbot_app:
                return await event.answer(toast(NO_USERBOT), alert=True)
        elif kind == "addadmin":
            if not is_owner(uid):
                return await event.answer("⛔ Khusus owner.", alert=True)
        else:
            return await event.answer()
        WAIT[chat] = kind
        await event.answer()
        return await show(event, "input")

    if data == "home":
        await event.answer()
        st = PENDING.pop(chat, None)
        try:
            await manager_bot.delete_messages(chat, [event.query.msg_id])
        except Exception:
            pass
        return await bring_panel(chat, "main")

    # --- favorit / riwayat ---
    if data.startswith("l:"):
        parts = data.split(":")
        op, kind = parts[1], parts[2]
        if kind not in ("fav", "hist"):
            return await event.answer()
        items = list_data(kind, uid)
        if op == "pg":
            LISTPAGE[(uid, kind)] = LISTPAGE.get((uid, kind), 0) + int(parts[3])
            await event.answer()
            return await show(event, kind)
        if op in ("play", "all"):
            err = need_vc()
            if err:
                return await event.answer(toast(err), alert=True)
            picked = items[:PLAYLIST_MAX] if op == "all" else [x for x in items if iid(x) == parts[3]]
            if not picked:
                return await event.answer("Tidak ditemukan.", alert=True)
            await event.answer("⏳ Menambahkan…")
            NOTICE[chat] = await do_enqueue_many(picked, by_name(uid))
            return await show(event, kind)
        if op == "save":
            it = next((x for x in items if iid(x) == parts[3]), None)
            added = bool(it) and fav_add(uid, it)
            await event.answer("⭐ Disimpan ke favorit" if added else "Sudah ada di favorit")
            return await show(event, kind)
        if op == "del":
            await event.answer("🗑 Dihapus" if fav_del(uid, parts[3]) else "Tidak ditemukan")
            return await show(event, kind)
        return await event.answer()

    # --- simpan lagu / volume / timer tidur ---
    if data == "n:save":
        cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
        item = mk_item(cur) if cur and cur.get("title") != "Silent" else None
        if not item:
            return await event.answer("Tidak ada lagu yang bisa disimpan.", alert=True)
        await event.answer("⭐ Disimpan ke favorit" if fav_add(uid, item) else "Sudah ada di favorit")
        return await show(event, VIEW.get(chat, "np"))
    if data.startswith("n:vol:"):
        await event.answer(toast(await act_volume(delta=int(data[6:]))))
        return await show(event, "np")
    if data.startswith("t:"):
        await event.answer(toast(set_sleep(int(data[2:]))))
        return await show(event, "settings")

    # --- hasil pencarian ---
    if data.startswith("pk:"):
        return await handle_pick(event, int(data[3:]))
    if data.startswith("pg:"):
        st = PENDING.get(chat)
        if not st:
            return await event.answer("Daftar kedaluwarsa, cari lagi.", alert=True)
        st["page"] = min(max(0, st["page"] + int(data[3:])), total_pages(st) - 1)
        await event.answer()
        return await rerender_results(event, st, new_image=True)
    if data == "nowtoggle":
        st = PENDING.get(chat)
        if not st:
            return await event.answer("Daftar kedaluwarsa, cari lagi.", alert=True)
        st["now"] = not st["now"]
        await event.answer("⏩ Langsung putar" if st["now"] else "➕ Masuk antrean")
        return await rerender_results(event, st, new_image=False)

    # --- pindah tampilan ---
    if data.startswith("v:"):
        await event.answer()
        return await show(event, data[2:])

    # --- aksi musik dan setelan ---
    if data in ACTIONS:
        text = await ACTIONS[data]()
        await event.answer(toast(text))
        return await show(event, VIEW.get(chat, "main"))
    if data == "a:hemat":
        await event.answer(toast(await act_hemat("")))
        return await show(event, "settings")
    if data.startswith("q:"):
        await event.answer(toast(await act_quality(data[2:])))
        return await show(event, "settings")
    if data.startswith("rm:"):
        await event.answer(toast(await act_remove(int(data[3:]))))
        return await show(event, "queue")

    # --- VC (admin & owner) dan session (owner) ---
    if data == "a:unsetvc":
        await event.answer(toast(await do_unsetvc()))
        return await show(event, "vc")
    if data == "a:session":
        if not is_owner(uid):
            return await event.answer("⛔ Khusus owner.", alert=True)
        await event.answer(toast(await send_session(chat)))
        return await show(event, "vc")
    await event.answer()


# ==========================================
# UPLOAD SESSION (OWNER)
# ==========================================
@manager_bot.on(events.NewMessage(func=lambda e: e.is_private and e.document))
async def on_document(event):
    if event.sender_id != OWNER_ID:
        return
    name = ""
    for a in event.document.attributes:
        if hasattr(a, "file_name"):
            name = a.file_name or ""
    if not name.lower().endswith(".zip"):
        return await event.reply("❌ Kirim file <code>.zip</code> yang berisi file <code>.session</code>.")

    msg = await event.reply("⏳ <i>Memproses file…</i>")
    zip_path = await event.download_media()
    try:
        extract_dir = "/tmp/session_extract"
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)
        os.remove(zip_path)

        found = glob.glob(f"{extract_dir}/**/*.session", recursive=True)
        if not found:
            return await msg.edit("❌ File <code>.session</code> tidak ditemukan di dalam zip!")
        for f in glob.glob("/tmp/userbot_session.session*"):
            os.remove(f)
        shutil.move(found[0], "/tmp/userbot_session.session")

        await msg.edit("✅ Session ditemukan.\n⏳ <i>Menyalakan userbot…</i>")
        await boot_userbot("/tmp/userbot_session")
        try:
            await msg.delete()
            await event.delete()
        except Exception:
            pass
        note = await restore_state()
        await bring_panel(
            event.chat_id, "vc",
            "🚀 <b>Userbot online!</b> " + (note + " " if note else "Tekan 📡 Pilih / Ganti VC. ")
            + "Tips: tekan 🔑 Session lalu simpan di Secrets SESSION_STRING agar tidak perlu upload zip lagi.")
    except Exception as e:
        await msg.edit(f"❌ <b>Gagal</b>\n<code>{esc(cut(e, 300))}</code>")


# ==========================================
# JALANKAN
# ==========================================
BOT_COMMANDS = [
    ("start", "Buka panel"), ("play", "Cari & putar lagu / playlist"), ("fast", "Putar hasil teratas"),
    ("vplay", "Nonton (share screen)"), ("np", "Sedang diputar"), ("queue", "Lihat antrean"),
    ("pause", "Jeda"), ("resume", "Lanjutkan"), ("skip", "Lewati lagu"), ("stop", "Hentikan"),
    ("loop", "Ulang lagu"), ("shuffle", "Acak antrean"), ("clear", "Kosongkan antrean"),
    ("remove", "Hapus nomor dari antrean"), ("volume", "Atur volume 0-200"),
    ("sleep", "Timer tidur (menit)"), ("fav", "Daftar favorit"), ("save", "Simpan lagu ke favorit"),
    ("history", "Riwayat lagu"), ("settings", "Menu setelan"),
    ("filter", "Filter lagu on/off"), ("hemat", "Mode hemat kuota"),
    ("quality", "Kualitas video"), ("admins", "Daftar admin"), ("status", "Status bot"),
    ("help", "Bantuan lengkap"), ("setvc", "Pilih / ganti VC"), ("unsetvc", "Keluar VC"),
    ("addadmin", "Owner: tambah admin"), ("deladmin", "Owner: hapus admin"),
    ("getsession", "Owner: ambil SESSION_STRING"),
]


async def register_commands():
    try:
        await manager_bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]))
    except Exception as e:
        print(f"Gagal pasang menu perintah: {e}")


async def notify_owner(text):
    try:
        await bring_panel(OWNER_ID, "main", text)
    except Exception as e:
        print(f"Gagal kirim notifikasi ke owner: {e}")


async def main():
    print("Memulai Manager Bot...")
    await manager_bot.start(bot_token=BOT_TOKEN)
    print("✅ Manager Bot siap!")
    await register_commands()
    if SESSION_STRING:
        try:
            await boot_userbot(StringSession(SESSION_STRING))
            note = await restore_state()
            await notify_owner("🚀 <b>Bot restart.</b> Userbot online otomatis. "
                               + (note if note else "Pilih VC lewat tombol 📡 VC."))
        except Exception as e:
            await notify_owner(f"❌ <b>Auto-start userbot gagal</b>\n<code>{esc(cut(e, 200))}</code>\n"
                               "Kirim file zip session secara manual.")
    else:
        await notify_owner("🤖 <b>Bot nyala.</b> Kirim file zip session untuk menyalakan userbot.")
    await manager_bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot dihentikan.")