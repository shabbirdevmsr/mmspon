# ==========================================================
# main.py — BOT ENGINE ONLY (no web panel)
#   python main.py
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
from pymongo import MongoClient, ReturnDocument
from telegram import (Update, InlineKeyboardMarkup, InlineKeyboardButton,
                      ReplyKeyboardMarkup, KeyboardButton, InputFile,
                      ReplyKeyboardRemove, ChatMember)
from telegram.error import TelegramError
from telegram.ext import (ApplicationBuilder, MessageHandler,
                          CallbackQueryHandler, ChatMemberHandler, filters)

load_dotenv()

TG_API_SERVER          = os.getenv("TG_API_SERVER", "").strip()
UPLOAD_TIMEOUT         = int(os.getenv("UPLOAD_TIMEOUT", "1800") or 1800)
PROGRESS_EDIT_INTERVAL = int(os.getenv("PROGRESS_EDIT_INTERVAL", "2") or 2)
BLUR_PERCENTAGE        = int(os.getenv("BLUR_PERCENTAGE", "20") or 20)
BOT_RELOAD_INTERVAL    = int(os.getenv("BOT_RELOAD_INTERVAL", "15") or 15)

MONGO_URI = (f"mongodb://{quote_plus(os.getenv('MONGO_USER',''))}:"
             f"{quote_plus(os.getenv('MONGO_PASS',''))}"
             f"@{os.getenv('MONGO_HOST','localhost')}:{os.getenv('MONGO_PORT','27017')}"
             f"/?authSource={os.getenv('MONGO_DB','')}")

STORAGE = Path(__file__).parent / "storage"
STORAGE.mkdir(exist_ok=True)
TMP_DIR = STORAGE / "tmp"
TMP_DIR.mkdir(exist_ok=True)
LOG_FILE = STORAGE / "bot_log.txt"

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = mongo[os.getenv("MONGO_DB", "")]

bots_col       = db["bots"]
users_col      = db["users"]
api_col        = db["api_sources"]
target_col     = db["api_targets"]
welcome_col    = db["welcome_settings"]
tracking_col   = db["api_tracking"]
adminstate_col = db["admin_state"]
jobs_col       = db["cron_jobs"]

BOT_APPS = {}


# ==========================================================
# UTIL
# ==========================================================
def blog(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except Exception:
            pass


# ==========================================================
# DB HELPERS
# ==========================================================
def get_bot(bid):          return bots_col.find_one({"_id": bid}) or {}
def get_all_bots(en=True): return list(bots_col.find({"enabled": True} if en else {}))
def get_api(aid):          return api_col.find_one({"_id": aid}) or {}
def get_all_apis(bid):     return list(api_col.find({"bot_id": bid}))
def get_targets(aid):      return list(target_col.find({"api_id": aid}))
def get_users(bid):        return list(users_col.find({"bot_id": bid}))
def get_welcome(bid, cid): return welcome_col.find_one({"_id": f"{bid}:{cid}"}) or {}


def get_tracking(aid):
    d = tracking_col.find_one({"_id": aid}) or {}
    d.setdefault("processed_slugs", [])
    d.setdefault("in_progress_slugs", [])
    for k in ("_id", "bot_id", "api_id"):
        d.pop(k, None)
    return d


def save_tracking(aid, t):
    t = dict(t); t.pop("_id", None)
    tracking_col.update_one({"_id": aid}, {"$set": t}, upsert=True)


def add_user(bid, uid, uname, first):
    r = users_col.update_one({"_id": f"{bid}:{uid}"},
        {"$setOnInsert": {"bot_id": bid, "user_id": uid,
                          "username": uname, "first_name": first,
                          "date": time.strftime("%Y-%m-%d %H:%M:%S")}},
        upsert=True)
    return r.upserted_id is not None


def get_state(bid):
    return (adminstate_col.find_one({"_id": f"{bid}:state"}) or {}).get("value", "")


def set_state(bid, v):
    adminstate_col.update_one({"_id": f"{bid}:state"},
        {"$set": {"bot_id": bid, "value": v}}, upsert=True)


def finish_slug(aid, slug, processed):
    t = get_tracking(aid)
    if processed and slug not in t["processed_slugs"]:
        t["processed_slugs"].append(slug)
        if len(t["processed_slugs"]) > 3000:
            t["processed_slugs"].pop(0)
    t["in_progress_slugs"] = [s for s in t["in_progress_slugs"] if s != slug]
    save_tracking(aid, t)


# ==========================================================
# API HELPERS
# ==========================================================
async def api_get(url):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=60) as r:
                return await r.json()
    except Exception as e:
        blog(f"api {url}: {e}")
        return {}


