import asyncio
import glob
import json
import os
import random
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile

import yt_dlp
from bs4 import BeautifulSoup
from telethon import TelegramClient, events, utils, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.sessions import MemorySession, StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, GroupCallConfig

# curl_cffi menyamar sebagai Chrome (menghindari 403 dari LK21).
# Kalau belum terpasang, otomatis pakai requests biasa.
try:
    from curl_cffi import requests as web
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests as web
    IMPERSONATE = {}

# ==========================================
# KONFIGURASI (semua rahasia WAJIB lewat GitHub Secrets / env)
# ==========================================
def _env(name, default=""):
    return os.environ.get(name) or default


def _need(name):
    v = _env(name)
    if not v:
        raise SystemExit(f"❌ Env {name} belum diisi (isi di GitHub Secrets).")
    return v


API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
BOT_TOKEN = _need("BOT_TOKEN")
OWNER_ID = int(_need("OWNER_ID"))
SESSION_STRING = _env("SESSION_STRING")  # opsional: userbot otomatis nyala tanpa upload zip

# Domain LK21 (ganti lewat Secret LK21_BASE_URL kalau domain berubah)
LK21_BASE_URL = _env("LK21_BASE_URL", "https://tv12.lk21official.cc").rstrip("/")

SILENT_AUDIO = "https://github.com/anars/blank-audio/raw/master/1-second-of-silence.mp3"
COOKIES = "cookies.txt"  # opsional (ditulis dari Secret YT_COOKIES oleh workflow)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

TMP_DIR = tempfile.gettempdir()
SESSION_BASE = os.path.join(TMP_DIR, "userbot_session")

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
PENDING = {}        # chat_id admin -> {"results": [...], "mids": [...]}
REQUESTS = {}       # uid -> nama (permintaan akses admin)
ASKED = set()
PLAY_ERRORS = {}
DATA = {"admins": {}}   # admins: {"id": "nama"}
DATA_DIRTY = False
DATA_TAG = "MUSICBOT_DATA "

# Cinema
CINEMA_ITEMS = {}
CINEMA_VIEW = {}


def get_queue(chat_id):
    return queues.setdefault(chat_id, [])


def silent_song():
    return {"url": SILENT_AUDIO, "title": "Silent", "duration": 0}


def clean(text):
    return re.sub(r"[*_\[\]~`]", "", str(text or ""))


def fmt_dur(sec):
    if not sec:
        return "?"
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


def quality_label():
    return "suara saja" if VIDEO_HEIGHT == 0 else f"{VIDEO_HEIGHT}p"


def is_owner(uid):
    return uid == OWNER_ID


def is_admin(uid):
    return uid == OWNER_ID or str(uid) in DATA["admins"]


# ==========================================
# PENYIMPANAN (di Saved Messages akun userbot, tahan restart)
# ==========================================
def snapshot():
    return {
        "admins": DATA["admins"], "filter_on": FILTER_ON, "video_height": VIDEO_HEIGHT,
        "hemat_on": HEMAT_ON, "prev_height": PREV_HEIGHT, "loop": LOOP,
    }


async def save_data():
    global DATA_DIRTY
    DATA_DIRTY = True
    if not userbot_app:
        return
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
    if not userbot_app:
        return
    try:
        msgs = await userbot_app.get_messages("me", search=DATA_TAG.strip(), limit=10)
        mine = next((m for m in msgs if m.out and (m.raw_text or "").startswith(DATA_TAG)), None)
        if not mine:
            return
        stored = json.loads(mine.raw_text[len(DATA_TAG):])
    except Exception as e:
        print(f"Gagal baca data: {e}")
        return
    DATA["admins"] = {**stored.get("admins", {}), **DATA["admins"]}
    if not DATA_DIRTY:
        FILTER_ON = stored.get("filter_on", FILTER_ON)
        VIDEO_HEIGHT = stored.get("video_height", VIDEO_HEIGHT)
        HEMAT_ON = stored.get("hemat_on", HEMAT_ON)
        PREV_HEIGHT = stored.get("prev_height", PREV_HEIGHT)
        LOOP = stored.get("loop", LOOP)


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


def _mk_stream(url, headers=None, **kw):
    if headers:
        try:
            return MediaStream(url, headers=headers, **kw)
        except TypeError:
            pass
    return MediaStream(url, **kw)


def video_stream(url, headers=None):
    for q in _video_quality_candidates():
        try:
            return _mk_stream(url, headers, video_parameters=q)
        except Exception:
            continue
    return _mk_stream(url, headers)


def make_stream(song):
    url = song["url"]
    if song.get("force_audio"):
        return audio_stream(url)
    if song.get("video"):
        return video_stream(url, song.get("headers"))
    if AUDIO_PLAIN:
        return MediaStream(url)
    return audio_stream(url)


async def call_any(names, *args, **kwargs):
    for n in names:
        fn = getattr(userbot_call, n, None)
        if fn is not None:
            return await fn(*args, **kwargs)
    raise AttributeError(f"Method {names} tidak ada di versi py-tgcalls ini")


async def probe_duration(url, headers=None):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd.append(url)
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 25)
        return int(float(out.decode().strip()))
    except Exception:
        if p and p.returncode is None:
            try:
                p.kill()
            except Exception:
                pass
        return 0


# ==========================================
# PENCARIAN, FILTER, AMBIL LINK (yt-dlp, multi-platform)
# ==========================================
DIRECT_STREAM_RE = re.compile(r"^https?://[^\s]+\.(?:m3u8|mp4|webm|mkv|mov|flv|ts)(?:\?|$)", re.I)


def detect_source(url):
    low = (url or "").lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "YouTube"
    if "soundcloud.com" in low:
        return "SoundCloud"
    if "vimeo.com" in low:
        return "Vimeo"
    if DIRECT_STREAM_RE.match(url or ""):
        return "Direct"
    return "Web"


