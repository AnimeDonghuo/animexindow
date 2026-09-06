import os
import io
import json
import asyncio
import requests
import time
import re
import shutil
import subprocess
import datetime
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified, FloodWait

try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token_here")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", -1003966911002))
EXTRA_CHANNELS = [
    int(x) for x in os.environ.get("EXTRA_CHANNELS", "").replace(" ", "").split(",") if x
]
THUMB_URL = os.environ.get("THUMB_URL", "https://i.ibb.co/KjTqgMkS/x.jpg")
SITE_URL = os.environ.get("SITE_URL", "https://animexin.dev/")
DB_FILE = os.environ.get("DB_FILE", "processed_posts.json")
SCHEDULE_TIME = os.environ.get("SCHEDULE_TIME", "17:00")

# Qualities that get re-encoded (comma separated heights).
ENCODE_QUALITIES = [q.strip() for q in
                    os.environ.get("QUALITIES", "360p,480p,720p").split(",") if q.strip()]
# 1080p is NOT re-encoded: the source from the site is already 1080p, so it is
# uploaded as-is (zero CPU). Only if it is bigger than the limit below do we
# re-encode it down to fit.
PASSTHROUGH_QUALITY = os.environ.get("PASSTHROUGH_QUALITY", "1080p")
ENABLE_PASSTHROUGH = os.environ.get("ENABLE_PASSTHROUGH", "1") not in ("0", "false", "False")
# Telegram bots cap at 2 GB; stay just under it.
PASSTHROUGH_MAX_BYTES = int(float(os.environ.get("PASSTHROUGH_MAX_GB", "1.95")) * 1024 ** 3)

def all_qualities():
    q = list(ENCODE_QUALITIES)
    if ENABLE_PASSTHROUGH and PASSTHROUGH_QUALITY not in q:
        q.append(PASSTHROUGH_QUALITY)
    return q

# x264 tuning for a weak VPS. veryfast is ~2-3x slower than ultrafast but
# produces roughly HALF the file size at the same visual quality.
X264_PRESET = os.environ.get("X264_PRESET", "veryfast")
CRF = {  # per-height CRF: higher = smaller file
    360: os.environ.get("CRF_360", "28"),
    480: os.environ.get("CRF_480", "27"),
    720: os.environ.get("CRF_720", "26"),
    1080: os.environ.get("CRF_1080", "25"),
}
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "96k")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "0")  # 0 = let ffmpeg decide

# Watchdog: kill ffmpeg only if it makes NO progress for this many seconds.
# (The old code used a fixed wall-clock timeout, which killed perfectly
#  healthy encodes on a slow VPS -> "FFmpeg timeout reached".)
FFMPEG_STALL_TIMEOUT = int(os.environ.get("FFMPEG_STALL_TIMEOUT", "600"))
# Absolute ceiling as a multiple of the video duration (realtime factor).
FFMPEG_MAX_RT_FACTOR = float(os.environ.get("FFMPEG_MAX_RT_FACTOR", "25"))
FFMPEG_MIN_DEADLINE = float(os.environ.get("FFMPEG_MIN_DEADLINE", "1800"))

EDIT_INTERVAL = float(os.environ.get("EDIT_INTERVAL", "12"))  # seconds between edits