def build_home_url(api, page):
    return api["home_url_template"].format(base_url=api["base_url"], page=page)


def build_video_url(api, vid):
    return api["video_url_template"].format(base_url=api["base_url"], id=vid)


def extract_video(raw, api):
    sf = api.get("slug_field", "slug")
    tf = api.get("title_field", "title")
    th = api.get("thumb_field", "thumbnail")
    return {
        "slug":  str(raw.get(sf) or raw.get("slug") or raw.get("id") or ""),
        "title": raw.get(tf) or raw.get("title") or raw.get("name") or "Untitled",
        "thumb": raw.get(th) or raw.get("thumbnail") or raw.get("image") or "",
    }


def next_page(cur, direction, max_page):
    if direction == "down":
        n = cur - 1
        return n if n >= 1 else None
    n = cur + 1
    if max_page and n > int(max_page):
        return None
    return n


# ==========================================================
# DOWNLOAD / UPLOAD
# ==========================================================
class ProgressFile:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        self.total = path.stat().st_size
        self.read_bytes = 0
    def read(self, size=-1):
        c = self.f.read(size); self.read_bytes += len(c); return c
    def seek(self, *a): return self.f.seek(*a)
    def tell(self): return self.f.tell()
    def close(self): self.f.close()
    @property
    def name(self): return str(self.path)
    def __len__(self): return self.total


async def edit_progress(bot, cid, mid, is_photo, text):
    try:
        if is_photo:
            await bot.edit_message_caption(chat_id=cid, message_id=mid,
                                           caption=text, parse_mode="HTML")
        else:
            await bot.edit_message_text(chat_id=cid, message_id=mid,
                                        text=text, parse_mode="HTML")
    except TelegramError:
        pass


async def stream_download(url, dest, progress_cb=None):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as r:
                if r.status != 200:
                    return False
                total = int(r.headers.get("Content-Length", 0))
                done, last = 0, 0
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(256 * 1024):
                        f.write(chunk); done += len(chunk)
                        if progress_cb and total:
                            now = time.time()
                            if now - last >= PROGRESS_EDIT_INTERVAL:
                                last = now
                                await progress_cb(done, total)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        blog(f"dl {url}: {e}")
        return False


async def download_thumb(url):
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=30) as r:
                data = await r.read()
        if len(data) < 100:
            return None
        p = TMP_DIR / f"t_{int(time.time()*1000)}.jpg"
        p.write_bytes(data)
        try:
            Image.open(p).verify(); return p
        except Exception:
            return None
    except Exception as e:
        blog(f"thumb: {e}")
        return None


def blur_image(src, dst, pct):
    img = Image.open(src).convert("RGB")
    r = max(1, int(pct / 3.33))
    img.filter(ImageFilter.GaussianBlur(radius=r)).save(dst, "JPEG", quality=90)