def _opts(use_cookies, video=False):
    h = VIDEO_HEIGHT or 480
    if video:
        # satu file yang sudah berisi video+suara (ffmpeg hanya menerima 1 URL)
        fmt = f"best[height<={h}][vcodec!=none][acodec!=none]/best[height<={h}]/best"
    else:
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
    opts = {
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if use_cookies and os.path.exists(COOKIES):
        opts["cookiefile"] = COOKIES
    return opts


def _flat_search(prefix, label, query, n):
    opts = _opts(use_cookies=(prefix == "ytsearch"))
    opts["extract_flat"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    out = []
    for v in (info or {}).get("entries") or []:
        if not v:
            continue
        url = v.get("url") or v.get("webpage_url")
        if prefix == "ytsearch" and v.get("id") and not (url or "").startswith("http"):
            url = f"https://www.youtube.com/watch?v={v['id']}"
        if not url:
            continue
        thumb = None
        if prefix == "ytsearch" and v.get("id"):
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
            n = 15 if FILTER_ON else MAX_RESULTS
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
    if not url and v.get("formats"):
        best = max((f for f in v["formats"] if f.get("url")),
                   key=lambda x: x.get("tbr") or x.get("abr") or 0, default=None)
        if best:
            url = best["url"]
    if not url:
        raise RuntimeError("Link stream tidak tersedia.")
    return {
        "url": url,
        "page": v.get("webpage_url") or v.get("original_url"),
        "title": v.get("title", "Unknown"),
        "duration": int(v.get("duration") or 0),
        "source": label,
        "channel": v.get("channel") or v.get("uploader") or "",
        "thumb": v.get("thumbnail"),
        "video": video,
        "fmt": f"{v.get('ext', '?')}/{v.get('protocol', '?')}",
        "headers": v.get("http_headers") if label == "Web" else None,
    }


def extract_stream(page_url, video=False):
    label = detect_source(page_url)
    if label == "Direct" or DIRECT_STREAM_RE.match(page_url):
        return {"url": page_url, "page": page_url, "title": "Direct Stream", "duration": 0,
                "source": "Direct", "video": video}, None
    try:
        with yt_dlp.YoutubeDL(_opts(label == "YouTube", video)) as ydl:
            info = ydl.extract_info(page_url, download=False)
        if info.get("entries"):
            info = info["entries"][0]
        return _song_from_info(info, label, video), None
    except Exception as e:
        print(f"Error resolve [{label}]: {e}")
        return None, str(e)[:200]


def resolve_song(item):
    page = item.get("page") or item.get("url")
    if not page:
        return None, "URL tidak valid."
    video = bool(item.get("video"))
    audio_movie = video and VIDEO_HEIGHT == 0   # mode "audio": film cuma suaranya
    if audio_movie:
        video = False

    song, err = extract_stream(page, video=video)

    # Cadangan: kalau YouTube gagal, cari judul yang sama di SoundCloud (khusus audio)
    title = item.get("title") or ""
    if not song and not item.get("video") and detect_source(page) == "YouTube" \
            and title and not title.startswith("http"):
        try:
            alt = _flat_search("scsearch", "SoundCloud", title, 3)
            if alt:
                song, e2 = extract_stream(alt[0]["page"])
                err = None if song else f"{err}\nSoundCloud: {e2}"
            else:
                err = f"{err}\nSoundCloud: tidak ada hasil"
        except Exception as e:
            print(f"Error fallback SC: {e}")
            err = f"{err}\nSoundCloud: {str(e)[:120]}"

    if song:
        song["force_audio"] = audio_movie
        if item.get("keep_title") and title:
            song["title"] = title
        else:
            song.setdefault("title", title or "Unknown")
        if not song.get("duration"):
            song["duration"] = int(item.get("duration") or 0)
        song["thumb"] = song.get("thumb") or item.get("thumb")
        song["channel"] = song.get("channel") or item.get("channel", "")
        song["page"] = page
    return song, err


# ==========================================
# LK21 CINEMA (web scraping)
# ==========================================
class CinemaError(Exception):
    pass


def _get(url, referer=None, timeout=15):
    headers = {"Accept-Language": "id,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    if not IMPERSONATE:
        headers["User-Agent"] = UA
    return web.get(url, headers=headers, timeout=timeout, **IMPERSONATE)


def _fetch_lk21(url):
    try:
        resp = _get(url, referer=LK21_BASE_URL + "/")
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        raise CinemaError(f"Website Error: {e}")


def _scrape_stream(page_url, depth=2, seen=None, referer=None):
    """Cadangan: cari .m3u8 / .mp4 langsung di HTML / iframe."""
    seen = seen if seen is not None else set()
    if page_url in seen or len(seen) > 8:
        return None
    seen.add(page_url)
    try:
        text = _get(page_url, referer=referer or LK21_BASE_URL + "/").text
    except Exception:
        return None

    media = re.findall(r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>\\]*)?", text, re.I)
    if media:
        m3 = [m for m in media if ".m3u8" in m.lower()]
        return (m3 or media)[0], page_url

    if depth <= 0:
        return None

    iframes = re.findall(r"""<iframe[^>]+(?:src|data-src)=["']([^"']+)["']""", text, re.I)
    for c in iframes:
        link = urllib.parse.urljoin(page_url, c.strip())
        if not link.startswith("http") or re.search(r"youtube|youtu\.be|ads", link, re.I):
            continue
        res = _scrape_stream(link, depth - 1, seen, referer=page_url)
        if res:
            return res
    return None


def _parse_search(soup):
    results, seen = [], set()

    def add(a, title, poster):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            return
        link = urllib.parse.urljoin(LK21_BASE_URL + "/", href)
        if urllib.parse.urlparse(link).path.strip("/") == "" or link in seen:
            return
        title = (title or "").strip()
        if not title:
            return
        seen.add(link)
        results.append({"title": clean(title), "link": link, "poster": poster})

    for art in soup.find_all("article"):
        a = art.find("a", href=True)
        if not a:
            continue
        t = art.find(["h3", "h2"])
        img = art.find("img")
        title = t.get_text(" ", strip=True) if t else (
            (img.get("alt") if img else "") or a.get("title") or "")
        add(a, title, img.get("src") if img else None)

    if not results:  # cadangan kalau struktur halaman bukan <article>
        for a in soup.find_all("a", href=True):
            img = a.find("img")
            if not img:
                continue
            t = a.find(["h3", "h2"])
            title = t.get_text(" ", strip=True) if t else (img.get("alt") or a.get("title") or "")
            add(a, title, img.get("src"))
    return results


def cinema_home_buttons():
    return [
        [Button.inline("🔎 Cara mencari", b"cin:search")],
        [Button.inline("🔄 Refresh", b"cin:home")],
    ]


def cinema_home_text():
    return ("🎬 **LK21 CINEMA CENTER**\n━━━━━━━━━━━━━━━━━━\n"
            "Ketik `/csearch judul film` untuk mencari.\n\n"
            "_(Gunakan tombol di bawah untuk navigasi)_")


async def cinema_edit(target, text, buttons=None):
    try:
        return await target.edit(text, buttons=buttons, link_preview=False)
    except Exception:
        if hasattr(target, "reply"):
            return await target.reply(text, buttons=buttons, link_preview=False)
        raise


async def cinema_search_list(target, chat_id, query):
    search_url = f"{LK21_BASE_URL}/search?s={urllib.parse.quote_plus(query)}"
    try:
        soup = await asyncio.to_thread(_fetch_lk21, search_url)
    except Exception as e:
        return await cinema_edit(target, f"❌ Gagal mencari: {clean(str(e))}",
                                 [[Button.inline("🏠 Cinema", b"cin:home")]])

    results = _parse_search(soup)
    if not results:
        print(f"[cinema] 0 hasil dari {search_url} (panjang html {len(str(soup))})")
        return await cinema_edit(
            target, f"🎬 **Hasil: {clean(query)}**\n\nTidak ada film ditemukan.",
            [[Button.inline("🏠 Cinema", b"cin:home")]])

    CINEMA_ITEMS[chat_id] = results[:10]
    CINEMA_VIEW[chat_id] = {"query": query, "type": "search"}

    lines = [f"🎬 **Hasil Pencarian: {clean(query)}**", "━━━━━━━━━━━━━━━━━━"]
    rows = []
    for i, item in enumerate(CINEMA_ITEMS[chat_id]):
        lines.append(f"**{i + 1}. {item['title']}**")
        rows.append([Button.inline(f"{i + 1}. {item['title'][:35]}", f"cin:detail:{i}".encode())])
    rows.append([Button.inline("🏠 Cinema", b"cin:home")])
    lines.append("\nPilih judul untuk melihat detail.")
    return await cinema_edit(target, "\n".join(lines), rows)


async def cinema_detail(target, chat_id, index):
    items = CINEMA_ITEMS.get(chat_id, [])
    if not (0 <= index < len(items)):
        raise CinemaError("Daftar kedaluwarsa.")
    item = items[index]
    try:
        soup = await asyncio.to_thread(_fetch_lk21, item["link"])
    except Exception as e:
        return await cinema_edit(target, f"❌ Gagal membuka detail: {clean(str(e))}",
                                 [[Button.inline("⬅️ Kembali", b"cin:back"),
                                   Button.inline("🏠 Cinema", b"cin:home")]])

    synopsis_tag = soup.find("div", class_="synopsis") or soup.find("div", class_="entry-content")
    synopsis = synopsis_tag.get_text(" ", strip=True) if synopsis_tag else "Sinopsis belum tersedia."
    if len(synopsis) > 400:
        synopsis = synopsis[:400] + "..."

    lines = [f"🎬 **{clean(item['title'])}**", "━━━━━━━━━━━━━━━━━━",
             f"**Sinopsis:**\n{clean(synopsis)}"]

    buttons = []
    meta_redirect = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    content = (meta_redirect.get("content", "") if meta_redirect else "")
    if "url=" in content.lower():
        target_url = re.split(r"url=", content, flags=re.I)[-1].strip().strip("'\"")
        target_url = urllib.parse.urljoin(LK21_BASE_URL + "/", target_url)
        buttons.append(Button.url("🌐 Buka di Nontondrama", target_url))
        lines.append("\n*(Ini adalah Series. Silakan buka link di atas untuk memilih episode)*")
    else:
        stream_url = None
        for f in soup.find_all("iframe"):
            src = f.get("src") or f.get("data-src") or ""
            if not src:
                continue
            s = urllib.parse.urljoin(item["link"], src)
            if s.startswith("http") and not re.search(r"youtube|youtu\.be|ads|doubleclick", s, re.I):
                stream_url = s
                break
        if stream_url:
            item["stream_url"] = stream_url
            buttons.append(Button.inline("▶ Putar di VC", f"cin:play:{index}".encode()))
            buttons.append(Button.url("🌐 Buka Player", stream_url))
        else:
            lines.append("\n❌ Link streaming tidak ditemukan di halaman ini.")
            buttons.append(Button.url("🌐 Buka Halaman", item["link"]))

    buttons.append(Button.inline("⬅️ Kembali", b"cin:back"))
    buttons.append(Button.inline("🏠 Cinema", b"cin:home"))
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return await cinema_edit(target, "\n".join(lines), keyboard)


async def cinema_play(event, chat_id, index):
    err = need_vc()
    if err:
        return await event.answer(toast(err), alert=True)

    items = CINEMA_ITEMS.get(chat_id, [])
    if not (0 <= index < len(items)) or "stream_url" not in items[index]:
        return await event.answer("Daftar kedaluwarsa, buka detail lagi.", alert=True)

    item = items[index]
    stream_url = item["stream_url"]
    await event.answer("⏳ Mengambil link video... tunggu sebentar")

    # Coba 1: yt-dlp (bisa menembus beberapa embed player)
    song, e = await asyncio.to_thread(
        resolve_song, {"page": stream_url, "title": item["title"], "source": "LK21",
                       "video": True, "keep_title": True})

    # Coba 2: scraper cadangan
    if not song:
        print(f"yt-dlp gagal untuk {stream_url}: {e}. Mencoba scraper cadangan...")
        res = await asyncio.to_thread(_scrape_stream, stream_url)
        if res:
            real_url, referer = res
            song = {
                "url": real_url, "page": stream_url, "title": item["title"],
                "duration": 0, "source": "LK21", "video": True,
                "force_audio": VIDEO_HEIGHT == 0,
                "headers": {"User-Agent": UA, "Referer": referer},
            }
        else:
            return await manager_bot.send_message(
                chat_id,
                f"❌ Gagal memutar video.\nError: {clean(e)}\n\n"
                "Coba tombol 'Buka Player' untuk menonton di browser.")

    song["duration"] = await probe_duration(song["url"], song.get("headers")) or song.get("duration", 0)
    text = await do_enqueue(song)
    await manager_bot.send_message(chat_id, text, buttons=ctrl_buttons())


async def cinema_callback(event):
    data = event.data.decode("utf-8", "ignore")
    chat_id = event.chat_id
    try:
        if data.startswith("cin:play:"):
            return await cinema_play(event, chat_id, int(data.split(":")[2]))
        await event.answer()
        if data == "cin:home":
            return await cinema_edit(event, cinema_home_text(), cinema_home_buttons())
        if data == "cin:search":
            return await cinema_edit(event, "🔎 Gunakan `/csearch judul film`",
                                     [[Button.inline("🏠 Cinema", b"cin:home")]])
        if data.startswith("cin:detail:"):
            return await cinema_detail(event, chat_id, int(data.split(":")[2]))
        if data == "cin:back":
            v = CINEMA_VIEW.get(chat_id, {})
            if v.get("type") == "search":
                return await cinema_search_list(event, chat_id, v.get("query", ""))
            return await cinema_edit(event, cinema_home_text(), cinema_home_buttons())
    except Exception as e:
        try:
            await event.edit(f"⚠️ Cinema: {clean(str(e))[:250]}",
                             buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])
        except Exception:
            pass


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
        try:
            await call_any(["unmute", "unmute_stream"], chat_id)
        except Exception:
            pass
        print(f"▶ Memutar: {song['title']} | {song.get('source')} {song.get('fmt')}")
        if song.get("duration"):
            schedule_next(chat_id, song, song["duration"] + 2)
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
    if song.get("video"):
        tag = " 🎬" + tag
    if now or idle:
        PLAY_ERRORS.pop(chat_id, None)
        q.insert(0, song)
        await play_next(chat_id)
        perr = PLAY_ERRORS.pop(chat_id, None)
        if perr:
            return f"❌ Gagal memutar {clean(song['title'])}: {clean(perr)}"
        return f"▶ **Memutar:** {clean(song['title'])}{tag}"
    q.append(song)
    return f"➕ **Antrean #{len(q)}:** {clean(song['title'])}{tag}"


# ==========================================
# AKSI KONTROL (dipakai perintah dan tombol)
# ==========================================
NO_USERBOT = "❌ Userbot belum online. Owner: kirim file zip session ke bot ini."
NO_VC = "❌ VC belum diset. Owner: /setvc @usernamegrup."


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
        return f"❌ Gagal: {ex}"


async def act_resume():
    e = need_vc()
    if e:
        return e
    try:
        await call_any(["resume", "resume_stream"], TARGET_VC_ID)
        song = current_songs.get(TARGET_VC_ID)
        if song and song.get("duration") and "left" in song and TARGET_VC_ID not in timers:
            schedule_next(TARGET_VC_ID, song, song["left"])
        return "▶ Dilanjutkan."
    except Exception as ex:
        return f"❌ Gagal: {ex}"


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
    return "⏹ Dihentikan (tetap di VC)."


async def act_loop():
    global LOOP
    LOOP = not LOOP
    await save_data()
    return f"🔁 Loop {'ON (lagu diulang)' if LOOP else 'OFF'}."


async def act_filter():
    global FILTER_ON
    FILTER_ON = not FILTER_ON
    await save_data()
    if FILTER_ON:
        return f"🧹 Filter ON ({fmt_dur(MIN_DUR)}-{fmt_dur(MAX_DUR)}, kata terlarang aktif)."
    return "🧹 Filter OFF."


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
                "Ubah: /quality 144, 240, 360, 480, 720, atau audio (paling hemat).")
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
    return f"🗑 Dihapus: {clean(s['title'])}"


