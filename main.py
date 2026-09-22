# ==========================================================
# a.py — multi-BOT, multi-API runner for Railway
#   pip install -r requirements.txt
#   python a.py
# ==========================================================
import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from dotenv import load_dotenv
from PIL import Image, ImageFilter
from pymongo import MongoClient
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, InputFile,
    ReplyKeyboardRemove, ChatMember,
)
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, filters,
)

load_dotenv()

MONGO_USER = os.getenv("MONGO_USER", "").strip()
MONGO_PASS = os.getenv("MONGO_PASS", "").strip()
MONGO_HOST = os.getenv("MONGO_HOST", "localhost").strip()
MONGO_PORT = int(os.getenv("MONGO_PORT", "27017") or 27017)
MONGO_DB   = os.getenv("MONGO_DB", "").strip()

BLUR_PERCENTAGE = int(os.getenv("BLUR_PERCENTAGE", "20") or 20)
UPLOAD_TIMEOUT  = int(os.getenv("UPLOAD_TIMEOUT", "1800") or 1800)
PROGRESS_EDIT_INTERVAL = int(os.getenv("PROGRESS_EDIT_INTERVAL", "2") or 2)

if not (MONGO_USER and MONGO_PASS and MONGO_DB):
    raise SystemExit("✗ MongoDB credentials missing in .env")

MONGO_URI = (
    f"mongodb://{quote_plus(MONGO_USER)}:{quote_plus(MONGO_PASS)}"
    f"@{MONGO_HOST}:{MONGO_PORT}/?authSource={MONGO_DB}"
)

STORAGE = Path(__file__).parent / "storage"
STORAGE.mkdir(exist_ok=True)
TMP_DIR = STORAGE / "tmp"
TMP_DIR.mkdir(exist_ok=True)
LOG_FILE  = STORAGE / "bot_log.txt"

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db    = mongo[MONGO_DB]

users_col      = db["users"]
api_col        = db["api_sources"]
target_col     = db["api_targets"]
welcome_col    = db["welcome_settings"]
tracking_col   = db["api_tracking"]
adminstate_col = db["admin_state"]
bots_col       = db["bots_config"] # Multi-bot DB

def blog(msg: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

def get_api(api_id: str) -> dict: return api_col.find_one({"_id": api_id}) or {}
def get_all_apis() -> list: return list(api_col.find())
def get_targets(api_id: str) -> list: return list(target_col.find({"api_id": api_id}))

def get_tracking(api_id: str) -> dict:
    doc = tracking_col.find_one({"_id": api_id}) or {}
    doc.setdefault("processed_slugs", [])
    doc.setdefault("in_progress_slugs", [])
    doc.pop("_id", None)
    return doc

def save_tracking(api_id: str, t: dict):
    t = dict(t); t.pop("_id", None)
    tracking_col.update_one({"_id": api_id}, {"$set": t}, upsert=True)

def get_users() -> list: return list(users_col.find())

def add_user(uid, username, first_name) -> bool:
    r = users_col.update_one(
        {"_id": uid},
        {"$setOnInsert": {"username": username, "first_name": first_name, "date": time.strftime("%Y-%m-%d %H:%M:%S")}},
        upsert=True,
    )
    return r.upserted_id is not None

# MODIFIED SLIGHTLY FOR MULTI-BOT STATE ISOLATION
def get_state(ctx) -> str:
    bot_id = ctx.bot_data.get("bot_id", "default")
    return (adminstate_col.find_one({"_id": f"state_{bot_id}"}) or {}).get("value", "")

def set_state(ctx, v: str):
    bot_id = ctx.bot_data.get("bot_id", "default")
    adminstate_col.update_one({"_id": f"state_{bot_id}"}, {"$set": {"value": v}}, upsert=True)

def get_welcome(chat_id: int) -> dict: return welcome_col.find_one({"_id": chat_id}) or {}
def esc(s: str) -> str: return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists(): Path(p).unlink()
        except Exception: pass

async def api_get(url: str) -> dict:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=60) as r:
                return await r.json()
    except Exception as e:
        blog(f"api {url}: {e}"); return {}