async def send_video_prog(bot, cid, video, thumb, caption,
                          admin_chat, admin_msg, use_photo,
                          where, title, slug, page, total, item):
    reader = ProgressFile(video)
    stop = asyncio.Event()
    async def poll():
        while not stop.is_set():
            pct = int(reader.read_bytes / reader.total * 100) if reader.total else 0
            await edit_progress(bot, admin_chat, admin_msg, use_photo,
                f"📤 <b>Uploading to {where}...</b> {pct}%\n\n"
                f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n"
                f"📄 {page} | {item}/{total}")
            await asyncio.sleep(PROGRESS_EDIT_INTERVAL)
    task = asyncio.create_task(poll())
    try:
        await bot.send_video(
            chat_id=cid,
            video=InputFile(reader, filename=video.name),
            thumbnail=InputFile(open(thumb, "rb"), filename="t.jpg") if thumb else None,
            caption=caption, parse_mode="HTML",
            protect_content=True, supports_streaming=True,
            write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
        return True
    except TelegramError as e:
        blog(f"up {cid}: {e}")
        return False
    finally:
        stop.set(); task.cancel(); reader.close()
        try: await task
        except asyncio.CancelledError: pass


# ==========================================================
# WELCOME
# ==========================================================
def _diff(u):
    sc = u.difference().get("status")
    om, nm = u.difference().get("is_member", (None, None))
    if sc is None: return None
    o, n = sc
    was = o in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or \
          (o == ChatMember.RESTRICTED and om is True)
    is_ = n in (ChatMember.MEMBER, ChatMember.OWNER, ChatMember.ADMINISTRATOR) or \
          (n == ChatMember.RESTRICTED and nm is True)
    return was, is_


async def track_chats(update, ctx):
    r = _diff(update.my_chat_member)
    if not r: return
    was, is_ = r
    if not was and is_:
        chat = update.effective_chat
        try:
            await ctx.bot.send_message(ctx.bot_data["admin_id"],
                f"🤖 <b>Bot added</b>\n\nType: {chat.type}\n"
                f"Title: {chat.title}\nID: <code>{chat.id}</code>",
                parse_mode="HTML")
        except Exception: pass


async def welcome_member(update, ctx):
    r = _diff(update.chat_member)
    if not r: return
    was, is_ = r
    if was or not is_: return
    chat = update.effective_chat
    user = update.chat_member.new_chat_member.user
    cfg = get_welcome(ctx.bot_data["bot_id"], chat.id)
    if not cfg.get("enabled", True): return
    tpl = cfg.get("message") or "👋 Welcome {mention} to <b>{chat}</b>!"
    try:
        await ctx.bot.send_message(chat.id, tpl.format(
            mention=user.mention_html(),
            chat=esc(chat.title or "the group"),
            first_name=esc(user.first_name or "friend"),
            username=("@" + user.username) if user.username else ""),
            parse_mode="HTML")
    except Exception as e:
        blog(f"welcome: {e}")


# ==========================================================
# KEYBOARDS
# ==========================================================
def admin_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📢 Broadcast"), KeyboardButton("📊 Status")],
         [KeyboardButton("▶️ Run Cron"),  KeyboardButton("📥 Browse Videos")],
         [KeyboardButton("⌨️ Hide Keyboard")]],
        resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)

def hidden_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("↩️ Show Keyboard")]], resize_keyboard=True)


# ==========================================================
# FEATURES
# ==========================================================
async def f_start(u, c, s):
    set_state(c.bot_data["bot_id"], "")
    await u.message.reply_text(
        f"👋 <b>{esc(c.bot_data['bot_name'])}</b> ready.",
        parse_mode="HTML", reply_markup=admin_kb())

async def f_cancel(u, c, s):
    set_state(c.bot_data["bot_id"], "")
    await u.message.reply_text("❌ Cancelled.", reply_markup=admin_kb())

async def f_hide(u, c, s):
    await u.message.reply_text("⌨️ Hidden.", reply_markup=hidden_kb())


async def f_status(u, c, s):
    bid = c.bot_data["bot_id"]
    apis = get_all_apis(bid)
    lines = [f"📊 <b>Status</b> — {esc(c.bot_data['bot_name'])}\n"]
    for a in apis:
        t = get_tracking(a["_id"])
        lines.append(
            f"<b>{esc(a['name'])}</b>\n"
            f"  ⚙️ {a.get('mode','ON')} | 📄 page {a.get('current_page',1)}"
            f" ({a.get('page_direction','down')})\n"
            f"  ✅ {len(t['processed_slugs'])} | 🎯 {len(get_targets(a['_id']))} targets")
    lines.append(f"\n👥 Users: {users_col.count_documents({'bot_id': bid})}")
    await u.message.reply_text("\n".join(lines),
        parse_mode="HTML", reply_markup=admin_kb())