async def act_clear():
    e = need_vc()
    if e:
        return e
    queues[TARGET_VC_ID] = []
    return "🧹 Antrean dikosongkan."


async def act_shuffle():
    e = need_vc()
    if e:
        return e
    random.shuffle(get_queue(TARGET_VC_ID))
    return "🔀 Antrean diacak."


# ==========================================
# TAMPILAN (teks + tombol)
# ==========================================
def bar(pos, total, n=10):
    if not total:
        return "▱" * n
    k = int(n * pos / total)
    return "▰" * k + "▱" * (n - k)


def panel_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    now = clean(cur["title"]) if cur and cur["title"] != "Silent" else "—"
    ub = "🟢 Online" if userbot_call else "🔴 Offline"
    vc = f"{TARGET_VC_ID}" if TARGET_VC_ID else "belum diset"
    qn = len(get_queue(TARGET_VC_ID)) if TARGET_VC_ID else 0
    return (
        "🎵 **Music Panel**\n\n"
        f"Userbot: {ub}\nVC: {vc}\n"
        f"Sedang main: {now}\nAntrean: {qn} lagu\n\n"
        f"🧹 Filter: {'ON' if FILTER_ON else 'OFF'}   🔁 Loop: {'ON' if LOOP else 'OFF'}\n"
        f"📺 Video: {quality_label()}{' 💡hemat' if HEMAT_ON else ''}\n\n"
        "Cari lagu: /play judul\nNonton: /vplay judul atau /vplay link\nCinema: /cinema"
    )