async def edit_progress(bot, chat_id, msg_id, is_photo, text):
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text, parse_mode="HTML")
        else:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML")
    except TelegramError: pass

async def download_thumb(url: str):
    if not url: return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=30) as r: data = await r.read()
        if len(data) < 100: return None
        p = TMP_DIR / f"thumb_{int(time.time()*1000)}.jpg"
        p.write_bytes(data)
        Image.open(p).verify()
        return p
    except Exception: return None

def blur_image(src: Path, dst: Path, pct: int):
    img = Image.open(src).convert("RGB")
    r = max(1, int(pct / 3.33))
    img.filter(ImageFilter.GaussianBlur(radius=r)).save(dst, "JPEG", quality=90)

def finish_slug(api_id: str, slug: str, processed: bool):
    t = get_tracking(api_id)
    if processed and slug not in t["processed_slugs"]:
        t["processed_slugs"].append(slug)
        if len(t["processed_slugs"]) > 3000: t["processed_slugs"].pop(0)
    t["in_progress_slugs"] = [s for s in t["in_progress_slugs"] if s != slug]
    save_tracking(api_id, t)

class ProgressFile:
    def __init__(self, path: Path):
        self.path = path; self.f = open(path, "rb")
        self.total = path.stat().st_size; self.read_bytes = 0
    def read(self, size=-1):
        chunk = self.f.read(size); self.read_bytes += len(chunk)
        return chunk
    def seek(self, *a): return self.f.seek(*a)
    def tell(self): return self.f.tell()
    def close(self): self.f.close()
    @property
    def name(self): return str(self.path)
    def __len__(self): return self.total

async def stream_download(url, dest: Path, progress_cb=None) -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as r:
                if r.status != 200: return False
                total = int(r.headers.get("Content-Length", 0))
                done, last = 0, 0
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            now = time.time()
                            if now - last >= PROGRESS_EDIT_INTERVAL:
                                last = now
                                await progress_cb(done, total)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        blog(f"download {url}: {e}"); return False

async def send_video_with_progress(bot, chat_id, video_path, thumb_path, caption,
                                   admin_chat_id, admin_msg_id, use_photo,
                                   where, title, slug, page, total, item):
    reader = ProgressFile(video_path)
    stop = asyncio.Event()
    async def poll():
        while not stop.is_set():
            pct = int(reader.read_bytes / reader.total * 100) if reader.total else 0
            await edit_progress(bot, admin_chat_id, admin_msg_id, use_photo,
                f"📤 <b>Uploading to {where}...</b> {pct}%\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{total}")
            await asyncio.sleep(PROGRESS_EDIT_INTERVAL)
    task = asyncio.create_task(poll())
    try:
        await bot.send_video(chat_id=chat_id, video=InputFile(reader, filename=video_path.name),
                             thumbnail=InputFile(open(thumb_path, "rb"), filename="t.jpg") if thumb_path else None,
                             caption=caption, parse_mode="HTML", protect_content=True, supports_streaming=True,
                             write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
        return True
    except TelegramError as e: blog(f"upload {chat_id}: {e}"); return False
    finally:
        stop.set(); task.cancel(); reader.close()
        try: await task
        except asyncio.CancelledError: pass

def extract_status_change(chat_member_update):
    status_change = chat_member_update.difference().get("status")
    old_is_member, new_is_member = chat_member_update.difference().get("is_member", (None, None))
    if status_change is None: return None
    old_status, new_status = status_change
    was_member = old_status in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or (old_status == ChatMember.RESTRICTED and old_is_member is True)
    is_member = new_status in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or (new_status == ChatMember.RESTRICTED and new_is_member is True)
    return was_member, is_member

async def track_chats(update: Update, ctx):
    result = extract_status_change(update.my_chat_member)
    if result is None: return
    was_member, is_member = result
    chat = update.effective_chat
    if not was_member and is_member:
        blog(f"Bot added to {chat.type}: {chat.id} ({chat.title})")
        try:
            admin_id = ctx.bot_data.get("admin_id")
            await ctx.bot.send_message(admin_id, f"🤖 <b>Bot added</b>\n\nType: {chat.type}\nTitle: {chat.title}\nID: <code>{chat.id}</code>", parse_mode="HTML")
        except Exception: pass

async def welcome_new_member(update: Update, ctx):
    result = extract_status_change(update.chat_member)
    if result is None: return
    was_member, is_member = result
    if was_member or not is_member: return
    chat = update.effective_chat
    user = update.chat_member.new_chat_member.user
    cfg = get_welcome(chat.id)
    if not cfg.get("enabled", True): return
    template = cfg.get("message") or "👋 Welcome {mention} to <b>{chat}</b>!\n\n📢 Join @virulvideopompom"
    try:
        text = template.format(mention=user.mention_html(), chat=esc(chat.title or "the group"),
                               first_name=esc(user.first_name or "friend"), username=("@" + user.username) if user.username else "")
        await ctx.bot.send_message(chat.id, text, parse_mode="HTML")
    except Exception as e: blog(f"welcome {chat.id}: {e}")

def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Status")],
                                [KeyboardButton("▶️ Run Cron"),  KeyboardButton("📥 Browse Videos")],
                                [KeyboardButton("⌨️ Hide Keyboard")]], resize_keyboard=True)
