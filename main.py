#!/usr/bin/env python3
# ==========================================================
# bot.py — Single-file Telegram Bot (python-telegram-bot v20+)
#   - FastAPI webhook + durable file queue + cron pipeline
#   - Custom Telegram API server with fallback
#   - Payments, inline keyboards, admin/user routing
# ==========================================================
import os
import json
import time
import random
import asyncio
import logging
import fcntl
from datetime import datetime
from contextlib import asynccontextmanager

import requests
from PIL import Image, ImageFilter
from fastapi import FastAPI, Request, Response, HTTPException
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, InputFile, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters,
)
from telegram.constants import ParseMode, ChatType

# ==========================================================
# CONFIG
# ==========================================================
BOT_TOKEN = '6984914369:AAFF4bC0124Wc1xMoa38WB4xn54wF5TJE3U'
ADMIN_ID = '5087403859'
CRON_SECRET = 'CHANGE_ME_random_long_string'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
DATA_FILE = os.path.join(BASE_DIR, 'sent_videos.json')
CHANNEL_POSTS_FILE = os.path.join(BASE_DIR, 'channel_posts.json')
LOG_FILE = os.path.join(BASE_DIR, 'bot_log.txt')
ADMIN_STATE = os.path.join(BASE_DIR, 'admin_state.txt')
QUEUE_FILE = os.path.join(BASE_DIR, 'queue.json')
PROCESSOR_LOCK = os.path.join(BASE_DIR, 'queue.lock')
SEEN_FILE = os.path.join(BASE_DIR, 'seen_updates.json')

API_BASE = 'https://shabbir.serv00.net/sex/mmsbaba/get.php'
API_VID65 = 'https://shabbir.serv00.net/sex/vid65/get.php'

# Custom Telegram API server — must end with a trailing slash
TG_API_SERVER = 'https://telegram-bot-api-production-29e4.up.railway.app/'
TG_API_SERVER_FALLBACK = 'https://api.telegram.org/'

BLUR_PERCENTAGE = 20
MAX_QUEUE_PROCESS = 100

GROUPS = ['-1003708527420']
CHANNELS = ['@virulvideopompom']

WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://yourdomain.com/webhook')
PORT = int(os.environ.get('PORT', 8443))

# ==========================================================
# LOGGING
# ==========================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def blog(msg: str):
    with open(LOG_FILE, 'a') as f:
        f.write(f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')


# ==========================================================
# GLOBALS (set during lifespan)
# ==========================================================
application: Application = None
bot = None


# ==========================================================
# FILE HELPERS
# ==========================================================
def read_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, (list, dict)) else (default if default is not None else [])
    except Exception:
        return default if default is not None else []


def write_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def get_tracking():
    t = read_json(DATA_FILE, {})
    if not isinstance(t, dict):
        t = {}
    t.setdefault('current_page', 203)
    t.setdefault('processed_slugs', [])
    t.setdefault('in_progress_slugs', [])
    t.setdefault('vid65_page', 1)
    t.setdefault('vid65_processed_ids', [])
    t.setdefault('vid65_in_progress_ids', [])
    t.setdefault('mode', 'ON')
    return t


def save_tracking(t):
    write_json(DATA_FILE, t)


# ==========================================================
# BLUR / CHANNEL HISTORY
# ==========================================================
def apply_blur(src, dst, pct):
    try:
        img = Image.open(src).convert('RGB')
        radius = max(1, int(round(pct * 0.4)))
        blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
        blurred.save(dst, 'JPEG', quality=90)
        return True
    except Exception as e:
        blog(f"Blur: {e}")
        return False


async def manage_channel_history(ch, mid):
    posts = read_json(CHANNEL_POSTS_FILE, {})
    posts.setdefault(ch, [])
    posts[ch].append(mid)
    if len(posts[ch]) > 10:
        old = posts[ch].pop(0)
        try:
            await bot.delete_message(chat_id=ch, message_id=old)
        except Exception:
            pass
    write_json(CHANNEL_POSTS_FILE, posts)


# ==========================================================
# API + HTML
# ==========================================================
def fetch_api_data(url):
    try:
        return requests.get(url, timeout=60).json()
    except Exception as e:
        blog(f"fetch_api_data: {e}")
        return None