def ctrl_buttons():
    return [
        [Button.inline("⏸", b"pause"), Button.inline("▶", b"resume"),
         Button.inline("⏭", b"skip"), Button.inline("⏹", b"stop")],
        [Button.inline("🎶 Now", b"np"), Button.inline("📜 Antrean", b"queue"),
         Button.inline("🔁 Loop", b"loop")],
    ]


def panel_buttons(uid):
    return ctrl_buttons() + [
        [Button.inline("🧹 Filter", b"filter"), Button.inline("💡 Hemat", b"hemat"),
         Button.inline("🔄 Refresh", b"refresh")],
        [Button.inline("🎬 Cinema Center", b"cin:home")],
        [Button.inline("👥 Admin", b"admins")],
    ]


def queue_text():
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    now = (f"🎶 **Sedang diputar:** {clean(cur['title'])}\n\n"
           if cur and cur["title"] != "Silent" else "")
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    if not q:
        return now + "📜 **Antrean kosong.**"
    lines = [f"{i + 1}. {clean(s['title'])}" for i, s in enumerate(q[:15])]
    more = f"\n… dan {len(q) - 15} lagi" if len(q) > 15 else ""
    return now + "📜 **Antrean:**\n" + "\n".join(lines) + more


def queue_buttons():
    q = get_queue(TARGET_VC_ID) if TARGET_VC_ID else []
    rows = []
    nums = [Button.inline(f"🗑{i + 1}", f"rm:{i + 1}".encode()) for i in range(min(len(q), 10))]
    for i in range(0, len(nums), 5):
        rows.append(nums[i:i + 5])
    rows.append([Button.inline("🔀 Acak", b"shuf"), Button.inline("🧹 Kosongkan", b"clr"),
                 Button.inline("⏭ Skip", b"skip")])
    return rows


