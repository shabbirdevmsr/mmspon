# ==========================================================
# main.py — multi-BOT, multi-API runner for Railway
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

MONGO_URI = f"mongodb://{quote_plus(os.getenv('MONGO_USER', ''))}:{quote_plus(os.getenv('MONGO_PASS', ''))}@{os.getenv('MONGO_HOST', 'localhost')}:{int(os.getenv('MONGO_PORT', '27017'))}/?authSource={os.getenv('MONGO_DB', '')}"

STORAGE = Path(__file__).parent / "storage"
TMP_DIR = STORAGE / "tmp"
STORAGE.mkdir(exist_ok=True)
TMP_DIR.mkdir(exist_ok=True)

UPLOAD_TIMEOUT = 1800
PROGRESS_EDIT_INTERVAL = 2
BLUR_PERCENTAGE = 20

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = mongo[os.getenv("MONGO_DB", "")]
users_col = db["users"]
api_col = db["api_sources"]
target_col = db["api_targets"]
welcome_col = db["welcome_settings"]
tracking_col = db["api_tracking"]
adminstate_col = db["admin_state"]
bots_col = db["bots_config"]

def blog(msg: str):
    with open(STORAGE / "bot_log.txt", "a", encoding="utf-8") as f: 
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

# BOT-SPECIFIC DATABASE HELPERS
def get_state(bot_id): return (adminstate_col.find_one({"_id": f"state_{bot_id}"}) or {}).get("value", "")
def set_state(bot_id, v): adminstate_col.update_one({"_id": f"state_{bot_id}"}, {"$set": {"value": v}}, upsert=True)
def get_api(api_id, bot_id): return api_col.find_one({"_id": api_id, "bot_id": bot_id}) or {}
def get_all_apis(bot_id): return list(api_col.find({"bot_id": bot_id}))
def get_targets(api_id, bot_id): return list(target_col.find({"api_id": api_id, "bot_id": bot_id}))

def get_tracking(api_id):
    doc = tracking_col.find_one({"_id": api_id}) or {}
    doc.setdefault("processed_slugs", [])
    doc.setdefault("in_progress_slugs", [])
    doc.pop("_id", None)
    return doc

def save_tracking(api_id, t):
    t = dict(t); t.pop("_id", None)
    tracking_col.update_one({"_id": api_id}, {"$set": t}, upsert=True)

def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists(): Path(p).unlink()
        except: pass

async def api_get(url):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=60) as r: return await r.json()
    except Exception: return {}

# ==========================================================
# FILE / DOWNLOAD / UPLOAD HELPERS
# ==========================================================
class ProgressFile:
    def __init__(self, path): 
        self.path = path
        self.f = open(path, "rb")
        self.total = path.stat().st_size
        self.read_bytes = 0
    def read(self, size=-1): 
        chunk = self.f.read(size)
        self.read_bytes += len(chunk)
        return chunk
    def seek(self, *a): return self.f.seek(*a)
    def tell(self): return self.f.tell()
    def close(self): self.f.close()
    @property
    def name(self): return str(self.path)
    def __len__(self): return self.total

async def stream_download(url, dest, progress_cb=None):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as r:
                if r.status != 200: return False
                total, done, last = int(r.headers.get("Content-Length", 0)), 0, 0
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk); done += len(chunk)
                        if progress_cb and total:
                            if time.time() - last >= PROGRESS_EDIT_INTERVAL:
                                last = time.time(); await progress_cb(done, total)
        return dest.exists() and dest.stat().st_size > 0
    except Exception: return False

async def download_thumb(url):
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

def blur_image(src, dst, pct):
    img = Image.open(src).convert("RGB")
    r = max(1, int(pct / 3.33))
    img.filter(ImageFilter.GaussianBlur(radius=r)).save(dst, "JPEG", quality=90)

async def edit_progress(bot, chat_id, msg_id, is_photo, text):
    try:
        if is_photo: await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=text, parse_mode="HTML")
        else: await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML")
    except TelegramError: pass

