import os
import json
import asyncio
import requests
import time
import re
import subprocess
import datetime
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import Message

# --- CONFIGURATION (ENVIRONMENT OR HARDCODED FALLBACK) ---
API_ID = int(os.environ.get("API_ID", ))  # Put your API ID here
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token_here")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", -1003966911002))
THUMB_URL = os.environ.get("THUMB_URL", "https://i.ibb.co/KjTqgMkS/x.jpg")
SITE_URL = os.environ.get("SITE_URL", "https://animexin.dev/")
DB_FILE = os.environ.get("DB_FILE", "processed_posts.json")
SCHEDULE_TIME = os.environ.get("SCHEDULE_TIME", "17:00")  # 5:00 PM (local system time)
# --------------------------------------------------------

app = Client("animexin_pro_v5", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DATABASE ---
def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f: 
                return json.load(f)
        except Exception:
            return []
    return []

def save_db(data):
    with open(DB_FILE, "w") as f: 
        json.dump(data, f)

# --- PROGRESS BAR UI ---
def get_progress_bar(current, total):
    try:
        pct = (current / total) * 100
        completed = int(pct / 10)
        bar = "🟢" * completed + "⚪" * (10 - completed)
        return f"|{bar}| {pct:.1f}%"
    except Exception:
        return "|⚪⚪⚪⚪⚪⚪⚪⚪⚪⚪| 0.0%"

async def update_progress_msg(current, total, msg, title, status_type):
    now = time.time()
    if not hasattr(update_progress_msg, "last_up"): 
        update_progress_msg.last_up = 0
    if now - update_progress_msg.last_up < 4: 
        return 
    update_progress_msg.last_up = now
    
    bar = get_progress_bar(current, total)
    try:
        await msg.edit(f"🎬 **{title}**\n\n{status_type}\n{bar}\n`{current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB`")
    except Exception: 
        pass

# --- SMART NAMING ENGINE ---
def clean_page_title(soup):
    h1 = soup.find("h1")
    if h1:
        name = h1.text.strip()
        name = re.split(r'Subtitle|Indonesia|English|Indo', name, flags=re.IGNORECASE)[0]
        name = name.replace("Episode", "Ep").strip()
        name = name.rstrip(',- ')
        return name
    return "Anime Episode"

def safe_filename(name):
    # Strip any characters that might break Linux terminal paths
    return re.sub(r'[^a-zA-Z0-9\s\.\-\[\]\(\)]', '', name).strip()

# --- MEDIAFIRE ENGLISH LINK FINDER ---
def find_english_mediafire(soup):
    all_links = soup.find_all('a', href=True)
    mediafire_links = []
    for link in all_links:
        href = link['href']
        if "mediafire.com" in href:
            mediafire_links.append(href)
    
    if len(mediafire_links) >= 2:
        print(f"Targeting English Mediafire: {mediafire_links[1]}")
        return mediafire_links[1]
    elif len(mediafire_links) == 1:
        return mediafire_links[0]
    return None

# --- ENCODING ENGINE ---
async def encode_video(input_f, output_f, res_p, msg, title):
    probe = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
        '-of', 'default=noprint_wrappers=1:nokey=1', input_f
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    
    try:
        duration = float(probe.stdout)
    except Exception:
        duration = 1.0  # Fallback duration to prevent division by zero
    
    res_val = res_p.replace("p", "")
    cmd = [
        'ffmpeg', '-i', input_f,
        '-vf', f'scale=-2:{res_val}',
        '-c:v', 'libx264', '-crf', '24', '-preset', 'ultrafast',  # 'ultrafast' saves heavy VPS CPU cycles
        '-threads', '1',  # Limit to 1 CPU thread to avoid VPS lockups
        '-c:a', 'copy', output_f, '-y'
    ]
    
    # Custom non-blocking stdout stream read loop (replaces readline to prevent LimitOverrun crashes)
    proc = await asyncio.create_subprocess_exec(
        *cmd, 
        stdout=asyncio.subprocess.PIPE, 
        stderr=asyncio.subprocess.STDOUT
    )
    
    buffer = ""
    while True:
        chunk = await proc.stdout.read(1024)
        if not chunk: 
            break
        buffer += chunk.decode('utf-8', errors='ignore')
        
        while "\r" in buffer or "\n" in buffer:
            r_idx = buffer.find("\r")
            n_idx = buffer.find("\n")
            split_idx = min(r_idx, n_idx) if (r_idx != -1 and n_idx != -1) else max(r_idx, n_idx)
            
            line = buffer[:split_idx].strip()
            buffer = buffer[split_idx + 1:]
            
            if "time=" in line:
                m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                if m:
                    curr_time = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                    pct = (curr_time / duration) * 100
                    bar = "" + "🟠" * int(pct/10) + "⚪" * (10 - int(pct/10))
                    try: 
                        await msg.edit(f"🎬 **{title}**\n\n⚙️ **Encoding {res_p}...**\n|{bar}| {pct:.1f}%")
                    except Exception: 
                        pass
                        
    await proc.wait()
    return os.path.exists(output_f)

# --- MAIN TASK ---
async def run_task(ep_url, status_msg):
    source_file = "raw_source.mp4"
    try:
        res = requests.get(ep_url, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        
        base_title = clean_page_title(soup)
        base_title = safe_filename(base_title)
        
        # 1. FIND THE SECOND MEDIAFIRE LINK (ENGLISH)
        mf_url = find_english_mediafire(soup)
        if not mf_url:
            await status_msg.edit(f"❌ English Mediafire link not found for:\n{base_title}")
            return False

        # 2. Get direct download link
        mf_res = requests.get(mf_url, timeout=20)
        mf_soup = BeautifulSoup(mf_res.text, 'html.parser')
        direct_download = mf_soup.find('a', {'id': 'downloadButton'})['href']

        # 3. Download Source File
        with requests.get(direct_download, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            curr = 0
            with open(source_file, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    f.write(chunk)
                    curr += len(chunk)
                    await update_progress_msg(curr, total, status_msg, base_title, "📥 **Downloading English Source**")

        # 4. Process all qualities sequentially
        for q in ["360p", "480p", "720p", "1080p"]:
            final_filename = f"{base_title} Eng Sub [{q}].mp4"
            temp_file = f"temp_{q}.mp4"
            
            # Encode
            encode_success = await encode_video(source_file, temp_file, q, status_msg, base_title)
            if not encode_success:
                print(f"Skipping upload for {q} due to encoding failure.")
                continue
            
            # Thumbnail download
            thumb = "thumb.jpg"
            if not os.path.exists(thumb):
                try:
                    with open(thumb, "wb") as f: 
                        f.write(requests.get(THUMB_URL, timeout=15).content)
                except Exception:
                    pass

            # Upload
            await app.send_document(
                chat_id=CHANNEL_ID,
                document=temp_file,
                thumb=thumb if os.path.exists(thumb) else None,
                file_name=final_filename,
                caption=f"🎬 **{base_title}**\n🔥 Quality: **{q}**\n✅ **English Subtitle**",
                progress=update_progress_msg,
                progress_args=(status_msg, base_title, f"📤 **Uploading {q}**")
            )
            
            # Delete temporary output file immediately to preserve disk space
            if os.path.exists(temp_file): 
                os.remove(temp_file)
            await asyncio.sleep(5)  # Rest time between encoding passes

        await status_msg.edit(f"✅ **Process Finished:**\n{base_title}")
        return True

    except Exception as e:
        await status_msg.edit(f"❌ **Error:** {str(e)}")
        return False
    finally:
        # Guarantee cleanup of raw video file
        if os.path.exists(source_file):
            try: 
                os.remove(source_file)
            except Exception: 
                pass

# --- MANUAL CHECK HELPER ---
async def trigger_manual_check(status_msg):
    db = load_db()
    res = requests.get(SITE_URL, timeout=20)
    soup = BeautifulSoup(res.text, 'html.parser')
    posts = soup.select(".utao .itao a")
    
    processed_count = 0
    for post in posts:
        link = post['href']
        if link not in db:
            msg = await app.send_message(CHANNEL_ID, "🚀 **Auto-Check: New Update**")
            success = await run_task(link, msg)
            if success:
                db.append(link)
                save_db(db)
                processed_count += 1
                await asyncio.sleep(10)  # Safe cooldown between whole episodes
                
    if processed_count == 0:
        await status_msg.edit("✅ **Site checked. Everything is up to date!**")
    else:
        await status_msg.edit(f"✅ **Manual Auto-Check complete! Processed {processed_count} posts.**")

# --- BACKGROUND AUTOMATION SCHEDULER ---
async def scheduler_loop():
    print("[Scheduler] Background daemon initialized.")
    await asyncio.sleep(10)  # Wait for Bot Startup
    
    while True:
        try:
            now = datetime.datetime.now()
            target_h, target_m = map(int, SCHEDULE_TIME.split(":"))
            
            if now.hour == target_h and now.minute == target_m:
                current_date = now.date()
                # Ensure the daily scheduler only runs once during the matching 17:00 minute
                if not hasattr(scheduler_loop, "last_run_date") or scheduler_loop.last_run_date != current_date:
                    scheduler_loop.last_run_date = current_date
                    print(f"[Scheduler] Running daily check at scheduled time: {SCHEDULE_TIME}")
                    
                    db = load_db()
                    res = requests.get(SITE_URL, timeout=20)
                    soup = BeautifulSoup(res.text, 'html.parser')
                    posts = soup.select(".utao .itao a")
                    
                    for post in posts:
                        link = post['href']
                        if link not in db:
                            # Start processing sequentially
                            msg = await app.send_message(CHANNEL_ID, f"📢 **Scheduler Trigger: New post detected at {SCHEDULE_TIME}!**")
                            success = await run_task(link, msg)
                            if success:
                                db.append(link)
                                save_db(db)
                                await asyncio.sleep(15)  # Cooldown before processing another episode
            
            # Sleep 45 seconds to keep checking and prevent double-triggering in the same minute
            await asyncio.sleep(45)
        except Exception as e:
            print(f"[Scheduler Error] {e}")
            await asyncio.sleep(10)

# --- COMMAND HANDLERS ---
@app.on_message(filters.command("chk"))
async def chk_command(c, m):
    msg = await m.reply("⚙️ **Starting Check Process...**")
    await trigger_manual_check(msg)

@app.on_message(filters.command("chklink"))
async def chklink_command(c, m):
    if len(m.command) < 2: 
        await m.reply("Usage: `/chklink <url>`")
        return
    url = m.command[1]
    msg = await m.reply("⚙️ **Manual Processing English Sub...**")
    await run_task(url, msg)

@app.on_message(filters.command("re_upload"))
async def reupload_command(c, m):
    if os.path.exists(DB_FILE): 
        os.remove(DB_FILE)
    msg = await m.reply("🗑️ **Database cleared. Triggering re-download auto-check...**")
    await trigger_manual_check(msg)

# --- START APP ---
async def start_bot():
    await app.start()
    print("Bot starting up...")
    asyncio.create_task(scheduler_loop())  # Register background scheduler
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(start_bot())