async def f_bc_prompt(u, c, s):
    set_state(c.bot_data["bot_id"], "waiting_broadcast")
    await u.message.reply_text("📝 Send the message to broadcast.",
        reply_markup=cancel_kb())


async def f_bc_send(u, c, s):
    bid = c.bot_data["bot_id"]
    set_state(bid, "")
    users = get_users(bid)
    await u.message.reply_text(f"🚀 Broadcasting to {len(users)} users...")
    ok = 0
    for usr in users:
        try:
            await u.message.bot.copy_message(chat_id=usr["user_id"],
                from_chat_id=u.message.chat.id,
                message_id=u.message.message_id)
            ok += 1
        except Exception as e:
            blog(f"bc {usr['user_id']}: {e}")
        await asyncio.sleep(0.05)
    await u.message.reply_text(f"✅ Sent to {ok} users.", reply_markup=admin_kb())


async def f_browse_prompt(u, c, s):
    bid = c.bot_data["bot_id"]
    set_state(bid, "waiting_api")
    apis = get_all_apis(bid)
    if not apis:
        await u.message.reply_text("❌ No APIs.")
        return
    kb = [[InlineKeyboardButton(f"🔌 {a['name']}", callback_data=f"apick_{a['_id']}")]
          for a in apis]
    await u.message.reply_text("Pick an API:", reply_markup=InlineKeyboardMarkup(kb))


async def f_pick_api(u, c, s):
    cq = u.callback_query
    aid = cq.data.split("_", 1)[1]
    set_state(c.bot_data["bot_id"], f"waiting_page:{aid}")
    await cq.message.reply_text("🔢 Send a page number.", reply_markup=cancel_kb())


async def f_browse_page(u, c, s):
    if not s.startswith("waiting_page:"): return
    aid = s.split(":", 1)[1]
    txt = u.message.text or ""
    if not txt.isdigit(): return
    set_state(c.bot_data["bot_id"], "")
    p = int(txt)
    api = get_api(aid)
    data = await api_get(build_home_url(api, p))
    if not data or not data.get("data"):
        await u.message.reply_text(f"❌ No videos on page {p}.",
            reply_markup=admin_kb()); return
    vids, total = data["data"], len(data["data"])
    v = extract_video(vids[0], api)
    cap = (f"🎬 <b>{esc(v['title'])}</b>\n\n"
           f"📄 Page: {p} | Item: 1/{total}\n🆔 <code>{v['slug']}</code>")
    row = [InlineKeyboardButton("⬇️ Download", callback_data=f"aget_{aid}_{p}_0")]
    if total > 1:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"abrowse_{aid}_{p}_1"))
    if v["thumb"]:
        await u.message.reply_photo(photo=v["thumb"], caption=cap,
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))
    else:
        await u.message.reply_text(cap, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([row]))


async def f_browse_nav(u, c, s):
    cq = u.callback_query
    _, aid, p, i = cq.data.split("_")
    p, i = int(p), int(i)
    api = get_api(aid)
    data = await api_get(build_home_url(api, p))
    if not data or not data.get("data"): return
    vids, total = data["data"], len(data["data"])
    if i >= total: i = 0
    v = extract_video(vids[i], api)
    cap = (f"🎬 <b>{esc(v['title'])}</b>\n\n"
           f"📄 Page: {p} | Item: {i+1}/{total}\n🆔 <code>{v['slug']}</code>")
    row = []
    if i > 0: row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"abrowse_{aid}_{p}_{i-1}"))
    row.append(InlineKeyboardButton("⬇️ Download", callback_data=f"aget_{aid}_{p}_{i}"))
    if i < total - 1: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"abrowse_{aid}_{p}_{i+1}"))
    try: await cq.message.delete()
    except Exception: pass
    if v["thumb"]:
        await cq.message.chat.send_photo(photo=v["thumb"], caption=cap,
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([row]))
    else:
        await cq.message.chat.send_message(cap, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([row]))