def admins_text():
    lines = [f"👑 Owner: {OWNER_ID}"]
    for uid, name in DATA["admins"].items():
        lines.append(f"• {clean(name)} ({uid})")
    if len(lines) == 1:
        lines.append("(belum ada admin tambahan)")
    lines.append("\nTambah: /addadmin ID atau minta orangnya kirim /start ke bot ini.")
    return "👥 **Daftar Admin**\n\n" + "\n".join(lines)


def admins_buttons(uid):
    if not is_owner(uid):
        return None
    rows = [[Button.inline(f"🗑 {n[:20]}", f"deladm:{u}".encode())]
            for u, n in list(DATA["admins"].items())[:10]]
    return rows or None


async def send_np(chat_id):
    cur = current_songs.get(TARGET_VC_ID) if TARGET_VC_ID else None
    if not cur or cur["title"] == "Silent":
        return await manager_bot.send_message(chat_id, "🎶 Tidak ada yang diputar.", buttons=ctrl_buttons())
    dur = cur.get("duration") or 0
    pos = elapsed(cur, TARGET_VC_ID)
    cap = (
        f"🎶 **Sedang diputar**\n**{clean(cur['title'])}**\n"
        f"{clean(cur.get('channel', ''))} • {cur.get('source', '?')}\n\n"
        f"{bar(pos, dur)}  {fmt_dur(pos)} / {fmt_dur(dur)}\n"
        f"🔁 Loop {'ON' if LOOP else 'OFF'}"
    )
    if cur.get("thumb"):
        try:
            return await manager_bot.send_file(chat_id, cur["thumb"], caption=cap, buttons=ctrl_buttons())
        except Exception as e:
            print(f"Thumb NP gagal: {e}")
    return await manager_bot.send_message(chat_id, cap, buttons=ctrl_buttons())


async def clear_cards(chat_id):
    st = PENDING.pop(chat_id, None)
    if st and st.get("mids"):
        try:
            await manager_bot.delete_messages(chat_id, st["mids"])
        except Exception:
            pass


