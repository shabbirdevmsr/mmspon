# ==========================================================
# a.py — multi-BOT, multi-API runner for Railway
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
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, InputFile, ReplyKeyboardRemove, ChatMember
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, ChatMemberHandler, filters

load_dotenv()

MONGO_URI = f"mongodb://{quote_plus(os.getenv('MONGO_USER', ''))}:{quote_plus(os.getenv('MONGO_PASS', ''))}@{os.getenv('MONGO_HOST', 'localhost')}:{int(os.getenv('MONGO_PORT', '27017'))}/?authSource={os.getenv('MONGO_DB', '')}"

STORAGE, TMP_DIR = Path(__file__).parent / "storage", Path(__file__).parent / "storage" / "tmp"
STORAGE.mkdir(exist_ok=True); TMP_DIR.mkdir(exist_ok=True)
UPLOAD_TIMEOUT = 1800; PROGRESS_EDIT_INTERVAL = 2

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = mongo[os.getenv("MONGO_DB", "")]
users_col, api_col, target_col, welcome_col, tracking_col, adminstate_col, bots_col = db["users"], db["api_sources"], db["api_targets"], db["welcome_settings"], db["api_tracking"], db["admin_state"], db["bots_config"]

def blog(msg: str):
    with open(STORAGE / "bot_log.txt", "a", encoding="utf-8") as f: f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

def get_state(ctx): return (adminstate_col.find_one({"_id": f"state_{ctx.bot_data.get('bot_id')}"}) or {}).get("value", "")
def set_state(ctx, v): adminstate_col.update_one({"_id": f"state_{ctx.bot_data.get('bot_id')}"}, {"$set": {"value": v}}, upsert=True)
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

class ProgressFile:
    def __init__(self, path): self.path = path; self.f = open(path, "rb"); self.total = path.stat().st_size; self.read_bytes = 0
    def read(self, size=-1): chunk = self.f.read(size); self.read_bytes += len(chunk); return chunk
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

async def welcome_new_member(update: Update, ctx):
    diff = update.chat_member.difference()
    if diff.get("status") != (ChatMember.LEFT, ChatMember.MEMBER): return
    bot_id = ctx.bot_data.get("bot_id")
    chat = update.effective_chat
    user = update.chat_member.new_chat_member.user
    cfg = welcome_col.find_one({"_id": str(chat.id), "bot_id": bot_id})
    if not cfg: return

    text = cfg.get("message", "Welcome {mention}!").format(mention=user.mention_html(), chat=esc(chat.title), first_name=esc(user.first_name))
    
    # ATTEMPT DIRECT MESSAGE (DM)
    try:
        await ctx.bot.send_message(user.id, text, parse_mode="HTML")
    except TelegramError as e:
        blog(f"Bot {bot_id} could not DM user {user.id} (Have they started the bot?): {e}")