def cancel_keyboard() -> ReplyKeyboardMarkup: return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)
def hidden_keyboard() -> ReplyKeyboardMarkup: return ReplyKeyboardMarkup([[KeyboardButton("↩️ Show Keyboard")]], resize_keyboard=True)

# EXACT FEATURES - ONLY CHANGED `set_state("")` TO `set_state(ctx, "")`
async def feat_start(update, ctx, state):
    set_state(ctx, "")
    tg_api_server = ctx.bot_data.get("api_server")
    kb_note = "" if tg_api_server else "\n\n⚠️ Uploads limited to 50 MB (no Bot API server configured)."
    await update.message.reply_text(f"👋 <b>Welcome Admin.</b>{kb_note}", parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_cancel(update, ctx, state):
    set_state(ctx, "")
    await update.message.reply_text("❌ Cancelled.", reply_markup=admin_keyboard())

async def feat_hide(update, ctx, state):
    await update.message.reply_text("⌨️ Hidden.", reply_markup=hidden_keyboard())

async def feat_status(update, ctx, state):
    apis = get_all_apis()
    lines = ["📊 <b>Bot Status</b>\n"]
    for a in apis:
        t = get_tracking(a["_id"]); targets = get_targets(a["_id"])
        lines.append(f"<b>{esc(a['name'])}</b>\n  ⚙️ Mode: {a.get('mode','ON')}\n  📄 Page: {a.get('current_page', 1)}\n  ✅ Processed: {len(t['processed_slugs'])}\n  🎯 Targets: {len(targets)}")
    lines.append(f"\n👥 Users: {users_col.count_documents({})}")
    
    tg_api_server = ctx.bot_data.get("api_server")
    lines.append(f"\n🌐 Bot API: {'self-hosted' if tg_api_server else 'api.telegram.org (50 MB)'}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_broadcast_prompt(update, ctx, state):
    set_state(ctx, "waiting_broadcast")
    await update.message.reply_text("📝 Send the message to broadcast.", reply_markup=cancel_keyboard())

async def feat_broadcast_send(update, ctx, state):
    set_state(ctx, "")
    users = get_users()
    await update.message.reply_text(f"🚀 Broadcasting to {len(users)} users...")
    ok = 0
    for u in users:
        try:
            await update.message.bot.copy_message(chat_id=u["_id"], from_chat_id=update.message.chat.id, message_id=update.message.message_id)
            ok += 1
        except Exception as e: blog(f"broadcast {u['_id']}: {e}")
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Sent to {ok} users.", reply_markup=admin_keyboard())

async def feat_browse_prompt(update, ctx, state):
    set_state(ctx, "waiting_api")
    apis = get_all_apis()
    if not apis:
        await update.message.reply_text("❌ No API sources. Add one in the web panel.")
        return
    kb = [[InlineKeyboardButton(f"🔌 {a['name']}", callback_data=f"apick_{a['_id']}")] for a in apis]
    await update.message.reply_text("Pick an API source:", reply_markup=InlineKeyboardMarkup(kb))

async def feat_pick_api(update, ctx, state):
    cq = update.callback_query
    api_id = cq.data.split("_", 1)[1]
    set_state(ctx, f"waiting_page:{api_id}")
    await cq.message.reply_text("🔢 Send a page number.", reply_markup=cancel_keyboard())

async def feat_browse_page(update, ctx, state):
    if not state.startswith("waiting_page:"): return
    api_id = state.split(":", 1)[1]
    text = update.message.text or ""
    if not text.isdigit(): return
    set_state(ctx, "")
    p = int(text); api = get_api(api_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data"):
        await update.message.reply_text(f"❌ No videos on page {p}.", reply_markup=admin_keyboard()); return
    vids, total, v = data["data"], len(data["data"]), data["data"][0]
    slug = str(v.get("slug") or v.get("id") or "")
    title = esc(v.get("title") or v.get("name") or "Untitled")
    thumb = v.get("thumbnail") or v.get("image") or ""
    caption = f"🎬 <b>{title}</b>\n\n📄 Page: {p} | Item: 1/{total}\n🆔 <code>{slug}</code>"
    row = [InlineKeyboardButton("⬇️ Download", callback_data=f"aget_{api_id}_{p}_0")]
    if total > 1: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"abrowse_{api_id}_{p}_1"))
    if thumb: await update.message.reply_photo(photo=thumb, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))
    else: await update.message.reply_text(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))