def html_escape(s):
    if s is None:
        return ''
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#039;'))


# ==========================================================
# KEYBOARDS
# ==========================================================
def admin_keyboard():
    t = get_tracking()
    mode_txt = '🟢 Mode: ON' if t.get('mode', 'ON') == 'ON' else '🔴 Mode: OFF'
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton('📢 Broadcast'), KeyboardButton('📊 Status')],
            [KeyboardButton('▶️ Run Cron'), KeyboardButton(mode_txt)],
            [KeyboardButton('📥 Browse Videos')],
            [KeyboardButton('⌨️ Hide Keyboard')],
        ],
        resize_keyboard=True,
        is_persistent=False,
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton('❌ Cancel')]],
        resize_keyboard=True,
        is_persistent=False,
    )


def hidden_keyboard():
    return ReplyKeyboardRemove()


# ==========================================================
# QUEUE (durable, flock-serialized)
# ==========================================================
def queue_push(update_dict):
    f = open(QUEUE_FILE, 'a+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        q = read_json(QUEUE_FILE, [])
        if not isinstance(q, list):
            q = []
        uid = update_dict.get('update_id')
        if uid is not None:
            for item in q:
                if item.get('update_id') == uid:
                    return
        q.append(update_dict)
        if len(q) > 500:
            q = q[-500:]
        write_json(QUEUE_FILE, q)
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def queue_pop():
    f = open(QUEUE_FILE, 'a+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        q = read_json(QUEUE_FILE, [])
        if not isinstance(q, list) or not q:
            return None
        item = q.pop(0)
        write_json(QUEUE_FILE, q)
        return item
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


async def process_queue_worker():
    """Process queued updates — serialized by a lock file."""
    lock_fp = open(PROCESSOR_LOCK, 'a+')
    try:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return

        seen = read_json(SEEN_FILE, {})
        if not isinstance(seen, dict):
            seen = {}
        if len(seen) > 2000:
            seen = dict(list(seen.items())[-2000:])

        count = 0
        while count < MAX_QUEUE_PROCESS:
            item = queue_pop()
            if not item:
                break

            uid = item.get('update_id')
            if uid is not None and str(uid) in seen:
                blog(f"skip seen {uid}")
                continue
            if uid is not None:
                seen[str(uid)] = time.time()
                write_json(SEEN_FILE, seen)

            try:
                update = Update.de_json(item, bot)
                await application.process_update(update)
            except Exception as e:
                blog(f"process_update: {e}")
            count += 1
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fp.close()


# ==========================================================
# ROUTER
# ==========================================================
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from_user = update.effective_user
    chat = update.effective_chat

    if not from_user or not chat:
        return

    if chat.type != ChatType.PRIVATE:
        blog(f"ignored chat type '{chat.type}'")
        return

    if str(from_user.id) == str(ADMIN_ID):
        await admin_handle(update, context)
    else:
        await user_handle(update, context)


# ==========================================================
# USER HANDLER
# ==========================================================
async def user_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or ''
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    first_name = msg.from_user.first_name or 'User'
    username = msg.from_user.username or 'None'

    users = read_json(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    exists = any(u.get('id') == user_id for u in users)
    if not exists:
        users.append({
            'id': user_id,
            'username': username,
            'first_name': first_name,
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })
        write_json(USERS_FILE, users)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🆕 <b>New User</b>\n\n👤 {first_name}\n🔗 @{username}\n🆔 <code>{user_id}</code>",
            parse_mode=ParseMode.HTML,
        )

    if text == '/start':
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👋 Hello {first_name}!\n\n📢 Join @virulvideopompom\nhttps://t.me/+Uj_xq6904lMzYWVl",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )


# ==========================================================
# ADMIN HANDLER
# ==========================================================
async def admin_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ---- Callbacks ----
    if update.callback_query:
        cq = update.callback_query
        data = cq.data or ''
        chat_id = cq.message.chat_id
        await cq.answer()

        if data in ('cron_source_old', 'cron_source_vid65', 'cron_source_both'):
            source = data.replace('cron_source_', '')
            try:
                await cq.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return await run_cron_with_progress(chat_id, source)

        if data.startswith('abrowse_'):
            _, p, i = data.split('_')
            return await admin_browse_render(chat_id, p, i, cq.message.message_id)

        if data.startswith('aget_'):
            _, p, i = data.split('_')
            return await admin_download_by_index(chat_id, p, i)
        return

    # ---- Text ----
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or ''
    chat_id = msg.chat_id
    state = ''
    if os.path.exists(ADMIN_STATE):
        with open(ADMIN_STATE) as f:
            state = f.read().strip()

    tracking = get_tracking()
    mode = tracking.get('mode', 'ON')

    # ❌ Cancel
    if text == '❌ Cancel':
        with open(ADMIN_STATE, 'w') as f:
            f.write('')
        return await context.bot.send_message(
            chat_id=chat_id, text="❌ Cancelled.",
            reply_markup=admin_keyboard(),
        )

    # ⌨️ Hide
    if text == '⌨️ Hide Keyboard':
        return await context.bot.send_message(
            chat_id=chat_id,
            text="⌨️ Hidden. Send /start to bring it back.",
            reply_markup=hidden_keyboard(),
        )

    # /start
    if text == '/start':
        with open(ADMIN_STATE, 'w') as f:
            f.write('')
        return await context.bot.send_message(
            chat_id=chat_id, text="👋 <b>Welcome Admin.</b>",
            parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(),
        )

    # Mode toggle
    if 'Mode: ' in text:
        new = 'OFF' if mode == 'ON' else 'ON'
        tracking['mode'] = new
        save_tracking(tracking)
        return await context.bot.send_message(
            chat_id=chat_id, text=f"✅ Mode → <b>{new}</b>",
            parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(),
        )

    # 📥 Browse
    if text == '📥 Browse Videos':
        with open(ADMIN_STATE, 'w') as f:
            f.write('waiting_page')
        return await context.bot.send_message(
            chat_id=chat_id,
            text="🔢 Send a <b>page number</b>.",
            parse_mode=ParseMode.HTML, reply_markup=cancel_keyboard(),
        )

    if state == 'waiting_page' and text.isdigit():
        with open(ADMIN_STATE, 'w') as f:
            f.write('')
        return await admin_browse_page(chat_id, int(text))

    # 📢 Broadcast
    if text == '📢 Broadcast':
        with open(ADMIN_STATE, 'w') as f:
            f.write('waiting_broadcast')
        return await context.bot.send_message(
            chat_id=chat_id, text="📝 Send the message to broadcast.",
            reply_markup=cancel_keyboard(),
        )

    if state == 'waiting_broadcast':
        with open(ADMIN_STATE, 'w') as f:
            f.write('')
        return await admin_broadcast(chat_id, msg.message_id)

    # 📊 Status
    if text == '📊 Status':
        return await admin_status(chat_id, mode, tracking)

    # ▶️ Run Cron
    if text == '▶️ Run Cron':
        return await run_cron_with_progress(chat_id)

    # fallback
    await context.bot.send_message(
        chat_id=chat_id, text="👋 <b>Welcome Admin.</b>",
        parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(),
    )


# ==========================================================
# BROWSE
# ==========================================================
async def admin_browse_page(chat_id, p):
    data = fetch_api_data(f"{API_BASE}?action=home&page={p}")
    if not data or not data.get('data'):
        return await bot.send_message(
            chat_id=chat_id, text=f"❌ No videos on page {p}.",
            reply_markup=admin_keyboard(),
        )

    vids = data['data']
    total = len(vids)
    v = vids[0]
    caption = f"🎬 <b>{v.get('title', '')}</b>\n\n📄 Page: {p} | Item: 1/{total}\n🆔 <code>{v.get('slug', '')}</code>"

    row = [InlineKeyboardButton('⬇️ Download', callback_data=f"aget_{p}_0")]
    if total > 1:
        row.append(InlineKeyboardButton('Next ➡️', callback_data=f"abrowse_{p}_1"))

    await bot.send_photo(
        chat_id=chat_id, photo=v.get('thumbnail', ''),
        caption=caption, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([row]),
    )
    await bot.send_message(
        chat_id=chat_id, text="Use buttons below or ❌ Cancel above.",
        reply_markup=admin_keyboard(),
    )


async def admin_browse_render(chat_id, p, i, del_msg_id):
    api = fetch_api_data(f"{API_BASE}?action=home&page={int(p)}")
    if not api or not api.get('data'):
        return await bot.send_message(chat_id=chat_id, text=f"❌ No videos on page {p}.")

    vids = api['data']
    total = len(vids)
    idx = int(i)
    if idx >= len(vids):
        idx = 0
    v = vids[idx]

    caption = f"🎬 <b>{v.get('title', '')}</b>\n\n📄 Page: {p} | Item: {idx + 1}/{total}\n🆔 <code>{v.get('slug', '')}</code>"

    row = []
    if idx > 0:
        row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f"abrowse_{p}_{idx - 1}"))
    row.append(InlineKeyboardButton('⬇️ Download', callback_data=f"aget_{p}_{idx}"))
    if idx < total - 1:
        row.append(InlineKeyboardButton('Next ➡️', callback_data=f"abrowse_{p}_{idx + 1}"))

    try:
        await bot.delete_message(chat_id=chat_id, message_id=del_msg_id)
    except Exception:
        pass

    await bot.send_photo(
        chat_id=chat_id, photo=v.get('thumbnail', ''),
        caption=caption, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([row]),
    )