async def f_download(u, c, s):
    cq = u.callback_query
    _, aid, p, i = cq.data.split("_")
    p, i = int(p), int(i)
    cid = cq.message.chat.id
    api = get_api(aid)
    data = await api_get(build_home_url(api, p))
    if not data or not data.get("data") or i >= len(data["data"]):
        await c.bot.send_message(cid, "❌ Not found."); return
    v = extract_video(data["data"][i], api)
    vd = await api_get(build_video_url(api, v["slug"]))
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await c.bot.send_message(cid, "❌ No link.", parse_mode="HTML"); return
    tmp = TMP_DIR / f"br_{int(time.time()*1000)}.mp4"
    if not await stream_download(link, tmp):
        await c.bot.send_message(cid, "❌ Download failed.")
        cleanup(tmp); return
    thumb = await download_thumb(v["thumb"])
    try:
        await c.bot.send_video(chat_id=cid,
            video=InputFile(open(tmp, "rb"), filename=tmp.name),
            thumbnail=InputFile(open(thumb, "rb"), filename="t.jpg") if thumb else None,
            caption=f"🎬 <b>{esc(v['title'])}</b>\n🆔 <code>{v['slug']}</code>",
            parse_mode="HTML", supports_streaming=True,
            write_timeout=UPLOAD_TIMEOUT, read_timeout=UPLOAD_TIMEOUT)
    except TelegramError as e: blog(f"br: {e}")
    cleanup(tmp, thumb)


async def f_run_cron(u, c, s):
    bid = c.bot_data["bot_id"]
    apis = get_all_apis(bid)
    if not apis:
        await u.message.reply_text("❌ No APIs."); return
    kb = [[InlineKeyboardButton(f"▶️ {a['name']}", callback_data=f"crun_{a['_id']}")]
          for a in apis]
    await u.message.reply_text("Which API?", reply_markup=InlineKeyboardMarkup(kb))


async def f_cron_pick(u, c, s):
    cq = u.callback_query
    aid = cq.data.split("_", 1)[1]
    await cq.message.reply_text(f"⏳ Running cron for {get_api(aid).get('name','?')}...")
    await run_cron(c.bot, cq.message.chat.id, aid, c.bot_data["bot_id"])


async def f_user(u, c, s):
    if u.callback_query: return
    msg = u.message
    if not msg or not msg.text: return
    bid = c.bot_data["bot_id"]
    uid = msg.from_user.id
    first = msg.from_user.first_name or "User"
    uname = msg.from_user.username or "None"
    if add_user(bid, uid, uname, first):
        try:
            await c.bot.send_message(c.bot_data["admin_id"],
                f"🆕 <b>New User</b> ({esc(c.bot_data['bot_name'])})\n\n"
                f"👤 {first}\n🔗 @{uname}\n🆔 <code>{uid}</code>",
                parse_mode="HTML")
        except Exception: pass
    if msg.text == "/start":
        await msg.reply_text(
            f"👋 Hello {first}!\n\n📢 Join @virulvideopompom\n"
            f"https://t.me/+Uj_xq6904lMzYWVl",
            reply_markup=ReplyKeyboardRemove())


# ==========================================================
# ROUTER
# ==========================================================
FEATURES = []

def reg(order, h, buttons=None, callbacks=None, admin_only=True,
        state=None, catch_all=False, state_prefix=None):
    FEATURES.append({"order": order, "h": h, "buttons": buttons or [],
                     "callbacks": callbacks or [], "admin_only": admin_only,
                     "state": state, "catch_all": catch_all,
                     "state_prefix": state_prefix})
    FEATURES.sort(key=lambda f: f["order"])