app = Client("animexin_pro_v5", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# --- DATABASE ---
def load_db():
    """DB schema: {url: {"title": str, "done": [quality, ...], "no_link": bool}}
    Old format (a plain list of urls) is migrated automatically."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):  # migrate legacy format
                return {u: {"title": "", "done": list(all_qualities()), "no_link": False}
                        for u in data}
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def entry_for(db, url):
    e = db.setdefault(url, {"title": "", "done": [], "no_link": False})
    e.setdefault("done", [])
    e.setdefault("no_link", False)
    e.setdefault("title", "")
    return e


def missing_qualities(db, url):
    """Which qualities still need uploading for this episode."""
    done = set(entry_for(db, url).get("done", []))
    return [q for q in all_qualities() if q not in done]


def is_complete(db, url):
    return url in db and not missing_qualities(db, url)


def save_db(data):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, DB_FILE)


# --- SAFE EDIT (fixes MESSAGE_NOT_MODIFIED spam) ---
_last_text = {}   # message key -> last text we actually sent
_last_time = {}   # message key -> last edit timestamp


def _mkey(msg):
    return (getattr(msg, "chat", None) and msg.chat.id, msg.id)


async def safe_edit(msg, text, force=False):
    """Edit a message only when the content actually changed and enough time
    has passed. Silently ignores MESSAGE_NOT_MODIFIED instead of logging it."""
    if msg is None:
        return
    key = _mkey(msg)
    now = time.time()

    if _last_text.get(key) == text:      # identical content -> Telegram would 400
        return
    if not force and now - _last_time.get(key, 0) < EDIT_INTERVAL:
        return

    _last_text[key] = text
    _last_time[key] = now
    try:
        await msg.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        _last_time[key] = now + float(getattr(e, "value", 5))
    except Exception as e:
        print(f"[Edit] {type(e).__name__}: {e}")


def _forget(msg):
    key = _mkey(msg)
    _last_text.pop(key, None)
    _last_time.pop(key, None)


# --- PROGRESS BAR ---
def get_progress_bar(pct, filled="🟢"):
    pct = max(0.0, min(100.0, pct))
    n = int(pct / 10)
    return f"|{filled * n}{'⚪' * (10 - n)}| {pct:.1f}%"


async def update_progress_msg(current, total, msg, title, status_type):
    pct = (current / total * 100) if total else 0
    await safe_edit(
        msg,
        f"🎬 **{title}**\n\n{status_type}\n{get_progress_bar(pct)}\n"
        f"`{current/1048576:.1f}MB / {total/1048576:.1f}MB`",
    )


# --- NAMING ---
def clean_page_title(soup):
    h1 = soup.find("h1")
    if h1:
        name = h1.text.strip()
        name = re.split(r"Subtitle|Indonesia|English|Indo", name, flags=re.IGNORECASE)[0]
        name = name.replace("Episode", "Ep").strip()
        return name.rstrip(",- ")
    return "Anime Episode"


def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9\s\.\-\[\]\(\)]", "", name).strip() or "Anime Episode"


# --- POSTER (fixes 400 IMAGE_PROCESS_FAILED) ---
def normalise_image(raw_bytes, out_path, max_side=1280, max_bytes=4 * 1024 * 1024):
    """Telegram rejects webp/avif/CMYK/huge/oversized-ratio images with
    IMAGE_PROCESS_FAILED. Re-encode everything to a plain baseline RGB JPEG."""
    if not raw_bytes:
        return None
    if not PIL_OK:
        # Without Pillow we can only trust real JPEGs.
        if raw_bytes[:3] == b"\xff\xd8\xff":
            with open(out_path, "wb") as f:
                f.write(raw_bytes)
            return out_path
        return None
    try:
        im = Image.open(io.BytesIO(raw_bytes))
        im.load()
        if im.mode != "RGB":
            im = im.convert("RGB")

        w, h = im.size
        if w < 20 or h < 20:
            return None
        # Telegram requires width+height <= 10000 and ratio <= 20
        if max(w, h) / min(w, h) > 19:
            return None
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        for q in (85, 75, 65, 55, 45):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True, progressive=False)
            if buf.tell() <= max_bytes:
                break
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
        return out_path
    except Exception as e:
        print(f"[Poster] cannot normalise image: {e}")
        return None


def fetch_image(url, out_path):
    try:
        r = requests.get(
            url, timeout=25,
            headers={"User-Agent": "Mozilla/5.0", "Referer": SITE_URL},
        )
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            return None
        return normalise_image(r.content, out_path)
    except Exception as e:
        print(f"[Poster] download failed {url}: {e}")
        return None


def find_poster_url(soup):
    for sel, attr in (
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        (".thumb img", "src"),
        ("article img", "src"),
        ("img", "src"),
    ):
        tag = soup.select_one(sel)
        if tag and tag.get(attr):
            return tag[attr]
    return None


async def send_poster_card(chat_id, poster_path, caption):
    """Never let a broken poster kill the post - fall back to plain text."""
    if poster_path and os.path.exists(poster_path):
        try:
            await app.send_photo(chat_id, poster_path, caption=caption)
            return True
        except Exception as e:
            print(f"[Poster] send_photo failed for {chat_id}: {e} -> text fallback")
    try:
        await app.send_message(chat_id, caption, disable_web_page_preview=True)
        return True
    except Exception as e:
        print(f"[Poster] text fallback failed for {chat_id}: {e}")
        return False


# --- MEDIAFIRE ---
def find_english_mediafire(soup):
    links = [a["href"] for a in soup.find_all("a", href=True) if "mediafire.com" in a["href"]]
    if len(links) >= 2:
        print(f"Targeting English Mediafire: {links[1]}")
        return links[1]
    return links[0] if links else None


def mediafire_direct(mf_url):
    r = requests.get(mf_url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(r.text, "html.parser")
    btn = soup.find("a", {"id": "downloadButton"})
    if btn and btn.get("href", "").startswith("http"):
        return btn["href"]
    m = re.search(r'href="(https://download[^"]+)"', r.text)
    if m:
        return m.group(1)
    raise RuntimeError("Could not resolve Mediafire direct link")


# --- PROBE ---
def probe(input_f):
    """Return (duration_seconds, height, has_audio)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,height",
             "-of", "json", input_f],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120,
        ).stdout
        data = json.loads(out or "{}")
        dur = float(data.get("format", {}).get("duration") or 0) or 0.0
        height, has_audio = 0, False
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                height = max(height, int(s.get("height") or 0))
            if s.get("codec_type") == "audio":
                has_audio = True
        return dur, height, has_audio
    except Exception as e:
        print(f"[Probe] {e}")
        return 0.0, 0, True