async def send_video_with_progress(bot, chat_id, video_path, thumb_path, caption, admin_chat_id, admin_msg_id, use_photo, where, title, slug, page, total, item):
    reader = ProgressFile(video_path)
    stop = asyncio.Event()
    async def poll():
        while not stop.is_set():
            pct = int(reader.read_bytes / reader.total * 100) if reader.total else 0
            await edit_progress(bot, admin_chat_id, admin_msg_id, use_photo, f"📤 <b>Uploading to {where}...</b> {pct}%\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{total}")
            await asyncio.sleep(PROGRESS_EDIT_INTERVAL)
    task = asyncio.create_task(poll())
    try:
        await bot.send_video(chat_id=chat_id, video=InputFile(reader, filename=video_path.name), thumbnail=InputFile(open(thumb_path, "rb"), filename="t.jpg") if thumb_path else None, caption=caption, parse_mode="HTML", protect_content=True, supports_streaming=True, write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
        return True
    except TelegramError as e: 
        blog(f"upload {chat_id}: {e}")
        return False
    finally:
        stop.set(); task.cancel(); reader.close()
        try: await task
        except asyncio.CancelledError: pass


# ==========================================================
# ADMIN FEATURES & BOT LOGIC
# ==========================================================
def extract_status_change(chat_member_update):
    status_change = chat_member_update.difference().get("status")
    old_is_member, new_is_member = chat_member_update.difference().get("is_member", (None, None))
    if status_change is None: return None
    old_status, new_status = status_change
    was_member = old_status in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or (old_status == ChatMember.RESTRICTED and old_is_member is True)
    is_member = new_status in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or (new_status == ChatMember.RESTRICTED and new_is_member is True)
    return was_member, is_member

async def welcome_new_member(update: Update, ctx):
    diff = update.chat_member.difference()
    if diff.get("status") != (ChatMember.LEFT, ChatMember.MEMBER): return
    bot_id = ctx.bot_data.get("bot_id")
    chat = update.effective_chat
    user = update.chat_member.new_chat_member.user
    cfg = welcome_col.find_one({"_id": str(chat.id), "bot_id": bot_id})
    if not cfg: return

    text = cfg.get("message", "Welcome {mention}!").format(mention=user.mention_html(), chat=esc(chat.title), first_name=esc(user.first_name))
    try: await ctx.bot.send_message(user.id, text, parse_mode="HTML")
    except TelegramError as e: blog(f"Bot {bot_id} could not DM user {user.id}: {e}")

def admin_keyboard(): return ReplyKeyboardMarkup([[KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Status")], [KeyboardButton("▶️ Run Cron"), KeyboardButton("📥 Browse Videos")], [KeyboardButton("⌨️ Hide Keyboard")]], resize_keyboard=True)
def cancel_keyboard(): return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)
def hidden_keyboard(): return ReplyKeyboardMarkup([[KeyboardButton("↩️ Show Keyboard")]], resize_keyboard=True)

async def feat_start(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    set_state(bot_id, "")
    await update.message.reply_text(f"👋 <b>Welcome Admin.</b>", parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_cancel(update, ctx, state):
    set_state(ctx.bot_data.get("bot_id"), "")
    await update.message.reply_text("❌ Cancelled.", reply_markup=admin_keyboard())

async def feat_hide(update, ctx, state):
    await update.message.reply_text("⌨️ Hidden.", reply_markup=hidden_keyboard())

async def feat_status(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    apis = get_all_apis(bot_id)
    lines = [f"📊 <b>Bot Status</b>\n"]
    for a in apis:
        t = get_tracking(a["_id"]); targets = get_targets(a["_id"], bot_id)
        lines.append(f"<b>{esc(a['name'])}</b>\n  ⚙️ Mode: {a.get('mode','ON')}\n  📄 Page: {a.get('current_page', 1)}\n  ✅ Processed: {len(t['processed_slugs'])}\n  🎯 Targets: {len(targets)}")
    lines.append(f"\n👥 Users: {users_col.count_documents({})}")
    lines.append(f"\n🌐 Bot API: {'self-hosted' if ctx.bot_data.get('api_server') else 'api.telegram.org (50 MB)'}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_broadcast_prompt(update, ctx, state):
    set_state(ctx.bot_data.get("bot_id"), "waiting_broadcast")
    await update.message.reply_text("📝 Send the message to broadcast.", reply_markup=cancel_keyboard())

async def feat_broadcast_send(update, ctx, state):
    set_state(ctx.bot_data.get("bot_id"), "")
    users = list(users_col.find())
    await update.message.reply_text(f"🚀 Broadcasting to {len(users)} users...")
    ok = 0
    for u in users:
        try:
            await update.message.bot.copy_message(chat_id=u["_id"], from_chat_id=update.message.chat.id, message_id=update.message.message_id)
            ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Sent to {ok} users.", reply_markup=admin_keyboard())

async def feat_browse_prompt(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    set_state(bot_id, "waiting_api")
    apis = get_all_apis(bot_id)
    if not apis:
        await update.message.reply_text("❌ No API sources. Add one in the web panel.")
        return
    kb = [[InlineKeyboardButton(f"🔌 {a['name']}", callback_data=f"apick_{a['_id']}")] for a in apis]
    await update.message.reply_text("Pick an API source:", reply_markup=InlineKeyboardMarkup(kb))

async def feat_pick_api(update, ctx, state):
    cq = update.callback_query; api_id = cq.data.split("_", 1)[1]
    set_state(ctx.bot_data.get("bot_id"), f"waiting_page:{api_id}")
    await cq.message.reply_text("🔢 Send a page number.", reply_markup=cancel_keyboard())

async def feat_browse_page(update, ctx, state):
    if not state.startswith("waiting_page:"): return
    bot_id = ctx.bot_data.get("bot_id")
    api_id = state.split(":", 1)[1]
    text = update.message.text or ""
    if not text.isdigit(): return
    set_state(bot_id, ""); p = int(text)
    api = get_api(api_id, bot_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data"):
        await update.message.reply_text(f"❌ No videos on page {p}.", reply_markup=admin_keyboard()); return
    vids, total, v = data["data"], len(data["data"]), data["data"][0]
    slug = str(v.get("slug") or v.get("id") or ""); title = esc(v.get("title") or v.get("name") or "Untitled"); thumb = v.get("thumbnail") or v.get("image") or ""
    caption = f"🎬 <b>{title}</b>\n\n📄 Page: {p} | Item: 1/{total}\n🆔 <code>{slug}</code>"
    row = [InlineKeyboardButton("⬇️ Download", callback_data=f"aget_{api_id}_{p}_0")]
    if total > 1: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"abrowse_{api_id}_{p}_1"))
    if thumb: await update.message.reply_photo(photo=thumb, caption=caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))
    else: await update.message.reply_text(caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))

async def feat_browse_nav(update, ctx, state):
    cq = update.callback_query
    bot_id = ctx.bot_data.get("bot_id")
    _, api_id, p, i = cq.data.split("_"); p, i = int(p), int(i)
    api = get_api(api_id, bot_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data"): return
    vids, total = data["data"], len(data["data"])
    if i >= total: i = 0
    v = vids[i]
    slug = str(v.get("slug") or v.get("id") or ""); title = esc(v.get("title") or v.get("name") or "Untitled"); thumb = v.get("thumbnail") or v.get("image") or ""
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
    bot_id = ctx.bot_data.get("bot_id")
    _, api_id, p, i = cq.data.split("_"); p, i = int(p), int(i); chat_id = cq.message.chat.id
    api = get_api(api_id, bot_id)
    url = f"{api['base_url']}?action={api['home_action']}&page={p}"
    data = await api_get(url)
    if not data or not data.get("data") or i >= len(data["data"]):
        await ctx.bot.send_message(chat_id, "❌ Video not found."); return
    v = data["data"][i]; slug = str(v.get("slug") or v.get("id") or ""); title = esc(v.get("title") or v.get("name") or "Untitled")
    thumb_url = v.get("thumbnail") or v.get("image") or ""
    vid_url = f"{api['base_url']}?action={api['video_action']}&id={slug}"
    vd = await api_get(vid_url)
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await ctx.bot.send_message(chat_id, f"❌ No download link.\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>", parse_mode="HTML"); return
    
    tmp = TMP_DIR / f"browse_{bot_id}_{int(time.time()*1000)}.mp4"
    if not await stream_download(link, tmp):
        await ctx.bot.send_message(chat_id, "❌ Download failed."); cleanup(tmp); return
    thumb = await download_thumb(thumb_url)
    try:
        await ctx.bot.send_video(chat_id=chat_id, video=InputFile(open(tmp, "rb"), filename=tmp.name), thumbnail=InputFile(open(thumb, "rb"), filename="t.jpg") if thumb else None, caption=f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>", parse_mode="HTML", supports_streaming=True, write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
    except TelegramError as e:
        blog(f"browse upload: {e}"); await ctx.bot.send_message(chat_id, "❌ Upload failed.")
    cleanup(tmp, thumb)

async def feat_run_cron(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    apis = get_all_apis(bot_id)
    if not apis:
        await update.message.reply_text("❌ No API sources configured for this bot."); return
    kb = [[InlineKeyboardButton(f"▶️ {a['name']}", callback_data=f"crun_{a['_id']}")] for a in apis]
    await update.message.reply_text("Which API to run cron for?", reply_markup=InlineKeyboardMarkup(kb))

async def feat_cron_pick(update, ctx, state):
    cq = update.callback_query
    api_id = cq.data.split("_", 1)[1]
    await cq.message.reply_text(f"⏳ Running cron...")
    bot_id = ctx.bot_data.get("bot_id")
    await run_cron(ctx, ctx.bot, cq.message.chat.id, api_id, bot_id)

async def run_cron(ctx, bot, admin_chat_id, api_id, bot_id):
    lock = STORAGE / f"cron_{bot_id}_{api_id}.lock"
    if lock.exists():
        if time.time() - lock.stat().st_mtime < UPLOAD_TIMEOUT:
            await bot.send_message(admin_chat_id, "⚠️ Cron already running for this API."); return
        lock.unlink()
    lock.write_text(str(os.getpid()))
    try: await _cron_pipeline(bot, admin_chat_id, api_id, bot_id)
    finally:
        try: lock.unlink()
        except FileNotFoundError: pass

async def _cron_pipeline(bot, admin_chat_id, api_id, bot_id):
    api = get_api(api_id, bot_id)
    if not api: return
    page = int(api.get("current_page", 1)); mode = api.get("mode", "ON")
    data = await api_get(f"{api['base_url']}?action={api['home_action']}&page={page}")
    if not data or not data.get("data"):
        await bot.send_message(admin_chat_id, f"❌ No videos on page {page}."); return
    
    videos = data["data"]; t = get_tracking(api_id); v, idx = None, -1
    for i in range(len(videos) - 1, -1, -1):
        slug = str(videos[i].get("slug", videos[i].get("id", "")))
        if slug and slug not in t["processed_slugs"] and slug not in t["in_progress_slugs"]: v, idx = videos[i], i; break
        
    if not v:
        api_col.update_one({"_id": api_id}, {"$set": {"current_page": max(1, page - 1)}})
        await bot.send_message(admin_chat_id, f"✅ Page {page} done."); return

    title, slug, thumb_url = esc(v.get("title", v.get("name", "Video"))), str(v.get("slug", v.get("id", ""))), v.get("thumbnail", v.get("image", ""))
    item = idx + 1
    
    t["in_progress_slugs"].append(slug)
    save_tracking(api_id, t)
    
    thumb = await download_thumb(thumb_url)
    init_text = f"⏳ <b>Cron: {esc(api['name'])}</b>\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{len(videos)}\n⚙️ Mode: {mode}"
    if thumb:
        init = await bot.send_photo(admin_chat_id, photo=open(thumb, "rb"), caption=init_text, parse_mode="HTML")
        use_photo = True
    else:
        init = await bot.send_message(admin_chat_id, init_text, parse_mode="HTML")
        use_photo = False
    msg_id = init.message_id
    
    vd = await api_get(f"{api['base_url']}?action={api['video_action']}&id={slug}")
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"❌ No download link for <code>{slug}</code>")
        t["in_progress_slugs"].remove(slug); t["processed_slugs"].append(slug); save_tracking(api_id, t); cleanup(thumb); return
        
    tmp = TMP_DIR / f"cron_{bot_id}_{int(time.time()*1000)}.mp4"
    async def on_dl(done, ttl):
        pct = int(done / ttl * 100)
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"📥 <b>Downloading...</b> {pct}%\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{len(videos)}\n📦 {round(done/1048576,1)} MB / {round(ttl/1048576,1)} MB")
        
    if not await stream_download(link, tmp, on_dl):
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"❌ Download failed for <code>{slug}</code>")
        t["in_progress_slugs"].remove(slug); save_tracking(api_id, t); cleanup(thumb); return
    
    blur = None
    if thumb:
        blur = TMP_DIR / f"blur_{bot_id}_{int(time.time()*1000)}.jpg"
        try: blur_image(thumb, blur, BLUR_PERCENTAGE)
        except Exception: blur.write_bytes(thumb.read_bytes())

    targets = get_targets(api_id, bot_id)
    if not targets:
        await edit_progress(bot, admin_chat_id, msg_id, use_photo, "❌ No channels/groups configured for this API.")
        t["in_progress_slugs"].remove(slug); save_tracking(api_id, t); cleanup(tmp, thumb, blur); return

    channel_cap = "Join @virulvideopompom 🎬\n\nhttps://t.me/+Uj_xq6904lMzYWVl"
    group_cap = f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom"
    sent_as_video = False
    
    if mode == "ON":
        for tg in targets:
            cap = channel_cap if tg["type"] == "channel" else group_cap
            t_thumb = blur if tg["type"] == "channel" else thumb
            ok = await send_video_with_progress(bot, tg["chat_id"], tmp, t_thumb, cap, admin_chat_id, msg_id, use_photo, tg["type"], title, slug, page, len(videos), item)
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

    t["in_progress_slugs"].remove(slug); t["processed_slugs"].append(slug); save_tracking(api_id, t)
    if len(t["processed_slugs"]) > 3000: t["processed_slugs"].pop(0)
    
    cleanup(tmp, thumb, blur)
    await edit_progress(bot, admin_chat_id, msg_id, use_photo, f"✅ <b>Done!</b>\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n📄 Page: {page} | Item: {item}/{len(videos)}\n📤 Sent: {'video' if sent_as_video else 'fallback'}")

async def feat_user(update, ctx, state):
    if update.callback_query: return
    msg = update.message; uid = msg.from_user.id
    users_col.update_one({"_id": uid}, {"$setOnInsert": {"first_name": msg.from_user.first_name, "username": msg.from_user.username, "date": time.strftime("%Y-%m-%d %H:%M:%S")}}, upsert=True)
    if msg.text == "/start":
        await msg.reply_text(f"👋 Hello {msg.from_user.first_name}!", reply_markup=ReplyKeyboardRemove())

FEATURES = [
    {"order": 1, "handler": feat_start, "buttons": ["/start"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 2, "handler": feat_cancel, "buttons": ["❌ Cancel"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 3, "handler": feat_hide, "buttons": ["⌨️ Hide Keyboard"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 10, "handler": feat_status, "buttons": ["📊 Status"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 10, "handler": feat_broadcast_prompt, "buttons": ["📢 Broadcast"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 50, "handler": feat_broadcast_send, "buttons": [], "admin_only": True, "state": "waiting_broadcast", "catch_all": False, "state_prefix": None},
    {"order": 10, "handler": feat_browse_prompt, "buttons": ["📥 Browse Videos"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 20, "handler": feat_pick_api, "buttons": [], "callbacks": ["apick_"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 50, "handler": feat_browse_page, "buttons": [], "admin_only": True, "state": None, "catch_all": False, "state_prefix": "waiting_page:"},
    {"order": 20, "handler": feat_browse_nav, "buttons": [], "callbacks": ["abrowse_"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 20, "handler": feat_download, "buttons": [], "callbacks": ["aget_"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 10, "handler": feat_run_cron, "buttons": ["▶️ Run Cron"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 20, "handler": feat_cron_pick, "buttons": [], "callbacks": ["crun_"], "admin_only": True, "state": None, "catch_all": False, "state_prefix": None},
    {"order": 200, "handler": feat_user, "buttons": [], "admin_only": False, "state": None, "catch_all": True, "state_prefix": None}
]

async def router(update: Update, ctx):
    if update.chat_member: await welcome_new_member(update, ctx); return
    if not update.effective_chat or update.effective_chat.type != "private": return
    
    user_id = update.effective_user.id
    is_admin = (user_id == ctx.bot_data.get("admin_id"))
    text = update.message.text if update.message else None
    cb = update.callback_query.data if update.callback_query else ""
    if cb: await update.callback_query.answer()

    bot_id = ctx.bot_data.get("bot_id")
    state = get_state(bot_id)

    for f in FEATURES:
        if f["admin_only"] and not is_admin: continue
        if f["state"] is not None and f["state"] != state: continue
        if f["state_prefix"] is not None and not state.startswith(f["state_prefix"]): continue
        
        matched = False
        if f["catch_all"]: matched = True
        elif text is not None and text in f["buttons"]: matched = True
        elif cb and any(cb.startswith(p) for p in f.get("callbacks", [])): matched = True
        
        if matched:
            await f["handler"](update, ctx, state)
            return

    if is_admin and text:
        await update.message.reply_text("👋 <b>Welcome Admin.</b>", parse_mode="HTML", reply_markup=admin_keyboard())

# ==========================================================
# MULTI-BOT DYNAMIC RUNNER
# ==========================================================
RUNNING_BOTS = {}

async def start_single_bot(b):
    b_id, b_token, b_admin, b_api = b["_id"], b["token"], b["admin_id"], b.get("api_server", "").strip()
    builder = ApplicationBuilder().token(b_token).media_write_timeout(UPLOAD_TIMEOUT).read_timeout(UPLOAD_TIMEOUT).write_timeout(UPLOAD_TIMEOUT)
    if b_api: builder = builder.base_url(f"{b_api}/bot").base_file_url(f"{b_api}/file/bot")
    app = builder.build()
    app.bot_data.update({"bot_id": b_id, "admin_id": b_admin, "api_server": b_api})
    app.add_handler(MessageHandler(filters.ALL, router))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    await app.initialize(); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    logging.info(f"✓ Started bot: {b.get('name', b_id)}")
    return app

async def watch_database():
    logging.info("Starting Dynamic Bot Loader...")
    while True:
        try:
            active = list(bots_col.find({"is_active": True}))
            active_ids = [str(b["_id"]) for b in active]
            for b in active:
                if b["_id"] not in RUNNING_BOTS:
                    try: RUNNING_BOTS[b["_id"]] = await start_single_bot(b)
                    except Exception as e: logging.error(f"✗ Start fail {b['_id']}: {e}")
            for bid in list(RUNNING_BOTS.keys()):
                if bid not in active_ids:
                    logging.info(f"⚠ Stopping {bid} (deactivated)")
                    app = RUNNING_BOTS[bid]
                    try:
                        if app.updater and app.updater.running: await app.updater.stop()
                        if app.running: await app.stop()
                    except Exception as e: logging.warning(f"Ignored stop error for {bid}: {e}")
                    finally:
                        RUNNING_BOTS.pop(bid, None)
        except Exception as e: logging.error(f"DB Error: {e}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(watch_database())