reg(1,  f_start,           buttons=["/start"])
reg(2,  f_cancel,          buttons=["❌ Cancel"])
reg(3,  f_hide,            buttons=["⌨️ Hide Keyboard"])
reg(10, f_status,          buttons=["📊 Status"])
reg(10, f_bc_prompt,       buttons=["📢 Broadcast"])
reg(50, f_bc_send,         state="waiting_broadcast")
reg(10, f_browse_prompt,   buttons=["📥 Browse Videos"])
reg(20, f_pick_api,        callbacks=["apick_"])
reg(50, f_browse_page,     state_prefix="waiting_page:")
reg(20, f_browse_nav,      callbacks=["abrowse_"])
reg(20, f_download,        callbacks=["aget_"])
reg(10, f_run_cron,        buttons=["▶️ Run Cron"])
reg(20, f_cron_pick,       callbacks=["crun_"])
reg(200, f_user, admin_only=False, catch_all=True)


async def router(update, ctx):
    if update.chat_member:
        await welcome_member(update, ctx); return
    chat = update.effective_chat
    if not chat or chat.type != "private": return

    bid = ctx.bot_data["bot_id"]
    admin_id = ctx.bot_data["admin_id"]
    uid = update.effective_user.id if update.effective_user else None
    is_admin = (uid == admin_id)

    if update.callback_query:
        await update.callback_query.answer()
        text = None; cb = update.callback_query.data or ""
    else:
        text = update.message.text if update.message else None; cb = ""

    state = get_state(bid)
    for f in FEATURES:
        if f["admin_only"] != is_admin: continue
        if f["state"] is not None and f["state"] != state: continue
        if f["state_prefix"] is not None and not state.startswith(f["state_prefix"]): continue
        m = False
        if f["catch_all"]: m = True
        elif text is not None and text in f["buttons"]: m = True
        elif cb and any(cb.startswith(p) for p in f["callbacks"]): m = True
        if m:
            try: await f["h"](update, ctx, state)
            except Exception as e:
                import traceback
                blog(f"[{bid}] {f['h'].__name__}: {e}\n{traceback.format_exc()}")
            return
    if is_admin and text:
        await update.message.reply_text("👋 <b>Welcome Admin.</b>",
            parse_mode="HTML", reply_markup=admin_kb())


# ==========================================================
# CRON
# ==========================================================
async def run_cron(bot, admin_chat, aid, bid):
    lock = STORAGE / f"cron_{aid.replace(':','_')}.lock"
    if lock.exists():
        if time.time() - lock.stat().st_mtime < UPLOAD_TIMEOUT:
            await bot.send_message(admin_chat, "⚠️ Cron running."); return
        lock.unlink()
    lock.write_text(str(os.getpid()))
    try: await _cron_pipeline(bot, admin_chat, aid, bid)
    finally:
        try: lock.unlink()
        except FileNotFoundError: pass