# ==========================================================
# BROADCAST / STATUS
# ==========================================================
async def admin_broadcast(chat_id, msg_id):
    users = read_json(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    await bot.send_message(chat_id=chat_id, text=f"🚀 Broadcasting to {len(users)} users...")
    ok = 0
    for u in users:
        try:
            await bot.copy_message(chat_id=u['id'], from_chat_id=ADMIN_ID, message_id=msg_id)
            ok += 1
        except Exception as e:
            blog(f"broadcast to {u.get('id')}: {e}")
        await asyncio.sleep(0.05)

    await bot.send_message(
        chat_id=chat_id, text=f"✅ Broadcast done. Sent to {ok} users.",
        reply_markup=admin_keyboard(),
    )


async def admin_status(chat_id, mode, tracking):
    users = read_json(USERS_FILE, [])
    users_count = len(users) if isinstance(users, list) else 0
    queue_count = 0
    if os.path.exists(QUEUE_FILE):
        q = read_json(QUEUE_FILE, [])
        queue_count = len(q) if isinstance(q, list) else 0

    t = ("📊 <b>Bot Status</b>\n\n"
         f"⚙️ <b>Mode:</b> {mode}\n"
         f"👥 <b>Users:</b> {users_count}\n"
         f"📄 <b>Old API Page:</b> {tracking.get('current_page', 203)}\n"
         f"✅ <b>Old API Processed:</b> {len(tracking.get('processed_slugs', []))}\n"
         f"🔄 <b>Old API In Progress:</b> {len(tracking.get('in_progress_slugs', []))}\n"
         f"\n📡 <b>VID65 Page:</b> {tracking.get('vid65_page', 1)}\n"
         f"✅ <b>VID65 Processed:</b> {len(tracking.get('vid65_processed_ids', []))}\n"
         f"🔄 <b>VID65 In Progress:</b> {len(tracking.get('vid65_in_progress_ids', []))}\n"
         f"📥 <b>Queue:</b> {queue_count}\n"
         f"🆔 <b>Admin:</b> <code>{ADMIN_ID}</code>")

    await bot.send_message(
        chat_id=chat_id, text=t,
        parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(),
    )


# ==========================================================
# CRON
# ==========================================================
async def run_cron_with_progress(admin_chat_id, source=None):
    if source is None:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton('1️⃣ Old API', callback_data='cron_source_old'),
                InlineKeyboardButton('2️⃣ VID65 API', callback_data='cron_source_vid65'),
            ],
            [InlineKeyboardButton('▶️ Run Both', callback_data='cron_source_both')],
        ])
        return await bot.send_message(
            chat_id=admin_chat_id,
            text="📡 <b>Select API source</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    lock_path = os.path.join(BASE_DIR, 'cron.lock')
    lock_fp = open(lock_path, 'a+')
    try:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return await bot.send_message(
                chat_id=admin_chat_id, text="⚠️ A cron is already running."
            )

        try:
            sources = ['old', 'vid65'] if source == 'both' else [source]
            for src in sources:
                await cron_pipeline(admin_chat_id, GROUPS, CHANNELS, src)
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
    finally:
        lock_fp.close()


def cron_source_info(source):
    if source == 'vid65':
        return {
            'label': 'VID65 API',
            'api': API_VID65,
            'pageKey': 'vid65_page',
            'processedKey': 'vid65_processed_ids',
            'inProgressKey': 'vid65_in_progress_ids',
        }
    return {
        'label': 'Old API',
        'api': API_BASE,
        'pageKey': 'current_page',
        'processedKey': 'processed_slugs',
        'inProgressKey': 'in_progress_slugs',
    }


async def cron_pipeline(admin_chat_id, groups, channels, source='old'):
    info = cron_source_info(source)
    tracking = get_tracking()
    page = int(tracking.get(info['pageKey'], 1 if source == 'vid65' else 203))
    mode = tracking.get('mode', 'ON')
    processed = tracking.get(info['processedKey'], [])
    in_progress = tracking.get(info['inProgressKey'], [])

    if source == 'vid65':
        home_url = f"{info['api']}?page={max(1, page)}"
    else:
        home_url = f"{info['api']}?action=home&page={max(1, page)}"

    home = fetch_api_data(home_url)
    if not home or not home.get('data'):
        return await bot.send_message(
            chat_id=admin_chat_id,
            text=f"❌ <b>{info['label']}</b>: no videos on page {page}.",
            parse_mode=ParseMode.HTML,
        )

    videos = home['data']
    total = len(videos)

    v = None
    current_idx = -1
    item_key = ''

    if source == 'vid65':
        for i in range(total):
            vid_id = videos[i].get('id')
            item_key = 'id:' + str(vid_id) if vid_id is not None else 'id:'
            if vid_id is None or vid_id == '':
                continue
            if item_key in processed or item_key in in_progress:
                continue
            v = videos[i]
            current_idx = i
            break
    else:
        for i in range(total - 1, -1, -1):
            s = videos[i].get('slug', '')
            if not s:
                continue
            if s in processed or s in in_progress:
                continue
            v = videos[i]
            current_idx = i
            item_key = s
            break

    if v is None:
        if source == 'vid65':
            last_page = int(home.get('pagination', {}).get('total_pages', page))
            if page < last_page:
                tracking[info['pageKey']] = page + 1
                save_tracking(tracking)
                return await bot.send_message(
                    chat_id=admin_chat_id,
                    text=f"✅ <b>VID65 page {page} done</b> → next page: {page + 1}",
                    parse_mode=ParseMode.HTML,
                )
            return await bot.send_message(
                chat_id=admin_chat_id,
                text="✅ <b>VID65 API finished.</b> All available pages are processed.",
                parse_mode=ParseMode.HTML,
            )
        else:
            tracking[info['pageKey']] = max(1, page - 1)
            save_tracking(tracking)
            return await bot.send_message(
                chat_id=admin_chat_id,
                text=f"✅ <b>Old API page {page} done</b> → next: {tracking[info['pageKey']]}",
                parse_mode=ParseMode.HTML,
            )

    if source == 'vid65':
        vid_id = str(v.get('id', ''))
        title = html_escape(v.get('name', 'Untitled'))
        slug = f"vid65-{vid_id}"
        thumb_url = v.get('image', '')
        link = v.get('video', '')
    else:
        title = html_escape(v.get('title', 'Untitled'))
        slug = v.get('slug', '')
        thumb_url = v.get('thumbnail', '')
        link = ''

    item_num = current_idx + 1
    mode_label = 'Video' if mode == 'ON' else 'Image'

    tracking.setdefault(info['inProgressKey'], []).append(item_key)
    save_tracking(tracking)

    # ---------- thumbnail ----------
    thumb_path = None
    if thumb_url:
        try:
            raw = requests.get(thumb_url, timeout=30).content
        except Exception:
            raw = None
        if raw and len(raw) > 100:
            t = os.path.join(BASE_DIR, f"thumb_init_{int(time.time())}_{random.randint(1000, 9999)}.jpg")
            with open(t, 'wb') as f:
                f.write(raw)
            try:
                Image.open(t).verify()
                thumb_path = t
            except Exception:
                try:
                    os.remove(t)
                except Exception:
                    pass

    init_caption = (
        f"⏳ <b>Cron started...</b>\n\n"
        f"📡 <b>Source:</b> {info['label']}\n"
        f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n"
        f"📄 Page: {page} | Item: {item_num}/{total}\n"
        f"⚙️ Mode: {mode_label}"
    )

    if thumb_path:
        init = await bot.send_photo(
            chat_id=admin_chat_id, photo=InputFile(thumb_path),
            caption=init_caption, parse_mode=ParseMode.HTML,
        )
        use_photo = True
    else:
        init = await bot.send_message(
            chat_id=admin_chat_id, text=init_caption, parse_mode=ParseMode.HTML,
        )
        use_photo = False

    msg_id = init.message_id

    # ---------- old API second request ----------
    if source == 'old':
        vd = fetch_api_data(f"{API_BASE}?action=video&id={requests.utils.quote(slug)}")
        link = (vd.get('data', {}).get('downloadLink', '') if vd else '') or ''

    if not link:
        await progress_edit(
            admin_chat_id, msg_id, use_photo,
            f"❌ <b>No download link</b>\n\n📡 {info['label']}\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
        )
        tracking[info['processedKey']].append(item_key)
        tracking[info['inProgressKey']] = [x for x in tracking[info['inProgressKey']] if x != item_key]
        save_tracking(tracking)
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        return

    # ---------- download ----------
    tmp = os.path.join(BASE_DIR, f"cron_{int(time.time())}_{random.randint(1000, 9999)}.mp4")
    last_pct = -1
    last_ts = 0

    try:
        r = requests.get(link, stream=True, timeout=(15, 1800))
        total_size = int(r.headers.get('content-length', 0))
        downloaded = 0
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = int((downloaded / total_size) * 100)
                    now = time.time()
                    if pct != last_pct and (now - last_ts) >= 2 and pct < 100:
                        last_pct = pct
                        last_ts = now
                        mb_now = round(downloaded / 1048576, 1)
                        mb_tot = round(total_size / 1048576, 1)
                        await progress_edit(
                            admin_chat_id, msg_id, use_photo,
                            f"📥 <b>Downloading...</b> {pct}%\n\n"
                            f"📡 {info['label']}\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n"
                            f"📄 Page: {page} | Item: {item_num}/{total}\n"
                            f"📦 {mb_now} MB / {mb_tot} MB",
                        )
    except Exception as e:
        blog(f"download error: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        await progress_edit(
            admin_chat_id, msg_id, use_photo,
            f"❌ <b>Download failed</b>\n\n📡 {info['label']}\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
        )
        tracking[info['inProgressKey']] = [x for x in tracking[info['inProgressKey']] if x != item_key]
        save_tracking(tracking)
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        return

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except Exception:
            pass
        await progress_edit(
            admin_chat_id, msg_id, use_photo,
            f"❌ <b>Download failed</b>\n\n📡 {info['label']}\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
        )
        tracking[info['inProgressKey']] = [x for x in tracking[info['inProgressKey']] if x != item_key]
        save_tracking(tracking)
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        return

    # ---------- blur ----------
    blur_thumb = os.path.join(BASE_DIR, f"thumb_blur_{int(time.time())}_{random.randint(1000, 9999)}.jpg")
    has_thumb = False
    if thumb_path:
        has_thumb = True
        if not apply_blur(thumb_path, blur_thumb, BLUR_PERCENTAGE):
            import shutil
            shutil.copy(thumb_path, blur_thumb)

    # ---------- upload ----------
    channel_caption = "Join @virulvideopompom 🎬\n\nhttps://t.me/+Uj_xq6904lMzYWVl"
    group_caption = f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom"
    sent_as_video = False

    if mode == 'ON':
        for g in groups:
            await video_upload(g, tmp, thumb_path, has_thumb, group_caption)
            await asyncio.sleep(1)
        for c in channels:
            res = await video_upload(c, tmp, blur_thumb, has_thumb, channel_caption)
            if res:
                await manage_channel_history(c, res.message_id)
                sent_as_video = True

    if not sent_as_video:
        await progress_edit(
            admin_chat_id, msg_id, use_photo,
            f"⚠️ <b>Falling back to photo/link...</b>\n\n📡 {info['label']}\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
        )

        for g in groups:
            if mode == 'OFF' and has_thumb:
                await bot.send_photo(
                    chat_id=g, photo=InputFile(thumb_path),
                    caption=group_caption, parse_mode=ParseMode.HTML,
                    protect_content=True,
                )
            else:
                await bot.send_message(
                    chat_id=g,
                    text=f"🎬 <b>{title}</b>\n\n📥 {link}\n\n🔥 @virulvideopompom",
                    parse_mode=ParseMode.HTML, protect_content=True,
                )
            await asyncio.sleep(1)

        if has_thumb:
            for c in channels:
                res = await bot.send_photo(
                    chat_id=c, photo=InputFile(blur_thumb),
                    caption=channel_caption, parse_mode=ParseMode.HTML,
                )
                await manage_channel_history(c, res.message_id)

    # ---------- cleanup ----------
    try:
        os.remove(tmp)
    except Exception:
        pass
    if thumb_path and os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except Exception:
            pass
    if os.path.exists(blur_thumb):
        try:
            os.remove(blur_thumb)
        except Exception:
            pass

    # ---------- finalize ----------
    tracking = get_tracking()
    tracking.setdefault(info['processedKey'], []).append(item_key)
    if len(tracking[info['processedKey']]) > 3000:
        tracking[info['processedKey']].pop(0)
    tracking[info['inProgressKey']] = [x for x in tracking[info['inProgressKey']] if x != item_key]
    save_tracking(tracking)

    await progress_edit(
        admin_chat_id, msg_id, use_photo,
        f"✅ <b>Cron finished!</b>\n\n"
        f"📡 <b>{info['label']}</b>\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>\n"
        f"📄 <b>Page:</b> {page} | Item: {item_num}/{total}\n"
        f"📤 <b>Sent as video:</b> {'Yes' if sent_as_video else 'No'}",
    )


async def video_upload(target, tmp, thumb, has_thumb, caption):
    try:
        if has_thumb and thumb:
            return await bot.send_video(
                chat_id=target,
                video=InputFile(tmp),
                thumbnail=InputFile(thumb),
                caption=caption,
                parse_mode=ParseMode.HTML,
                protect_content=True,
                supports_streaming=True,
            )
        return await bot.send_video(
            chat_id=target,
            video=InputFile(tmp),
            caption=caption,
            parse_mode=ParseMode.HTML,
            protect_content=True,
            supports_streaming=True,
        )
    except Exception as e:
        blog(f"video_upload to {target}: {e}")
        return None


# ==========================================================
# DOWNLOAD BY INDEX
# ==========================================================
async def admin_download_by_index(chat_id, page, index):
    data = fetch_api_data(f"{API_BASE}?action=home&page={int(page)}")
    if not data or not data.get('data') or int(index) >= len(data['data']):
        return await bot.send_message(chat_id=chat_id, text="❌ Video not found.")

    v = data['data'][int(index)]
    slug = v.get('slug', '')
    title = html_escape(v.get('title', 'Untitled'))

    vd = fetch_api_data(f"{API_BASE}?action=video&id={requests.utils.quote(slug)}")
    link = (vd.get('data', {}).get('downloadLink', '') if vd else '') or ''

    if not link:
        return await bot.send_message(
            chat_id=chat_id,
            text=f"❌ No download link.\n\n🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
            parse_mode=ParseMode.HTML,
        )

    tmp = os.path.join(BASE_DIR, f"browse_{int(time.time())}.mp4")
    try:
        r = requests.get(link, stream=True, timeout=(15, 1800))
        with open(tmp, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return await bot.send_message(chat_id=chat_id, text="❌ Download failed.")

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return await bot.send_message(chat_id=chat_id, text="❌ Download failed.")

    thumb = None
    thumb_url = v.get('thumbnail', '')
    if thumb_url:
        try:
            raw = requests.get(thumb_url, timeout=30).content
        except Exception:
            raw = None
        if raw and len(raw) > 100:
            t = os.path.join(BASE_DIR, f"browse_thumb_{int(time.time())}.jpg")
            with open(t, 'wb') as f:
                f.write(raw)
            try:
                Image.open(t).verify()
                thumb = t
            except Exception:
                try:
                    os.remove(t)
                except Exception:
                    pass

    if thumb:
        await bot.send_video(
            chat_id=chat_id,
            video=InputFile(tmp),
            thumbnail=InputFile(thumb),
            caption=f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
        )
    else:
        await bot.send_video(
            chat_id=chat_id,
            video=InputFile(tmp),
            caption=f"🎬 <b>{title}</b>\n🆔 <code>{slug}</code>",
            parse_mode=ParseMode.HTML,
            supports_streaming=True,
        )

    try:
        os.remove(tmp)
    except Exception:
        pass
    if thumb:
        try:
            os.remove(thumb)
        except Exception:
            pass


# ==========================================================
# PROGRESS EDIT
# ==========================================================
async def progress_edit(chat_id, message_id, is_photo, caption):
    try:
        if is_photo:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=caption, parse_mode=ParseMode.HTML,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=caption, parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        blog(f"progress_edit: {e}")


# ==========================================================
# PAYMENT HANDLERS
# ==========================================================
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Premium Access",
        description="Unlock premium features",
        payload="premium-access",
        provider_token=os.environ.get('PAYMENT_PROVIDER_TOKEN', ''),
        currency="USD",
        prices=[LabeledPrice(label="Premium", amount=499)],
        start_parameter="premium",
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    if q.invoice_payload != "premium-access":
        await q.answer(ok=False, error_message="Something went wrong...")
    else:
        await q.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Thank you for your payment! 🎉")


# ==========================================================
# FASTAPI + LIFESPAN
# ==========================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global application, bot

    # --- Build Application with custom server, fall back if needed ---
    application = None
    for server in (TG_API_SERVER, TG_API_SERVER_FALLBACK):
        try:
            app_builder = (
                ApplicationBuilder()
                .token(BOT_TOKEN)
                .base_url(server)
                .connect_timeout(20)
                .read_timeout(1800)
                .write_timeout(1800)
            )
            candidate = app_builder.build()
            # Verify connectivity
            me = await candidate.bot.get_me()
            if me:
                logger.info(f"Using Telegram API server: {server}")
                application = candidate
                break
        except Exception as e:
            logger.warning(f"Server {server} failed: {e}")
            continue

    if application is None:
        raise RuntimeError("All Telegram API servers failed")

    bot = application.bot

    # --- Register handlers (order matters: specific first) ---
    application.add_handler(CommandHandler('pay', pay_command))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    application.add_handler(MessageHandler(filters.ALL, router))
    application.add_handler(CallbackQueryHandler(router))

    await application.initialize()

    # --- Set webhook ---
    try:
        await bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
        logger.info(f"Webhook set: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

    # --- Process any queued updates from before restart ---
    asyncio.create_task(process_queue_worker())

    yield

    # Shutdown
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    """Receive Telegram update → queue → respond 200 → background process."""
    try:
        data = await request.json()
        queue_push(data)
        asyncio.create_task(process_queue_worker())
        return Response(content='{"ok":true}', media_type='application/json')
    except Exception as e:
        blog(f"webhook error: {e}")
        raise HTTPException(status_code=400, detail="Bad Request")


@app.get("/run_cron")
async def run_cron(secret: str = ''):
    if secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    asyncio.create_task(run_cron_with_progress(ADMIN_ID))
    return Response(content='{"ok":true}', media_type='application/json')


@app.get("/health")
async def health():
    return {"status": "ok"}


# ==========================================================
# ENTRY POINT
# ==========================================================
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=PORT)