# --- ENCODER ---
async def _run_ffmpeg(cmd, msg, title, duration, label, output_f):
    """Run ffmpeg with a progress-based watchdog. Returns True on success.

    The watchdog kills ffmpeg only when it stops making progress (or blows the
    absolute deadline), never on a plain wall-clock timer -- a slow encode on a
    weak VPS is allowed to simply take a long time.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    started = time.time()
    hard_deadline = started + max(FFMPEG_MIN_DEADLINE, duration * FFMPEG_MAX_RT_FACTOR)
    last_progress = time.time()
    out_time = 0.0
    killed_reason = None

    async def reader():
        nonlocal out_time, last_progress
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode("utf-8", "ignore").strip()
                if line.startswith("out_time_ms="):
                    try:
                        out_time = int(line.split("=", 1)[1]) / 1_000_000
                        last_progress = time.time()
                    except Exception:
                        pass
                elif line.startswith("out_time="):
                    m = re.match(r"out_time=(\d+):(\d+):([\d.]+)", line)
                    if m:
                        out_time = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                    + float(m.group(3)))
                        last_progress = time.time()

    reader_task = asyncio.create_task(reader())

    while proc.returncode is None:
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
            break
        except asyncio.TimeoutError:
            pass

        if duration:
            pct = min(99.9, out_time / duration * 100)
            eta = ""
            if out_time > 5:
                speed = out_time / max(1e-3, time.time() - started)
                if speed > 0:
                    remain = int((duration - out_time) / speed)
                    eta = f"\n⏳ ETA `{remain // 60}m {remain % 60}s`"
            await safe_edit(
                msg,
                f"🎬 **{title}**\n\n{label}\n{get_progress_bar(pct, '🟠')}{eta}",
            )

        if time.time() - last_progress > FFMPEG_STALL_TIMEOUT:
            killed_reason = f"stalled for {FFMPEG_STALL_TIMEOUT}s"
        elif time.time() > hard_deadline:
            killed_reason = "exceeded hard deadline"
        if killed_reason:
            print(f"[FFmpeg] killing: {killed_reason}")
            try:
                proc.kill()
            except Exception:
                pass
            break

    await proc.wait()
    reader_task.cancel()
    err = b""
    try:
        err = await proc.stderr.read()
    except Exception:
        pass

    ok = (proc.returncode == 0 and os.path.exists(output_f)
          and os.path.getsize(output_f) > 100_000)
    if not ok:
        print(f"[FFmpeg] failed (rc={proc.returncode}, {killed_reason or 'error'}): "
              f"{err.decode('utf-8', 'ignore')[-500:]}")
        if os.path.exists(output_f):
            try:
                os.remove(output_f)
            except Exception:
                pass
    return ok


async def encode_video(input_f, output_f, res_p, msg, title, duration, src_height, has_audio):
    res_val = int(res_p.replace("p", ""))

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-progress", "pipe:1", "-stats_period", "5",
        "-i", input_f,
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += [
        # Width is derived from the DISPLAY aspect ratio (dar), so anamorphic
        # sources are converted to square pixels instead of coming out
        # stretched; trunc(../2)*2 guarantees the mod-2 dimensions that
        # libx264 + yuv420p require (odd sizes are what "break" the picture).
        "-vf", f"scale=trunc(oh*dar/2)*2:{res_val}:flags=bicubic,setsar=1",
        "-c:v", "libx264",
        "-preset", X264_PRESET,
        "-crf", str(CRF.get(res_val, "26")),
        "-profile:v", "high", "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-g", "250", "-sc_threshold", "0",
        "-threads", FFMPEG_THREADS,
        "-max_muxing_queue_size", "1024",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ac", "2", "-ar", "44100"]
    cmd += ["-movflags", "+faststart", "-y", output_f]

    return await _run_ffmpeg(cmd, msg, title, duration,
                             f"⚙️ **Encoding {res_p}**", output_f)



# --- SIZE-CAPPED RE-ENCODE (for oversized 1080p passthrough) ---
async def encode_to_fit(input_f, output_f, msg, title, duration, has_audio,
                        target_bytes, height=1080):
    """Re-encode at `height` using two-pass-style bitrate targeting so the
    result lands just under target_bytes. Used only when the original file is
    too big to upload as-is."""
    if duration <= 0:
        return False

    audio_bps = 128000 if has_audio else 0
    # 4% muxing/overhead safety margin
    total_bps = (target_bytes * 8 * 0.96) / duration
    video_bps = int(total_bps - audio_bps)
    if video_bps < 200_000:
        video_bps = 200_000
    maxrate = int(video_bps * 1.5)
    bufsize = int(video_bps * 2)

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-progress", "pipe:1", "-stats_period", "5",
        "-i", input_f, "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += [
        "-vf", f"scale=trunc(oh*dar/2)*2:{height}:flags=bicubic,setsar=1",
        "-c:v", "libx264", "-preset", X264_PRESET,
        "-b:v", str(video_bps), "-maxrate", str(maxrate), "-bufsize", str(bufsize),
        "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
        "-g", "250", "-sc_threshold", "0",
        "-threads", FFMPEG_THREADS, "-max_muxing_queue_size", "1024",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", "-y", output_f]

    print(f"[Shrink] targeting {target_bytes/1048576:.0f}MB "
          f"-> video bitrate {video_bps/1000:.0f}kbps")
    ok = await _run_ffmpeg(cmd, msg, title, duration,
                           f"🗜️ **Shrinking {height}p to fit**", output_f)
    if ok and os.path.getsize(output_f) > target_bytes:
        print(f"[Shrink] still {os.path.getsize(output_f)/1048576:.0f}MB, retrying lower")
        return await encode_to_fit(input_f, output_f, msg, title, duration,
                                   has_audio, int(target_bytes * 0.9), height)
    return ok


# --- MAIN TASK ---
async def run_task(ep_url, status_msg, db=None, only_missing=True, announce=True):
    """Process one episode. Only uploads qualities not already recorded as done,
    so re-running /chk on the same link retries just the failures."""
    source_file = "raw_source.mp4"
    poster_file = "poster.jpg"
    own_db = db is None
    if own_db:
        db = load_db()
    entry = entry_for(db, ep_url)

    wanted = missing_qualities(db, ep_url) if only_missing else list(all_qualities())
    if not wanted:
        await safe_edit(status_msg,
                        f"✅ **Already complete:**\n{entry.get('title') or ep_url}",
                        force=True)
        return True

    try:
        res = requests.get(ep_url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, "html.parser")
        base_title = safe_filename(clean_page_title(soup))
        entry["title"] = base_title

        mf_url = find_english_mediafire(soup)
        if not mf_url:
            # Not an error: many posts simply have no Mediafire mirror yet.
            # Record it and stay quiet so repeated /chk runs don't spam.
            first_time = not entry.get("no_link")
            entry["no_link"] = True
            if own_db:
                save_db(db)
            if first_time:
                print(f"[Skip] No Mediafire link yet: {base_title}")
            await safe_edit(status_msg,
                            f"⏭️ **Skipped (no Mediafire link yet):**\n{base_title}",
                            force=True)
            return None  # None = "nothing to do", not a failure

        # A link exists now, so clear any previous no-link marker.
        entry["no_link"] = False

        if announce:
            poster_url = find_poster_url(soup)
            poster = fetch_image(poster_url, poster_file) if poster_url else None
            caption = (f"🎬 **{base_title}**\n✅ **English Subtitle**\n"
                       f"📤 Uploading: {', '.join(wanted)}")
            for cid in [CHANNEL_ID] + EXTRA_CHANNELS:
                await send_poster_card(cid, poster, caption)

        direct = mediafire_direct(mf_url)

        with requests.get(direct, stream=True, timeout=(30, 300),
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            curr = 0
            with open(source_file, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    curr += len(chunk)
                    await update_progress_msg(curr, total, status_msg, base_title,
                                              "📥 **Downloading English Source**")

        duration, src_height, has_audio = probe(source_file)
        if duration <= 0:
            await safe_edit(status_msg, f"❌ Source is not a valid video:\n{base_title}",
                            force=True)
            return False
        src_bytes = os.path.getsize(source_file)
        print(f"[Source] {base_title}: {duration:.0f}s, {src_height}p, "
              f"{src_bytes/1048576:.0f}MB, audio={has_audio}")

        thumb = "thumb.jpg"
        if not os.path.exists(thumb):
            fetch_image(THUMB_URL, thumb)

        uploaded, failed = [], []
        for q in wanted:
            target = int(q.replace("p", ""))
            temp_file = f"temp_{q}.mp4"
            upload_path = None

            if q == PASSTHROUGH_QUALITY and ENABLE_PASSTHROUGH:
                # Upload the site's own file untouched -- no CPU at all --
                # unless it is too big for Telegram, in which case shrink it.
                if src_bytes <= PASSTHROUGH_MAX_BYTES:
                    print(f"[{q}] passthrough, no re-encode "
                          f"({src_bytes/1048576:.0f}MB)")
                    upload_path = source_file
                else:
                    print(f"[{q}] {src_bytes/1048576:.0f}MB exceeds limit -> shrinking")
                    await safe_edit(status_msg,
                                    f"🎬 **{base_title}**\n\n🗜️ Source is "
                                    f"`{src_bytes/1048576:.0f}MB`, shrinking to fit...")
                    if await encode_to_fit(source_file, temp_file, status_msg,
                                           base_title, duration, has_audio,
                                           PASSTHROUGH_MAX_BYTES,
                                           min(target, src_height or target)):
                        upload_path = temp_file
            else:
                if src_height and target > src_height:
                    print(f"Skipping {q}: source is only {src_height}p")
                    continue
                if await encode_video(source_file, temp_file, q, status_msg,
                                      base_title, duration, src_height, has_audio):
                    upload_path = temp_file

            if not upload_path:
                print(f"[{q}] encoding failed -- will retry on next /chk")
                failed.append(q)
                continue

            size_mb = os.path.getsize(upload_path) / 1048576
            tag = "Original" if upload_path == source_file else "Encoded"
            print(f"[{tag}] {q} -> {size_mb:.1f}MB")
            try:
                await app.send_document(
                    chat_id=CHANNEL_ID,
                    document=upload_path,
                    thumb=thumb if os.path.exists(thumb) else None,
                    file_name=f"{base_title} Eng Sub [{q}].mp4",
                    caption=f"🎬 **{base_title}**\n🔥 Quality: **{q}**  •  "
                            f"`{size_mb:.0f}MB`\n✅ **English Subtitle**",
                    progress=update_progress_msg,
                    progress_args=(status_msg, base_title, f"📤 **Uploading {q}**"),
                )
                uploaded.append(q)
                if q not in entry["done"]:
                    entry["done"].append(q)
                if own_db:
                    save_db(db)   # persist after each success so a crash resumes
            except Exception as e:
                print(f"[Upload] {q} failed: {e}")
                failed.append(q)
            finally:
                if upload_path == temp_file and os.path.exists(temp_file):
                    os.remove(temp_file)
            await asyncio.sleep(5)

        still = missing_qualities(db, ep_url)
        if not still:
            msg = f"✅ **Complete:**\n{base_title}\nUploaded: {', '.join(uploaded) or 'nothing new'}"
        else:
            msg = (f"⚠️ **Partial:**\n{base_title}\n"
                   f"✅ Done: {', '.join(entry['done']) or 'none'}\n"
                   f"❌ Still missing: {', '.join(still)}\n"
                   f"_Run_ `/chk {ep_url}` _to retry only these._")
        await safe_edit(status_msg, msg, force=True)
        return not still

    except Exception as e:
        print(f"[Task Error] {type(e).__name__}: {e}")
        await safe_edit(status_msg, f"❌ **Error:** {type(e).__name__}: {e}", force=True)
        return False
    finally:
        if own_db:
            save_db(db)
        _forget(status_msg)
        for f in (source_file, poster_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        for f in os.listdir("."):
            if f.startswith("temp_") and f.endswith(".mp4"):
                try:
                    os.remove(f)
                except Exception:
                    pass


# --- SITE POLLING ---
def fetch_site_links():
    res = requests.get(SITE_URL, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(res.text, "html.parser")
    posts = soup.select(".utao .itao a") or soup.select("article a[href]")
    seen, out = set(), []
    for p in posts:
        link = p.get("href")
        if link and link not in seen:
            seen.add(link)
            out.append(link)
    return out


def pending_links(db):
    """Episodes that still have at least one quality missing."""
    return [l for l in fetch_site_links() if missing_qualities(db, l)]


async def process_links(links, status_msg, db):
    done = partial = skipped = 0
    for link in links:
        print(f"[Polling] Processing: {link}")
        msg = await app.send_message(CHANNEL_ID, "🚀 **Auto-Check: New Update**")
        result = await run_task(link, msg, db=db)
        save_db(db)
        if result is None:
            skipped += 1
        elif result:
            done += 1
        else:
            partial += 1
        await asyncio.sleep(10)
    return done, partial, skipped


async def trigger_manual_check(status_msg):
    db = load_db()
    links = pending_links(db)
    if not links:
        await safe_edit(status_msg, "✅ **Site checked. Everything is up to date!**",
                        force=True)
        return
    await safe_edit(status_msg, f"🔎 Found **{len(links)}** episode(s) needing work...",
                    force=True)
    done, partial, skipped = await process_links(links, status_msg, db)
    save_db(db)
    parts = [f"✅ Complete: **{done}**"]
    if partial:
        parts.append(f"⚠️ Partial: **{partial}** (re-run /chk to retry)")
    if skipped:
        parts.append(f"⏭️ No link yet: **{skipped}**")
    await safe_edit(status_msg, "**Auto-Check finished**\n" + "\n".join(parts), force=True)


async def scheduler_loop():
    print("[Scheduler] Background daemon initialized.")
    await asyncio.sleep(10)
    while True:
        try:
            now = datetime.datetime.now()
            th, tm = map(int, SCHEDULE_TIME.split(":"))
            if now.hour == th and now.minute == tm:
                today = now.date()
                if getattr(scheduler_loop, "last_run_date", None) != today:
                    scheduler_loop.last_run_date = today
                    print(f"[Scheduler] Daily check at {SCHEDULE_TIME}")
                    db = load_db()
                    links = pending_links(db)
                    if links:
                        msg = await app.send_message(
                            CHANNEL_ID, "📢 **Scheduler: checking for updates...**")
                        await process_links(links, msg, db)
                        save_db(db)
            await asyncio.sleep(45)
        except Exception as e:
            print(f"[Scheduler Error] {e}")
            await asyncio.sleep(30)


# --- COMMANDS ---
URL_RE = re.compile(r"https?://\S+")


@app.on_message(filters.command("chk"))
async def chk_command(c, m):
    """/chk            -> scan the site for anything incomplete
       /chk <ep link>  -> retry ONLY the qualities that failed for that episode"""
    urls = URL_RE.findall(m.text or "")
    if urls:
        url = urls[0].rstrip(").,")
        db = load_db()
        miss = missing_qualities(db, url)
        if not miss:
            await m.reply(f"✅ **Already complete** — all qualities uploaded.\n"
                          f"Use `/force {url}` to re-upload everything.")
            return
        msg = await m.reply(f"⚙️ **Retrying only:** `{', '.join(miss)}`")
        await run_task(url, msg, db=db)
        save_db(db)
    else:
        await trigger_manual_check(await m.reply("⚙️ **Starting Check Process...**"))


# Kept for backwards compatibility -- same behaviour as /chk <link>
@app.on_message(filters.command("chklink"))
async def chklink_command(c, m):
    urls = URL_RE.findall(m.text or "")
    if not urls:
        await m.reply("Usage: `/chklink <url>`")
        return
    db = load_db()
    url = urls[0].rstrip(").,")
    msg = await m.reply("⚙️ **Manual Processing English Sub...**")
    await run_task(url, msg, db=db)
    save_db(db)


@app.on_message(filters.command("force"))
async def force_command(c, m):
    """Re-upload every quality for a link, ignoring what is already done."""
    urls = URL_RE.findall(m.text or "")
    if not urls:
        await m.reply("Usage: `/force <url>`")
        return
    db = load_db()
    url = urls[0].rstrip(").,")
    entry_for(db, url)["done"] = []
    msg = await m.reply("⚙️ **Force re-uploading all qualities...**")
    await run_task(url, msg, db=db, only_missing=False)
    save_db(db)


@app.on_message(filters.command("re_upload"))
async def reupload_command(c, m):
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    await trigger_manual_check(await m.reply("🗑️ **Database cleared. Re-checking...**"))


@app.on_message(filters.command("status"))
async def status_command(c, m):
    db = load_db()
    total, used, free = shutil.disk_usage(".")
    incomplete = [(u, missing_qualities(db, u)) for u in db if missing_qualities(db, u)]
    lines = [
        f"🧠 Encoded: `{', '.join(ENCODE_QUALITIES)}`",
        f"📼 Passthrough: `{PASSTHROUGH_QUALITY}` "
        f"(re-encode above `{PASSTHROUGH_MAX_BYTES/1073741824:.2f}GB`)",
        f"⚙️ Preset: `{X264_PRESET}` • Threads: `{FFMPEG_THREADS}`",
        f"💾 Disk free: `{free/1073741824:.1f} GB`",
        f"📚 Episodes tracked: `{len(db)}`",
    ]
    if incomplete:
        lines.append(f"\n⚠️ **{len(incomplete)} incomplete:**")
        for u, miss in incomplete[:8]:
            title = db[u].get("title") or u
            note = "no mediafire link yet" if db[u].get("no_link") else ", ".join(miss)
            lines.append(f"• {title} — `{note}`")
    await m.reply("\n".join(lines))


async def start_bot():
    await app.start()
    print("Bot starting up...")
    asyncio.create_task(scheduler_loop())
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(start_bot())