async def show_results(chat_id, query, results, video):
    await clear_cards(chat_id)
    mids = []
    head = await manager_bot.send_message(
        chat_id, f"{'🎬' if video else '🔎'} **Hasil untuk:** {clean(query)}")
    mids.append(head.id)
    for i, r in enumerate(results):
        ch = f"{clean(r['channel'])} • " if r.get("channel") else ""
        cap = f"**{i + 1}. {clean(r['title'])}**\n{ch}{fmt_dur(r['duration'])} • {r['source']}"
        btns = [[Button.inline("▶ Putar", f"pick:{i}".encode()),
                 Button.inline("➕ Antre", f"add:{i}".encode())]]
        msg = None
        if r.get("thumb"):
            try:
                msg = await manager_bot.send_file(chat_id, r["thumb"], caption=cap, buttons=btns)
            except Exception as e:
                print(f"Thumb gagal: {e}")
        if msg is None:
            msg = await manager_bot.send_message(chat_id, cap, buttons=btns)
        mids.append(msg.id)
    tail = await manager_bot.send_message(
        chat_id, "Pilih salah satu 👆", buttons=[[Button.inline("❌ Tutup", b"close")]])
    mids.append(tail.id)
    PENDING[chat_id] = {"results": results, "mids": mids}


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
# PERINTAH (lewat bot panel)
# ==========================================
def arg_of(event):
    parts = (event.raw_text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def deny(event):
    await event.reply(
        f"⛔ **Kamu belum jadi admin.**\nID kamu: {event.sender_id}",
        buttons=[[Button.inline("🙋 Minta akses ke owner", b"req")]],
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


@cmd(r"^/(start|panel|menu)(?:\s|$)")
async def c_start(event):
    await event.reply(panel_text(), buttons=panel_buttons(event.sender_id))


@cmd(r"^/help(?:\s|$)")
async def c_help(event):
    await event.reply(
        "📖 **Perintah**\n\n"
        "/play judul|link - cari lagu (ada gambar + tombol)\n"
        "/fast judul - langsung putar hasil teratas\n"
        "/vplay judul|link - nonton (share screen)\n"
        "/playurl url - audio dari link apa saja\n"
        "/vplayurl url - video dari link apa saja\n"
        "/np /queue /pause /resume /skip /stop\n"
        "/loop /shuffle /clear /remove N\n"
        "/filter /hemat [144|240|360] /quality audio|144..720\n"
        "/cinema /csearch judul - film LK21\n"
        "/admins /status\n\n"
        "**Owner:** /setvc @grup /unsetvc /addadmin ID /deladmin ID /getsession /amode"
    )


@cmd(r"^/cinema(?:\s|$)")
async def cinema_command(event):
    CINEMA_VIEW[event.chat_id] = {"type": "home"}
    await event.reply(cinema_home_text(), buttons=cinema_home_buttons(), link_preview=False)


@cmd(r"^/csearch(?:\s|$)")
async def cinema_search_command(event):
    q = arg_of(event)
    if not q:
        return await event.reply("Cara mencari: `/csearch judul film`",
                                 buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])
    status = await event.reply("🔎 Mencari di LK21...")
    try:
        await cinema_search_list(status, event.chat_id, q)
    except Exception as e:
        await status.edit(f"⚠️ Error: {clean(str(e))}", buttons=[[Button.inline("🏠 Cinema", b"cin:home")]])


@cmd(r"^/setvc(?:\s|$)", owner=True)
async def c_setvc(event):
    global TARGET_VC_ID
    if not userbot_app:
        return await event.reply(NO_USERBOT)
    raw = arg_of(event)
    if not raw:
        return await event.reply(
            "**Usage:**\n/setvc @usernamegrup\n/setvc https://t.me/usernamegrup\n"
            "/setvc -5240836914\n/setvc https://t.me/+kodeundangan (grup privat)")
    m = await event.reply("⏳ Mencoba masuk VC...")
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
        await m.edit(f"✅ **Target VC diset!**\nID: {chat_id}\nSilakan /play judul.",
                     buttons=[[Button.inline("🎛 Panel", b"refresh")]])
    except Exception as e:
        hint = (
            "Akun belum 'mengenal' grup ini. Pakai @usernamegrup atau link grup. "
            "Grup privat: link undangan t.me/+...."
            if "input entity" in str(e) or "Cannot find any entity" in str(e)
            else "Cek: VC grup sedang aktif, dan ID/username benar."
        )
        await m.edit(f"❌ Gagal masuk: {e}\n{hint}")


@cmd(r"^/unsetvc(?:\s|$)", owner=True)
async def c_unsetvc(event):
    global TARGET_VC_ID
    if TARGET_VC_ID:
        cancel_timer(TARGET_VC_ID)
        try:
            await call_any(["leave_call", "leave_group_call"], TARGET_VC_ID)
        except Exception:
            pass
        queues[TARGET_VC_ID] = []
        current_songs.pop(TARGET_VC_ID, None)
    TARGET_VC_ID = None
    await event.reply("✅ Target VC dilepas.")


@cmd(r"^/play(?:\s|$)")
async def c_play(event):
    err = need_vc()
    if err:
        return await event.reply(err)
    q = arg_of(event)
    if not q:
        return await event.reply("**Usage:** /play judul lagu atau /play link")
    m = await event.reply("🔎 Mencari...")
    if q.startswith("http"):
        song, e = await asyncio.to_thread(
            resolve_song, {"page": q, "title": q, "source": detect_source(q)})
        if not song:
            return await m.edit(f"❌ Gagal ambil audio.\n{clean(e)}")
        return await m.edit(await do_enqueue(song), buttons=ctrl_buttons())
    results, e = await asyncio.to_thread(search_candidates, q)
    if not results:
        return await m.edit(f"❌ {clean(e)}")
    await m.delete()
    await show_results(event.chat_id, q, results, video=False)


@cmd(r"^/fast(?:\s|$)")
async def c_fast(event):
    err = need_vc()
    if err:
        return await event.reply(err)
    q = arg_of(event)
    if not q:
        return await event.reply("**Usage:** /fast judul lagu")
    m = await event.reply("🔎 Mencari...")
    results, e = await asyncio.to_thread(search_candidates, q)
    if not results:
        return await m.edit(f"❌ {clean(e)}")
    song, e = await asyncio.to_thread(resolve_song, results[0])
    if not song:
        return await m.edit(f"❌ Gagal ambil audio.\n{clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())


@cmd(r"^/vplay(?:\s|$)")
async def c_vplay(event):
    err = need_vc()
    if err:
        return await event.reply(err)
    q = arg_of(event)
    if not q:
        return await event.reply("**Usage:** /vplay judul atau /vplay link")
    m = await event.reply("🔎 Mencari video...")
    if q.startswith("http"):
        song, e = await asyncio.to_thread(
            resolve_song, {"page": q, "title": q, "source": detect_source(q), "video": True})
        if not song:
            return await m.edit(f"❌ Gagal ambil video.\n{clean(e)}")
        return await m.edit(await do_enqueue(song), buttons=ctrl_buttons())
    try:
        results = await asyncio.to_thread(_flat_search, "ytsearch", "YouTube", q, MAX_RESULTS)
    except Exception as ex:
        return await m.edit(f"❌ Gagal mencari: {clean(str(ex)[:200])}")
    if not results:
        return await m.edit("❌ Video tidak ditemukan.")
    for r in results:
        r["video"] = True
    await m.delete()
    await show_results(event.chat_id, q, results, video=True)


@cmd(r"^/playurl(?:\s|$)")
async def c_playurl(event):
    err = need_vc()
    if err:
        return await event.reply(err)
    url = arg_of(event)
    if not url.startswith("http"):
        return await event.reply("**Usage:** /playurl https://...")
    m = await event.reply("🔎 Mengekstrak stream...")
    song, e = await asyncio.to_thread(
        resolve_song, {"page": url, "title": url, "source": detect_source(url)})
    if not song:
        return await m.edit(f"❌ Gagal: {clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())


@cmd(r"^/vplayurl(?:\s|$)")
async def c_vplayurl(event):
    err = need_vc()
    if err:
        return await event.reply(err)
    url = arg_of(event)
    if not url.startswith("http"):
        return await event.reply("**Usage:** /vplayurl https://...")
    m = await event.reply("🔎 Mengekstrak video...")
    song, e = await asyncio.to_thread(
        resolve_song, {"page": url, "title": url, "source": detect_source(url), "video": True})
    if not song:
        return await m.edit(f"❌ Gagal: {clean(e)}")
    await m.edit(await do_enqueue(song), buttons=ctrl_buttons())


@cmd(r"^/np(?:\s|$)")
async def c_np(event):
    await send_np(event.chat_id)


@cmd(r"^/queue(?:\s|$)")
async def c_queue(event):
    await event.reply(queue_text(), buttons=queue_buttons())


@cmd(r"^/pause(?:\s|$)")
async def c_pause(event):
    await event.reply(await act_pause(), buttons=ctrl_buttons())


@cmd(r"^/resume(?:\s|$)")
async def c_resume(event):
    await event.reply(await act_resume(), buttons=ctrl_buttons())


@cmd(r"^/skip(?:\s|$)")
async def c_skip(event):
    await event.reply(await act_skip(), buttons=ctrl_buttons())


@cmd(r"^/stop(?:\s|$)")
async def c_stop(event):
    await event.reply(await act_stop())


@cmd(r"^/loop(?:\s|$)")
async def c_loop(event):
    await event.reply(await act_loop())


@cmd(r"^/shuffle(?:\s|$)")
async def c_shuffle(event):
    await event.reply(await act_shuffle())


@cmd(r"^/clear(?:\s|$)")
async def c_clear(event):
    await event.reply(await act_clear())


@cmd(r"^/remove(?:\s|$)")
async def c_remove(event):
    a = arg_of(event)
    if not a.isdigit():
        return await event.reply("**Usage:** /remove 2")
    await event.reply(await act_remove(int(a)))


@cmd(r"^/filter(?:\s|$)")
async def c_filter(event):
    await event.reply(await act_filter())


@cmd(r"^/hemat(?:\s|$)")
async def c_hemat(event):
    await event.reply(await act_hemat(arg_of(event)))


@cmd(r"^/quality(?:\s|$)")
async def c_quality(event):
    await event.reply(await act_quality(arg_of(event)))


@cmd(r"^/amode(?:\s|$)", owner=True)
async def c_amode(event):
    global AUDIO_PLAIN
    AUDIO_PLAIN = not AUDIO_PLAIN
    await event.reply(
        f"🔊 Mode audio: {'BIASA' if AUDIO_PLAIN else 'AUDIO-ONLY'}. Putar ulang untuk mencoba.")


@cmd(r"^/admins(?:\s|$)")
async def c_admins(event):
    await event.reply(admins_text(), buttons=admins_buttons(event.sender_id))


@cmd(r"^/addadmin(?:\s|$)", owner=True)
async def c_addadmin(event):
    a = arg_of(event)
    if not a:
        return await event.reply("**Usage:** /addadmin 123456789 atau /addadmin @username")
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
        return await event.reply(f"❌ Tidak bisa menemukan user: {e}\nPakai ID angka.")
    if uid == OWNER_ID:
        return await event.reply("Itu kamu sendiri, owner sudah punya semua akses.")
    DATA["admins"][str(uid)] = name
    await save_data()
    await event.reply(f"✅ **{clean(name)}** ({uid}) sekarang admin.")
    try:
        await manager_bot.send_message(uid, "✅ Kamu sekarang admin Music Bot. Ketik /start.")
    except Exception:
        pass


@cmd(r"^/deladmin(?:\s|$)", owner=True)
async def c_deladmin(event):
    a = arg_of(event)
    if not a.lstrip("-").isdigit() or str(int(a)) not in DATA["admins"]:
        return await event.reply("**Usage:** /deladmin ID (lihat /admins).")
    name = DATA["admins"].pop(str(int(a)))
    await save_data()
    await event.reply(f"🗑 {clean(name)} dihapus dari admin.")


@cmd(r"^/getsession(?:\s|$)", owner=True)
async def c_getsession(event):
    if not userbot_app:
        return await event.reply(NO_USERBOT)
    s = StringSession.save(userbot_app.session)
    m = await event.reply(
        "🔑 **SESSION_STRING** (rahasia, setara password akun!)\n\n"
        f"{s}\n\n"
        "Simpan di GitHub Secrets dengan nama SESSION_STRING, supaya userbot nyala otomatis "
        "tanpa upload zip. Pesan ini terhapus otomatis dalam 90 detik.")
    await asyncio.sleep(90)
    try:
        await m.delete()
    except Exception:
        pass


@cmd(r"^/status(?:\s|$)")
async def c_status(event):
    up = int(time.time() - START_TIME)
    await event.reply(
        "📊 **Status**\n"
        f"Uptime: {up // 3600}j {up % 3600 // 60}m\n"
        f"Userbot: {'online' if userbot_call else 'offline'}\n"
        f"VC: {TARGET_VC_ID or 'belum diset'}\n"
        f"Admin: {len(DATA['admins'])} + owner\n"
        f"Cookies: {'ada' if os.path.exists(COOKIES) else 'tidak ada'}\n"
        f"Penyimpanan: {'sinkron' if not DATA_DIRTY else 'belum tersimpan (menunggu userbot)'}\n"
        f"Cinema: {LK21_BASE_URL}\n"
        f"Anti-403: {'curl_cffi aktif' if IMPERSONATE else 'tidak aktif (pasang curl_cffi)'}"
    )


# ==========================================
# TOMBOL (CALLBACK)
# ==========================================
def toast(text):
    return re.sub(r"[*_]", "", text)[:190]


async def refresh_panel(event):
    try:
        await event.edit(panel_text(), buttons=panel_buttons(event.sender_id))
    except Exception:
        pass


async def handle_pick(event, idx, now):
    st = PENDING.get(event.chat_id)
    if not st or not (0 <= idx < len(st["results"])):
        return await event.answer("Daftar kedaluwarsa, cari lagi.", alert=True)
    err = need_vc()
    if err:
        return await event.answer(toast(err), alert=True)
    item = st["results"][idx]
    await event.answer("⏳ Mengambil…")
    song, e = await asyncio.to_thread(resolve_song, item)
    if not song:
        return await manager_bot.send_message(event.chat_id, f"❌ Gagal ambil.\n{clean(e)}")
    text = await do_enqueue(song, now=now)
    if now:
        await clear_cards(event.chat_id)
    await manager_bot.send_message(event.chat_id, text, buttons=ctrl_buttons())


@manager_bot.on(events.CallbackQuery())
async def on_callback(event):
    data = event.data.decode("utf-8", "ignore")
    uid = event.sender_id

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
                f"🙋 Permintaan akses admin\nNama: {clean(name)}\nUsername: {uname}\nID: {uid}",
                buttons=[[Button.inline("✅ Setujui", f"appr:{uid}".encode()),
                          Button.inline("❌ Tolak", f"rej:{uid}".encode())]],
            )
        except Exception as e:
            ASKED.discard(uid)
            return await event.answer(f"Gagal kirim ke owner: {e}"[:190], alert=True)
        return await event.answer("✅ Permintaan dikirim ke owner.", alert=True)

    if not is_admin(uid):
        return await event.answer("⛔ Kamu bukan admin.", alert=True)

    # --- Cinema (khusus admin) ---
    if data.startswith("cin:"):
        return await cinema_callback(event)

    # --- khusus owner ---
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
                await event.edit(f"✅ {clean(name)} ({tid}) disetujui jadi admin.")
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
                await event.edit(f"❌ Permintaan {tid} ditolak.")
            except Exception:
                pass
            try:
                await manager_bot.send_message(tid, "❌ Permintaan akses admin ditolak owner.")
            except Exception:
                pass
        else:
            name = DATA["admins"].pop(str(tid), None)
            await save_data()
            await event.answer(f"Dihapus: {name}" if name else "Tidak ditemukan.", alert=False)
            try:
                await event.edit(admins_text(), buttons=admins_buttons(uid))
            except Exception:
                pass
        return

    # --- hasil pencarian ---
    if data.startswith("pick:"):
        return await handle_pick(event, int(data[5:]), now=True)
    if data.startswith("add:"):
        return await handle_pick(event, int(data[4:]), now=False)
    if data == "close":
        await event.answer()
        return await clear_cards(event.chat_id)

    # --- kontrol musik ---
    simple = {"pause": act_pause, "resume": act_resume, "skip": act_skip,
              "stop": act_stop, "loop": act_loop}
    if data in simple:
        return await event.answer(toast(await simple[data]()))
    if data == "np":
        await event.answer()
        return await send_np(event.chat_id)
    if data == "queue":
        await event.answer()
        return await manager_bot.send_message(event.chat_id, queue_text(), buttons=queue_buttons())
    if data in ("shuf", "clr") or data.startswith("rm:"):
        if data == "shuf":
            text = await act_shuffle()
        elif data == "clr":
            text = await act_clear()
        else:
            text = await act_remove(int(data[3:]))
        await event.answer(toast(text))
        try:
            await event.edit(queue_text(), buttons=queue_buttons())
        except Exception:
            pass
        return
    if data == "filter":
        await event.answer(toast(await act_filter()))
        return await refresh_panel(event)
    if data == "hemat":
        await event.answer(toast(await act_hemat("")))
        return await refresh_panel(event)
    if data == "refresh":
        await event.answer("🔄")
        return await refresh_panel(event)
    if data == "admins":
        await event.answer()
        return await manager_bot.send_message(
            event.chat_id, admins_text(), buttons=admins_buttons(uid))
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
        return await event.reply("❌ Kirim file .zip yang berisi file .session.")

    msg = await event.reply("⏳ Memproses file...")
    zip_path = await event.download_media()
    try:
        extract_dir = os.path.join(TMP_DIR, "session_extract")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)
        os.remove(zip_path)

        found = glob.glob(f"{extract_dir}/**/*.session", recursive=True)
        if not found:
            return await msg.edit("❌ File .session tidak ditemukan di dalam zip!")
        for f in glob.glob(SESSION_BASE + ".session*"):
            os.remove(f)
        shutil.move(found[0], SESSION_BASE + ".session")

        await msg.edit("✅ Session ditemukan.\n⏳ Menyalakan userbot...")
        await boot_userbot(SESSION_BASE)
        await msg.edit(
            "🚀 Userbot online!\n\n"
            "1. /setvc @usernamegrup\n2. /play judul\n\n"
            "Tips: ketik /getsession lalu simpan di Secrets SESSION_STRING "
            "supaya tidak perlu upload zip lagi setiap restart.",
            buttons=[[Button.inline("🎛 Panel", b"refresh")]],
        )
    except Exception as e:
        await msg.edit(f"❌ Gagal:\n{e}")


# ==========================================
# JALANKAN
# ==========================================
async def notify_owner(text):
    try:
        await manager_bot.send_message(OWNER_ID, text)
    except Exception as e:
        print(f"Gagal kirim notifikasi ke owner: {e}")


async def main():
    print("Memulai Manager Bot...")
    await manager_bot.start(bot_token=BOT_TOKEN)
    print("✅ Manager Bot siap!")
    if SESSION_STRING:
        try:
            await boot_userbot(StringSession(SESSION_STRING))
            await notify_owner("🚀 Bot restart. Userbot online otomatis.\n/start untuk panel, lalu /setvc.")
        except Exception as e:
            await notify_owner(f"❌ Auto-start userbot gagal: {e}\nKirim file zip session secara manual.")
    else:
        await notify_owner("🤖 Bot nyala. Kirim file zip session untuk menyalakan userbot.")
    await manager_bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot dihentikan.")