def admin_keyboard(): return ReplyKeyboardMarkup([[KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Status")], [KeyboardButton("▶️ Run Cron"), KeyboardButton("📥 Browse Videos")], [KeyboardButton("⌨️ Hide Keyboard")]], resize_keyboard=True)
def cancel_keyboard(): return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)

async def feat_start(update, ctx, state):
    set_state(ctx, "")
    await update.message.reply_text(f"👋 <b>Welcome Admin.</b>", parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_cancel(update, ctx, state):
    set_state(ctx, "")
    await update.message.reply_text("❌ Cancelled.", reply_markup=admin_keyboard())

async def feat_status(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    apis = list(api_col.find({"bot_id": bot_id}))
    lines = [f"📊 <b>Bot Status ({bot_id})</b>\n"]
    for a in apis:
        t = tracking_col.find_one({"_id": a["_id"]}) or {}
        targets = list(target_col.find({"api_id": a["_id"]}))
        lines.append(f"<b>{esc(a['name'])}</b>\n  📄 Page: {a.get('current_page', 1)}\n  ✅ Processed: {len(t.get('processed_slugs',[]))}\n  🎯 Targets: {len(targets)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_keyboard())

async def feat_run_cron(update, ctx, state):
    bot_id = ctx.bot_data.get("bot_id")
    apis = list(api_col.find({"bot_id": bot_id}))
    if not apis:
        await update.message.reply_text("❌ No API sources configured for this bot."); return
    kb = [[InlineKeyboardButton(f"▶️ {a['name']}", callback_data=f"crun_{a['_id']}")] for a in apis]
    await update.message.reply_text("Which API to run cron for?", reply_markup=InlineKeyboardMarkup(kb))

async def feat_cron_pick(update, ctx, state):
    cq = update.callback_query
    api_id = cq.data.split("_", 1)[1]
    await cq.message.reply_text(f"⏳ Running cron...")
    await _cron_pipeline(ctx.bot, cq.message.chat.id, api_id, ctx.bot_data.get("bot_id"))

async def feat_user(update, ctx, state):
    if update.callback_query: return
    msg = update.message; uid = msg.from_user.id
    users_col.update_one({"_id": uid}, {"$setOnInsert": {"first_name": msg.from_user.first_name, "date": time.strftime("%Y-%m-%d %H:%M:%S")}}, upsert=True)
    if msg.text == "/start":
        await msg.reply_text(f"👋 Hello {msg.from_user.first_name}!", reply_markup=ReplyKeyboardRemove())

async def _cron_pipeline(bot, admin_chat_id, api_id, bot_id):
    api = api_col.find_one({"_id": api_id, "bot_id": bot_id})
    if not api: return
    page = int(api.get("current_page", 1))
    data = await api_get(f"{api['base_url']}?action={api['home_action']}&page={page}")
    if not data or not data.get("data"):
        await bot.send_message(admin_chat_id, f"❌ No videos on page {page}."); return
    
    videos = data["data"]
    t = tracking_col.find_one({"_id": api_id}) or {"processed_slugs": []}
    
    v, idx = None, -1
    for i in range(len(videos) - 1, -1, -1):
        slug = str(videos[i].get("slug", videos[i].get("id", "")))
        if slug and slug not in t["processed_slugs"]: v, idx = videos[i], i; break
        
    if not v:
        api_col.update_one({"_id": api_id}, {"$set": {"current_page": max(1, page - 1)}})
        await bot.send_message(admin_chat_id, f"✅ Page {page} done."); return

    title, slug = esc(v.get("title", "Video")), str(v.get("slug", v.get("id")))
    init = await bot.send_message(admin_chat_id, f"⏳ <b>Cron Downloading</b>\n🎬 {title}\n🆔 <code>{slug}</code>", parse_mode="HTML")
    
    vd = await api_get(f"{api['base_url']}?action={api['video_action']}&id={slug}")
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        tracking_col.update_one({"_id": api_id}, {"$push": {"processed_slugs": slug}})
        return
        
    tmp = TMP_DIR / f"cron_{bot_id}_{int(time.time()*1000)}.mp4"
    async def on_dl(done, ttl):
        if ttl: await bot.edit_message_text(f"📥 <b>DL:</b> {int(done/ttl*100)}%", chat_id=admin_chat_id, message_id=init.message_id, parse_mode="HTML")
        
    if not await stream_download(link, tmp, on_dl): cleanup(tmp); return
    
    targets = list(target_col.find({"api_id": api_id}))
    for tg in targets:
        await send_video_with_progress(bot, tg["chat_id"], tmp, None, f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom", admin_chat_id, init.message_id, False, tg["type"], title, slug, page, len(videos), idx+1)
        await asyncio.sleep(1)

    tracking_col.update_one({"_id": api_id}, {"$push": {"processed_slugs": {"$each": [slug], "$slice": -3000}}})
    cleanup(tmp)
    await bot.edit_message_text(f"✅ <b>Done:</b> {title}", chat_id=admin_chat_id, message_id=init.message_id, parse_mode="HTML")

FEATURES = [
    {"order": 1, "handler": feat_start, "buttons": ["/start"], "admin_only": True, "state": None, "catch_all": False},
    {"order": 2, "handler": feat_cancel, "buttons": ["❌ Cancel"], "admin_only": True, "state": None, "catch_all": False},
    {"order": 10, "handler": feat_status, "buttons": ["📊 Status"], "admin_only": True, "state": None, "catch_all": False},
    {"order": 10, "handler": feat_run_cron, "buttons": ["▶️ Run Cron"], "admin_only": True, "state": None, "catch_all": False},
    {"order": 20, "handler": feat_cron_pick, "buttons": [], "callbacks": ["crun_"], "admin_only": True, "state": None, "catch_all": False},
    {"order": 200, "handler": feat_user, "buttons": [], "admin_only": False, "state": None, "catch_all": True}
]

async def router(update: Update, ctx):
    if update.chat_member: await welcome_new_member(update, ctx); return
    if not update.effective_chat or update.effective_chat.type != "private": return
    
    user_id = update.effective_user.id
    is_admin = (user_id == ctx.bot_data.get("admin_id"))
    text = update.message.text if update.message else None
    cb = update.callback_query.data if update.callback_query else ""
    if cb: await update.callback_query.answer()

    state = get_state(ctx)

    for f in FEATURES:
        if f["admin_only"] and not is_admin: continue
        if f["state"] is not None and f["state"] != state: continue
        if f["catch_all"] or (text and text in f["buttons"]) or (cb and any(cb.startswith(p) for p in f.get("callbacks", []))):
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