async def feat_browse_nav(update, ctx, state):
    cq = update.callback_query
    _, api_id, p, i = cq.data.split("_")
    p, i = int(p), int(i); api = get_api(api_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data"): return
    vids, total = data["data"], len(data["data"])
    if i >= total: i = 0
    v = vids[i]
    slug = str(v.get("slug") or v.get("id") or "")
    title = esc(v.get("title") or v.get("name") or "Untitled")
    thumb = v.get("thumbnail") or v.get("image") or ""
    caption = f"🎬 <b>{title}</b>\n\n📄 Page: {p} | Item: {i+1}/{total}\n🆔 <code>{slug}</code>"
    row = []
    if i > 0: row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"abrowse_{api_id}_{p}_{i-1}"))
    row.append(InlineKeyboardButton("⬇️ Download", callback_data=f"aget_{api_id}_{p}_{i}"))
    if i < total - 1: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"abrowse_{api_id}_{p}_{i+1}"))
    try: await cq.message.delete()
    except Exception: pass
    if thumb: await cq.message.chat.send_photo(photo=thumb, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))
    else: await cq.message.chat.send_message(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))

async def feat_download(update, ctx, state):
    cq = update.callback_query
    _, api_id, p, i = cq.data.split("_")
    p, i = int(p), int(i)
    chat_id = cq.message.chat.id
    bot = ctx.bot; api = get_api(api_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data") or i >= len(data["data"]):
        await bot.send_message(chat_id, "❌ Video not found."); return
    v = data["data"][i]
    slug = str(v.get("slug") or v.get("id") or "")
    title = esc(v.get("title") or v.get("name") or "Untitled")
    thumb_url = v.get("thumbnail") or v.get("image") or ""
    vid_url = f"{api['base_url']}?action={api['video_action']}&id={slug}"
    vd = await api_get(vid_url)
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await bot.send_message(chat_id, f"❌ No download link.\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>", parse_mode="HTML"); return
    
    bot_id = ctx.bot_data.get("bot_id", "default")
    tmp = TMP_DIR / f"browse_{bot_id}_{int(time.time()*1000)}.mp4"
    if not await stream_download(link, tmp):
        await bot.send_message(chat_id, "❌ Download failed."); cleanup(tmp); return
    thumb = await download_thumb(thumb_url)
    try:
        await bot.send_video(chat_id=chat_id, video=InputFile(open(tmp, "rb"), filename=tmp.name),
            thumbnail=InputFile(open(thumb, "rb"), filename="t.jpg") if thumb else None,
            caption=f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>", parse_mode="HTML", supports_streaming=True,
            write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
    except TelegramError as e:
        blog(f"browse upload: {e}")
        await bot.send_message(chat_id, "❌ Upload failed.")
    cleanup(tmp, thumb)

async def feat_run_cron(update, ctx, state):
    apis = get_all_apis()
    if not apis:
        await update.message.reply_text("❌ No API sources. Add one in the web panel."); return
    kb = [[InlineKeyboardButton(f"▶️ {a['name']}", callback_data=f"crun_{a['_id']}")] for a in apis]
    await update.message.reply_text("Which API to run cron for?", reply_markup=InlineKeyboardMarkup(kb))

async def feat_cron_pick(update, ctx, state):
    cq = update.callback_query
    api_id = cq.data.split("_", 1)[1]
    await cq.message.reply_text(f"⏳ Running cron for {get_api(api_id).get('name','?')}...")
    await run_cron(ctx, ctx.bot, cq.message.chat.id, api_id)

async def feat_user(update, ctx, state):
    if update.callback_query: return
    msg = update.message
    if not msg or not msg.text: return
    uid = msg.from_user.id; first = msg.from_user.first_name or "User"; uname = msg.from_user.username or "None"
    
    admin_id = ctx.bot_data.get("admin_id")
    if add_user(uid, uname, first):
        try:
            await ctx.bot.send_message(admin_id, f"🆕 <b>New User</b>\n\n👤 {first}\n🔗 @{uname}\n🆔 <code>{uid}</code>", parse_mode="HTML")
        except Exception: pass
    if msg.text == "/start":
        await msg.reply_text(f"👋 Hello {first}!\n\n📢 Join @virulvideopompom\nhttps://t.me/+Uj_xq6904lMzYWVl", reply_markup=ReplyKeyboardRemove())

async def run_cron(ctx, bot, admin_chat_id: int, api_id: str):
    bot_id = ctx.bot_data.get("bot_id", "default")
    lock = STORAGE / f"cron_{bot_id}_{api_id}.lock"
    if lock.exists():
        if time.time() - lock.stat().st_mtime < UPLOAD_TIMEOUT:
            await bot.send_message(admin_chat_id, "⚠️ Cron already running for this API.")
            return
        lock.unlink()
    lock.write_text(str(os.getpid()))
    try: await _cron_pipeline(bot, admin_chat_id, api_id, bot_id)
    finally:
        try: lock.unlink()
        except FileNotFoundError: pass

async def _cron_pipeline(bot, admin_chat_id: int, api_id: str, bot_id: str):
    api = get_api(api_id)
    if not api:
        await bot.send_message(admin_chat_id, "❌ API not found."); return
    page = int(api.get("current_page", 1)); mode = api.get("mode", "ON")
    home_url = f"{api['base_url']}?action={api['home_action']}&page={page}"
    home = await api_get(home_url)
    if not home or not home.get("data"):
        await bot.send_message(admin_chat_id, f"❌ No videos on page {page}."); return
    videos = home["data"]; total = len(videos); t = get_tracking(api_id)
    v, idx = None, -1
    for i in range(total - 1, -1, -1):
        raw = videos[i]
        s = str(raw.get("slug") or raw.get("id") or "")
        if not s: continue
        if s in t["processed_slugs"] or s in t["in_progress_slugs"]: continue
        v, idx = raw, i; break
    if not v:
        new_page = max(1, page - 1)
        api_col.update_one({"_id": api_id}, {"$set": {"current_page": new_page}})
        await bot.send_message(admin_chat_id, f"✅ Page {page} done → next: {new_page}"); return

    title = esc(v.get("title") or v.get("name") or "Untitled")
    slug = str(v.get("slug") or v.get("id") or "")
    thumb_url = v.get("thumbnail") or v.get("image") or ""
    item = idx + 1
    t["in_progress_slugs"].append(slug)
    save_tracking(api_id, t)
    
    thumb = await download_thumb(thumb_url)
    init_text = (f"⏳ <b>Cron: {esc(api['name'])}</b>\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{total}\n⚙️ Mode: {mode}")
    if thumb:
        init = await bot.send_photo(admin_chat_id, photo=open(thumb, "rb"), caption=init_text, parse_mode="HTML")
        use_photo = True
    else:
        init = await bot.send_message(admin_chat_id, init_text, parse_mode="HTML")
        use_photo = False
    msg_id = init.message_id
    
    vid_url = f"{api['base_url']}?action={api['video_action']}&id={slug}"
    vd = await api_get(vid_url)
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"❌ No download link for <code>{slug}</code>")
        finish_slug(api_id, slug, processed=True); cleanup(thumb); return

    tmp = TMP_DIR / f"cron_{bot_id}_{int(time.time()*1000)}.mp4"
    async def on_dl(done, ttl):
        pct = int(done / ttl * 100)
        await edit_progress(bot, admin_chat_id, msg_id, use_photo,
            f"📥 <b>Downloading...</b> {pct}%\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{total}\n📦 {round(done/1048576,1)} MB / {round(ttl/1048576,1)} MB")
    
    if not await stream_download(link, tmp, on_dl):
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"❌ Download failed for <code>{slug}</code>")
        finish_slug(api_id, slug, processed=False); cleanup(thumb); return

    blur = None
    if thumb:
        blur = TMP_DIR / f"blur_{bot_id}_{int(time.time()*1000)}.jpg"
        try: blur_image(thumb, blur, BLUR_PERCENTAGE)
        except Exception: blur.write_bytes(thumb.read_bytes())

    targets = get_targets(api_id)
    if not targets:
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, "❌ No channels/groups configured for this API.")
        cleanup(tmp, thumb, blur); finish_slug(api_id, slug, processed=False); return

    channel_cap = "Join @virulvideopompom 🎬\n\nhttps://t.me/+Uj_xq6904lMzYWVl"
    group_cap = f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom"
    sent_as_video = False
    if mode == "ON":
        for tg in targets:
            cap = channel_cap if tg["type"] == "channel" else group_cap
            t_thumb = blur if tg["type"] == "channel" else thumb
            ok = await send_video_with_progress(bot, tg["chat_id"], tmp, t_thumb, cap, admin_chat_id, msg_id, use_photo, tg["type"], title, slug, page, total, item)
            if ok and tg["type"] == "channel": sent_as_video = True
            await asyncio.sleep(1)

    if not sent_as_video:
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"⚠️ Falling back to photo/link for <code>{slug}</code>")
        for tg in targets:
            try:
                if mode == "OFF" and thumb:
                    await bot.send_photo(tg["chat_id"], photo=open(thumb, "rb"), caption=group_cap, parse_mode="HTML", protect_content=True)
                else:
                    await bot.send_message(tg["chat_id"], f"🎬 <b>{title}</b>\n\n📥 {link}\n\n🔥 @virulvideopompom", parse_mode="HTML", protect_content=True)
            except TelegramError as e: blog(f"fallback {tg['chat_id']}: {e}")
            await asyncio.sleep(1)

    cleanup(tmp, thumb, blur); finish_slug(api_id, slug, processed=True)
    await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"✅ <b>Done!</b>\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{total}\n📤 Sent: {'video' if sent_as_video else 'fallback'}")