async def _cron_pipeline(bot, admin_chat, aid, bid):
    api = get_api(aid)
    if not api:
        await bot.send_message(admin_chat, "❌ API not found."); return

    page = int(api.get("current_page", 1))
    mode = api.get("mode", "ON")
    direction = api.get("page_direction", "down")
    max_page = api.get("max_page")

    home = await api_get(build_home_url(api, page))
    if not home or not home.get("data"):
        nxt = next_page(page, direction, max_page)
        if nxt is None:
            await bot.send_message(admin_chat, f"✅ All done for {esc(api['name'])}"); return
        api_col.update_one({"_id": aid}, {"$set": {"current_page": nxt}})
        await bot.send_message(admin_chat, f"⚠️ Page {page} empty → {nxt}"); return

    videos = home["data"]; total = len(videos)
    t = get_tracking(aid)

    v, idx = None, -1
    for i in range(total - 1, -1, -1):
        it = extract_video(videos[i], api)
        if not it["slug"]: continue
        if it["slug"] in t["processed_slugs"]: continue
        if it["slug"] in t["in_progress_slugs"]: continue
        v, idx = it, i; break

    if not v:
        nxt = next_page(page, direction, max_page)
        if nxt is None:
            await bot.send_message(admin_chat, f"✅ All done for {esc(api['name'])}"); return
        api_col.update_one({"_id": aid}, {"$set": {"current_page": nxt}})
        await bot.send_message(admin_chat, f"✅ Page {page} done → {nxt}"); return

    title = esc(v["title"]); slug = v["slug"]; item = idx + 1

    t["in_progress_slugs"].append(slug); save_tracking(aid, t)

    thumb = await download_thumb(v["thumb"])
    init = (f"⏳ <b>Cron: {esc(api['name'])}</b>\n\n🎬 <b>{title}</b>\n"
            f"🆔 <code>{slug}</code>\n📄 {page} | {item}/{total}\n⚙️ {mode}")
    if thumb:
        m = await bot.send_photo(admin_chat, photo=open(thumb, "rb"),
            caption=init, parse_mode="HTML"); use_photo = True
    else:
        m = await bot.send_message(admin_chat, init, parse_mode="HTML")
        use_photo = False
    mid = m.message_id

    vd = await api_get(build_video_url(api, slug))
    link = (vd or {}).get("data", {}).get("downloadLink", "")
    if not link:
        await edit_progress(bot, admin_chat, mid, use_photo,
            f"❌ No download link <code>{slug}</code>")
        finish_slug(aid, slug, True); cleanup(thumb); return

    tmp = TMP_DIR / f"c_{int(time.time()*1000)}.mp4"
    async def on_dl(done, ttl):
        pct = int(done / ttl * 100)
        await edit_progress(bot, admin_chat, mid, use_photo,
            f"📥 <b>Downloading...</b> {pct}%\n\n🎬 <b>{title}</b>\n"
            f"🆔 <code>{slug}</code>\n📄 {page} | {item}/{total}\n"
            f"📦 {round(done/1048576,1)}/{round(ttl/1048576,1)} MB")

    if not await stream_download(link, tmp, on_dl):
        await edit_progress(bot, admin_chat, mid, use_photo,
            f"❌ Download failed <code>{slug}</code>")
        finish_slug(aid, slug, False); cleanup(thumb); return

    blur = None
    if thumb:
        blur = TMP_DIR / f"b_{int(time.time()*1000)}.jpg"
        try: blur_image(thumb, blur, BLUR_PERCENTAGE)
        except Exception: blur.write_bytes(thumb.read_bytes())

    targets = get_targets(aid)
    if not targets:
        await edit_progress(bot, admin_chat, mid, use_photo, "❌ No targets.")
        cleanup(tmp, thumb, blur); finish_slug(aid, slug, False); return

    ch_cap = "Join @virulvideopompom 🎬\n\nhttps://t.me/+Uj_xq6904lMzYWVl"
    gr_cap = f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom"
    sent = False

    if mode == "ON":
        for tg in targets:
            cap = ch_cap if tg["type"] == "channel" else gr_cap
            tp = blur if tg["type"] == "channel" else thumb
            ok = await send_video_prog(bot, tg["chat_id"], tmp, tp, cap,
                admin_chat, mid, use_photo, tg["type"], title, slug, page, total, item)
            if ok and tg["type"] == "channel": sent = True
            await asyncio.sleep(1)

    if not sent:
        await edit_progress(bot, admin_chat, mid, use_photo, f"⚠️ Fallback <code>{slug}</code>")
        for tg in targets:
            try:
                if mode == "OFF" and thumb:
                    await bot.send_photo(tg["chat_id"], photo=open(thumb, "rb"),
                        caption=gr_cap, parse_mode="HTML", protect_content=True)
                else:
                    await bot.send_message(tg["chat_id"],
                        f"🎬 <b>{title}</b>\n\n📥 {link}\n\n🔥 @virulvideopompom",
                        parse_mode="HTML", protect_content=True)
            except TelegramError as e: blog(f"fb: {e}")
            await asyncio.sleep(1)

    cleanup(tmp, thumb, blur)
    finish_slug(aid, slug, True)
    await edit_progress(bot, admin_chat, mid, use_photo,
        f"✅ <b>Done!</b>\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n"
        f"📄 {page} | {item}/{total}\n📤 {'video' if sent else 'fallback'}")