FEATURES = []
def register(order, handler, *, buttons=None, callbacks=None, admin_only=True, state=None, catch_all=False, state_prefix=None):
    FEATURES.append({ "order": order, "handler": handler, "buttons": buttons or [], "callbacks": callbacks or [], "admin_only": admin_only, "state": state, "catch_all": catch_all, "state_prefix": state_prefix })
    FEATURES.sort(key=lambda f: f["order"])

register(1,   feat_start,            buttons=["/start"])
register(2,   feat_cancel,           buttons=["❌ Cancel"])
register(3,   feat_hide,             buttons=["⌨️ Hide Keyboard"])
register(10,  feat_status,           buttons=["📊 Status"])
register(10,  feat_broadcast_prompt, buttons=["📢 Broadcast"])
register(50,  feat_broadcast_send,   state="waiting_broadcast")
register(10,  feat_browse_prompt,    buttons=["📥 Browse Videos"])
register(20,  feat_pick_api,         callbacks=["apick_"])
register(50,  feat_browse_page,      state_prefix="waiting_page:")
register(20,  feat_browse_nav,       callbacks=["abrowse_"])
register(20,  feat_download,         callbacks=["aget_"])
register(10,  feat_run_cron,         buttons=["▶️ Run Cron"])
register(20,  feat_cron_pick,        callbacks=["crun_"])
register(200, feat_user, admin_only=False, catch_all=True)

async def router(update: Update, ctx):
    if update.chat_member:
        await welcome_new_member(update, ctx)
        return
    chat = update.effective_chat
    if not chat or chat.type != "private": return
    user_id = update.effective_user.id if update.effective_user else None
    
    # GET DYNAMIC ADMIN
    admin_id = ctx.bot_data.get("admin_id")
    is_admin = (user_id == admin_id)

    if update.callback_query:
        await update.callback_query.answer()
        text = None; cb = update.callback_query.data or ""
    else:
        text = update.message.text if update.message else None
        cb = ""

    state = get_state(ctx) # Isolated State Check

    for f in FEATURES:
        if f["admin_only"] != is_admin: continue
        if f["state"] is not None and f["state"] != state: continue
        if f["state_prefix"] is not None and not state.startswith(f["state_prefix"]): continue
        matched = False
        if f["catch_all"]: matched = True
        elif text is not None and text in f["buttons"]: matched = True
        elif cb and any(cb.startswith(p) for p in f["callbacks"]): matched = True
        if matched:
            try: await f["handler"](update, ctx, state)
            except Exception as e:
                import traceback
                blog(f"feature {f['handler'].__name__}: {e}\n{traceback.format_exc()}")
            return
            
    if is_admin and text:
        await update.message.reply_text("👋 <b>Welcome Admin.</b>", parse_mode="HTML", reply_markup=admin_keyboard())