# ==========================================================
# BOT RUNTIME
# ==========================================================
async def run_bot(cfg):
    bid = cfg["_id"]
    try:
        b = (ApplicationBuilder().token(cfg["token"])
             .media_write_timeout(UPLOAD_TIMEOUT)
             .read_timeout(UPLOAD_TIMEOUT)
             .write_timeout(UPLOAD_TIMEOUT)
             .connect_timeout(30).pool_timeout(60))
        if TG_API_SERVER:
            b = b.base_url(f"{TG_API_SERVER}/bot").base_file_url(f"{TG_API_SERVER}/file/bot")
        app = b.build()
        app.bot_data["bot_id"] = bid
        app.bot_data["admin_id"] = cfg["admin_id"]
        app.bot_data["bot_name"] = cfg.get("name", bid)
        app.add_handler(MessageHandler(filters.ALL, router))
        app.add_handler(CallbackQueryHandler(router))
        app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
        await app.initialize(); await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        BOT_APPS[bid] = app
        print(f"  ✓ {cfg['name']} ({bid})")
    except Exception as e:
        print(f"  ✗ {cfg.get('name','?')}: {e}")
        blog(f"bot {bid}: {e}")


async def bot_reloader():
    print(f"  ✓ reloader every {BOT_RELOAD_INTERVAL}s")
    while True:
        await asyncio.sleep(BOT_RELOAD_INTERVAL)
        try:
            for cfg in get_all_bots(enabled_only=True):
                if cfg["_id"] in BOT_APPS: continue
                print(f"  + starting {cfg['name']} ({cfg['_id']})")
                asyncio.create_task(run_bot(cfg))
        except Exception as e:
            print(f"reloader: {e}")


async def job_worker():
    print("  ✓ job worker polling")
    while True:
        try:
            job = jobs_col.find_one_and_update(
                {"status": "queued"},
                {"$set": {"status": "running",
                          "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}},
                return_document=ReturnDocument.AFTER)
            if not job:
                await asyncio.sleep(5); continue
            app = BOT_APPS.get(job["bot_id"])
            if not app:
                jobs_col.update_one({"_id": job["_id"]}, {"$set": {
                    "status": "error", "error": "Bot not running",
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}})
                continue
            try:
                await run_cron(app.bot, job["admin_chat_id"],
                               job["api_id"], job["bot_id"])
                jobs_col.update_one({"_id": job["_id"]}, {"$set": {
                    "status": "done",
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}})
            except Exception as e:
                import traceback
                jobs_col.update_one({"_id": job["_id"]}, {"$set": {
                    "status": "error", "error": str(e),
                    "traceback": traceback.format_exc(),
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}})
        except Exception as e:
            print(f"worker: {e}"); await asyncio.sleep(5)


async def async_main():
    try:
        mongo.admin.command("ping")
        print(f"✓ Mongo: {os.getenv('MONGO_DB','')}")
    except Exception as e:
        print(f"✗ Mongo FAILED: {e}"); return

    bots = get_all_bots(enabled_only=True)
    if not bots:
        print("⚠ No bots in DB. Add one at the panel → /bots")

    if TG_API_SERVER:
        print(f"✓ Bot API: {TG_API_SERVER}  (2 GB)")
    else:
        print("⚠ 50 MB limit (api.telegram.org)")

    print(f"✓ Starting {len(bots)} bot(s) from DB:")
    tasks = [asyncio.create_task(run_bot(b)) for b in bots]
    tasks.append(asyncio.create_task(job_worker()))
    tasks.append(asyncio.create_task(bot_reloader()))
    await asyncio.gather(*tasks)


def main():
    logging.basicConfig(level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    print("━━━ BOT ENGINE ━━━")
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nStopping...")


if __name__ == "__main__":
    main()