# ==========================================================
# MULTI-BOT DYNAMIC RUNNER
# ==========================================================
RUNNING_BOTS = {}

async def start_single_bot(bot_config: dict):
    b_id = str(bot_config.get("_id"))
    b_token = bot_config.get("token")
    b_admin = int(bot_config.get("admin_id", 0))
    b_api_server = bot_config.get("api_server", "").strip()

    builder = (
        ApplicationBuilder()
        .token(b_token)
        .media_write_timeout(UPLOAD_TIMEOUT)
        .read_timeout(UPLOAD_TIMEOUT)
        .write_timeout(UPLOAD_TIMEOUT)
        .connect_timeout(30)
        .pool_timeout(60)
    )

    if b_api_server:
        builder = builder.base_url(f"{b_api_server}/bot").base_file_url(f"{b_api_server}/file/bot")
        
    app = builder.build()
    
    app.bot_data["bot_id"] = b_id
    app.bot_data["admin_id"] = b_admin
    app.bot_data["api_server"] = b_api_server

    app.add_handler(MessageHandler(filters.ALL, router))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    logging.info(f"✓ Started bot: {bot_config.get('name', b_id)}")
    return app

async def watch_database():
    logging.info("Starting Dynamic Bot Loader. Monitoring database...")
    while True:
        try:
            active_bots = list(bots_col.find({"is_active": True}))
            active_ids = [str(b["_id"]) for b in active_bots]
            
            # Start new ones
            for bot_config in active_bots:
                bid = str(bot_config["_id"])
                if bid not in RUNNING_BOTS:
                    app = await start_single_bot(bot_config)
                    RUNNING_BOTS[bid] = app
                    
            # Stop deactivated ones
            for running_bid in list(RUNNING_BOTS.keys()):
                if running_bid not in active_ids:
                    logging.info(f"⚠ Stopping bot {running_bid} (deactivated in panel)")
                    app = RUNNING_BOTS[running_bid]
                    await app.updater.stop()
                    await app.stop()
                    del RUNNING_BOTS[running_bid]
                    
        except Exception as e:
            logging.error(f"Error checking database: {e}")
            
        await asyncio.sleep(60)

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        mongo.admin.command("ping")
        print(f"✓ Mongo connected: {MONGO_DB}")
    except Exception as e:
        print(f"✗ Mongo FAILED: {e}"); return

    try:
        asyncio.run(watch_database())
    except KeyboardInterrupt:
        print("\nShutting down bots...")

if __name__ == "__main__":
    main()
