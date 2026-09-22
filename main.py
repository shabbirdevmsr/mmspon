#!/usr/bin/env python3
"""
Single-file Python conversion of the uploaded Telegram bot.

Preserved logical modules:
- router / queue / Telegram HTTP infrastructure
- user handling
- admin keyboard, browse, broadcast, status
- cron/video processing
- JSON/text persistence
- external cron trigger

Environment variables:
  BOT_TOKEN       required
  ADMIN_ID        optional, defaults to the value from the original source
  CRON_SECRET     required for /?run_cron-style HTTP trigger if using the built-in HTTP server

Dependencies:
  pip install requests flask pillow
  Optional: ImageMagick/Pillow is used for thumbnail blur.

Run:
  python bot.py
"""

import os
import sys
import json
import time
import html
import fcntl
import threading
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from PIL import Image, ImageFilter
except ImportError:
    Image = None
    ImageFilter = None

try:
    from flask import Flask, request, jsonify
except ImportError:
    Flask = None


# ==========================================================
# CONFIG
# ==========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "5087403859").strip()
CRON_SECRET = os.getenv("CRON_SECRET", "CHANGE_ME_random_long_string").strip()

BASE_DIR = Path(__file__).resolve().parent

USERS_FILE = BASE_DIR / "users.json"
DATA_FILE = BASE_DIR / "sent_videos.json"
CHANNEL_POSTS_FILE = BASE_DIR / "channel_posts.json"
LOG_FILE = BASE_DIR / "bot_log.txt"
ADMIN_STATE = BASE_DIR / "admin_state.txt"
QUEUE_FILE = BASE_DIR / "queue.json"
PROCESSOR_LOCK = BASE_DIR / "queue.lock"
SEEN_FILE = BASE_DIR / "seen_updates.json"
CRON_LOCK = BASE_DIR / "cron.lock"

API_BASE = "https://shabbir.serv00.net/sex/mmsbaba/get.php"
API_VID65 = "https://shabbir.serv00.net/sex/vid65/get.php"

TG_API_SERVER = "https://telegram-bot-api-production-29e4.up.railway.app"
TG_API_SERVER_FALLBACK = "https://api.telegram.org"

MAX_SIZE = 2000 * 1024 * 1024
BLUR_PERCENTAGE = 20
MAX_QUEUE_PROCESS = 100

GROUPS = ["-1003708527420"]
CHANNELS = ["@virulvideopompom"]

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required.")


# ==========================================================
# SHARED HELPERS
# ==========================================================

def blog(message: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def json_load(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except Exception as e:
        blog(f"JSON load failed {path.name}: {e}")
        return default


def json_save(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return default


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


# ==========================================================
# FILE LOCK
# ==========================================================

class FileLock:
    def __init__(self, path: Path, blocking: bool = False):
        self.path = path
        self.blocking = blocking
        self.fp = None

    def acquire(self) -> bool:
        self.fp = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.fp.fileno(), flags)
            return True
        except BlockingIOError:
            self.fp.close()
            self.fp = None
            return False

    def release(self) -> None:
        if self.fp:
            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.fp.close()
            except Exception:
                pass
            self.fp = None


# ==========================================================
# QUEUE
# ==========================================================

def queue_push(update: dict) -> bool:
    lock = FileLock(QUEUE_FILE, blocking=True)
    # Keep queue data in the same file, with a separate process-safe lock.
    try:
        lock.acquire()
        q = json_load(QUEUE_FILE, [])
        if not isinstance(q, list):
            q = []

        uid = update.get("update_id")
        if uid is not None:
            for item in q:
                if item.get("update_id") == uid:
                    return True

        q.append(update)
        if len(q) > 500:
            q = q[-500:]
        json_save(QUEUE_FILE, q)
        return True
    except Exception as e:
        blog(f"queue_push: {e}")
        return False
    finally:
        lock.release()


def queue_pop() -> Optional[dict]:
    lock = FileLock(QUEUE_FILE, blocking=True)
    try:
        lock.acquire()
        q = json_load(QUEUE_FILE, [])
        if not isinstance(q, list) or not q:
            return None
        item = q.pop(0)
        json_save(QUEUE_FILE, q)
        return item
    except Exception as e:
        blog(f"queue_pop: {e}")
        return None
    finally:
        lock.release()


# ==========================================================
# TELEGRAM HTTP
# ==========================================================

_session = requests.Session()


def tg_call(
    method: str,
    data: Optional[dict] = None,
    file_field: Optional[str] = None,
    file_path: Optional[Path] = None,
    thumb_path: Optional[Path] = None,
    progress_fn=None,
) -> dict:
    data = dict(data or {})
    servers = [TG_API_SERVER, TG_API_SERVER_FALLBACK]

    for i, server in enumerate(servers):
        url = server.rstrip("/") + f"/bot{BOT_TOKEN}/{method}"

        files = {}
        opened = []

        try:
            if file_path and file_field and file_path.exists():
                fp = file_path.open("rb")
                opened.append(fp)
                files[file_field] = fp

            if thumb_path and thumb_path.exists():
                tp = thumb_path.open("rb")
                opened.append(tp)
                files["thumbnail"] = tp

            # requests does not expose PHP cURL's exact progress callback.
            # The video download/upload functions retain the admin progress flow
            # by updating status around the transfer.
            response = _session.post(
                url,
                data=data,
                files=files or None,
                timeout=1800,
                verify=True,
            )
            response_text = response.text

        except requests.RequestException as e:
            blog(f"HTTP {method} #{i}: {e}")
            for fp in opened:
                try:
                    fp.close()
                except Exception:
                    pass
            continue
        finally:
            for fp in opened:
                try:
                    fp.close()
                except Exception:
                    pass

        try:
            result = response.json()
        except Exception:
            blog(f"Bad JSON {method} #{i}")
            continue

        if not result.get("ok") and result.get("error_code") == 429:
            wait = int(result.get("parameters", {}).get("retry_after", 5))
            time.sleep(wait + 1)
            try:
                response = _session.post(
                    url,
                    data=data,
                    files=None,
                    timeout=1800,
                    verify=True,
                )
                result = response.json()
            except Exception as e:
                blog(f"Retry {method}: {e}")

        if not result.get("ok"):
            blog(f"TG {method} #{i}: {result.get('description', '?')}")
            if i == 0:
                continue

        return result

    return {"ok": False, "description": "all servers failed"}


def send_to_telegram(method: str, data: dict) -> dict:
    return tg_call(method, data)


def send_to_telegram_file(
    method: str,
    data: dict,
    file_field: Optional[str] = None,
    file_path: Optional[Path] = None,
    thumb_path: Optional[Path] = None,
    progress_fn=None,
) -> dict:
    return tg_call(method, data, file_field, file_path, thumb_path, progress_fn)


def fetch_api_data(url: str) -> Optional[dict]:
    try:
        r = _session.get(url, timeout=60, verify=True)
        return r.json()
    except Exception as e:
        blog(f"API request failed: {url}: {e}")
        return None


def progress_edit(chat_id: str, message_id: int, is_photo: bool, caption: str) -> dict:
    method = "editMessageCaption" if is_photo else "editMessageText"
    key = "caption" if is_photo else "text"
    return send_to_telegram(method, {
        "chat_id": chat_id,
        "message_id": message_id,
        key: caption,
        "parse_mode": "HTML",
    })


# ==========================================================
# TRACKING
# ==========================================================

def get_tracking() -> dict:
    t = json_load(DATA_FILE, {})
    if not isinstance(t, dict):
        t = {}

    t.setdefault("current_page", 203)
    t.setdefault("processed_slugs", [])
    t.setdefault("in_progress_slugs", [])
    t.setdefault("vid65_page", 1)
    t.setdefault("vid65_processed_ids", [])
    t.setdefault("vid65_in_progress_ids", [])
    t.setdefault("mode", "ON")
    return t


def save_tracking(t: dict) -> None:
    json_save(DATA_FILE, t)


# ==========================================================
# BLUR / CHANNEL HISTORY
# ==========================================================

def apply_blur(src: Path, dst: Path, pct: int) -> bool:
    if Image is None:
        return False

    try:
        img = Image.open(src).convert("RGB")
        radius = max(1, round(pct / 3.33))
        img = img.filter(ImageFilter.GaussianBlur(radius=radius))
        img.save(dst, "JPEG", quality=90)
        img.close()
        return True
    except Exception as e:
        blog(f"Pillow blur: {e}")
        return False


def manage_channel_history(ch: str, mid: int) -> None:
    posts = json_load(CHANNEL_POSTS_FILE, {})
    if not isinstance(posts, dict):
        posts = {}

    posts.setdefault(ch, [])
    posts[ch].append(mid)

    if len(posts[ch]) > 10:
        old = posts[ch].pop(0)
        send_to_telegram("deleteMessage", {
            "chat_id": ch,
            "message_id": old,
        })

    json_save(CHANNEL_POSTS_FILE, posts)


# ==========================================================
# USER
# ==========================================================

def user_handle(update: dict) -> None:
    if "callback_query" in update:
        send_to_telegram("answerCallbackQuery", {
            "callback_query_id": update["callback_query"]["id"]
        })
        return

    msg = update.get("message")
    if not msg:
        return

    text = msg.get("text", "")
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    first_name = msg["from"].get("first_name", "User")
    username = msg["from"].get("username", "None")

    users = json_load(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    exists = any(str(u.get("id")) == str(user_id) for u in users)

    if not exists:
        users.append({
            "id": user_id,
            "username": username,
            "first_name": first_name,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        json_save(USERS_FILE, users)

        send_to_telegram("sendMessage", {
            "chat_id": ADMIN_ID,
            "text": (
                f"🆕 <b>New User</b>\n\n"
                f"👤 {html.escape(first_name)}\n"
                f"🔗 @{html.escape(username)}\n"
                f"🆔 <code>{user_id}</code>"
            ),
            "parse_mode": "HTML",
        })

    if text == "/start":
        send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"👋 Hello {html.escape(first_name)}!\n\n"
                f"📢 Join @virulvideopompom\n"
                f"https://t.me/+Uj_xq6904lMzYWVl"
            ),
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"remove_keyboard": True}),
        })


# ==========================================================
# ADMIN KEYBOARDS
# ==========================================================

def admin_keyboard() -> str:
    t = get_tracking()
    mode_txt = "🟢 Mode: ON" if t.get("mode", "ON") == "ON" else "🔴 Mode: OFF"

    return json.dumps({
        "keyboard": [
            [{"text": "📢 Broadcast"}, {"text": "📊 Status"}],
            [{"text": "▶️ Run Cron"}, {"text": mode_txt}],
            [{"text": "📥 Browse Videos"}],
            [{"text": "⌨️ Hide Keyboard"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }, ensure_ascii=False)


def cancel_keyboard() -> str:
    return json.dumps({
        "keyboard": [[{"text": "❌ Cancel"}]],
        "resize_keyboard": True,
        "is_persistent": False,
    }, ensure_ascii=False)


def hidden_keyboard() -> str:
    return json.dumps({"remove_keyboard": True})


# ==========================================================
# ADMIN
# ==========================================================

def admin_handle(update: dict) -> None:
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]

        send_to_telegram("answerCallbackQuery", {
            "callback_query_id": cq["id"]
        })

        if data in ("cron_source_old", "cron_source_vid65", "cron_source_both"):
            source = data.replace("cron_source_", "", 1)

            send_to_telegram("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": cq["message"]["message_id"],
                "reply_markup": json.dumps({"inline_keyboard": []}),
            })
            return run_cron_with_progress(chat_id, source)

        if data.startswith("abrowse_"):
            return admin_browse_render(
                chat_id, data, cq["message"]["message_id"]
            )

        if data.startswith("aget_"):
            parts = data.split("_")
            if len(parts) >= 3:
                return admin_download_by_index(
                    chat_id, int(parts[1]), int(parts[2])
                )
            return

        return

    msg = update.get("message")
    if not msg:
        return

    text = msg.get("text", "")
    chat_id = msg["chat"]["id"]

    state = read_text(ADMIN_STATE, "")
    tracking = get_tracking()
    mode = tracking.get("mode", "ON")

    if text == "❌ Cancel":
        write_text(ADMIN_STATE, "")
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Cancelled.",
            "reply_markup": admin_keyboard(),
        })

    if text == "⌨️ Hide Keyboard":
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "⌨️ Hidden. Send /start to bring it back.",
            "reply_markup": hidden_keyboard(),
        })

    if text == "/start":
        write_text(ADMIN_STATE, "")
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "👋 <b>Welcome Admin.</b>",
            "parse_mode": "HTML",
            "reply_markup": admin_keyboard(),
        })

    if "Mode: " in text:
        new = "OFF" if mode == "ON" else "ON"
        tracking["mode"] = new
        save_tracking(tracking)
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"✅ Mode → <b>{new}</b>",
            "parse_mode": "HTML",
            "reply_markup": admin_keyboard(),
        })

    if text == "📥 Browse Videos":
        write_text(ADMIN_STATE, "waiting_page")
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "🔢 Send a <b>page number</b>.",
            "parse_mode": "HTML",
            "reply_markup": cancel_keyboard(),
        })

    if state == "waiting_page" and text.isnumeric():
        write_text(ADMIN_STATE, "")
        return admin_browse_page(chat_id, int(text))

    if text == "📢 Broadcast":
        write_text(ADMIN_STATE, "waiting_broadcast")
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "📝 Send the message to broadcast.",
            "reply_markup": cancel_keyboard(),
        })

    if state == "waiting_broadcast":
        write_text(ADMIN_STATE, "")
        return admin_broadcast(chat_id, msg["message_id"])

    if text == "📊 Status":
        return admin_status(chat_id, mode, tracking)

    if text == "▶️ Run Cron":
        return run_cron_with_progress(chat_id)

    send_to_telegram("sendMessage", {
        "chat_id": chat_id,
        "text": "👋 <b>Welcome Admin.</b>",
        "parse_mode": "HTML",
        "reply_markup": admin_keyboard(),
    })


def admin_browse_page(chat_id: str, page: int) -> None:
    data = fetch_api_data(f"{API_BASE}?action=home&page={int(page)}")
    if not data or not data.get("data"):
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ No videos on page {page}.",
            "reply_markup": admin_keyboard(),
        })

    vids = data["data"]
    total = len(vids)
    v = vids[0]

    title = html.escape(str(v.get("title", "Untitled")))
    slug = html.escape(str(v.get("slug", "")))

    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"📄 Page: {page} | Item: 1/{total}\n"
        f"🆔 <code>{slug}</code>"
    )

    row = [{
        "text": "⬇️ Download",
        "callback_data": f"aget_{page}_0"
    }]
    if total > 1:
        row.append({
            "text": "Next ➡️",
            "callback_data": f"abrowse_{page}_1"
        })

    send_to_telegram("sendPhoto", {
        "chat_id": chat_id,
        "photo": v.get("thumbnail", ""),
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [row]}),
    })

    send_to_telegram("sendMessage", {
        "chat_id": chat_id,
        "text": "Use buttons below or ❌ Cancel above.",
        "reply_markup": admin_keyboard(),
    })


def admin_browse_render(chat_id: str, data: str, delete_message_id: int) -> None:
    parts = data.split("_")
    if len(parts) < 3:
        return

    page = int(parts[1])
    idx = int(parts[2])

    api = fetch_api_data(f"{API_BASE}?action=home&page={page}")
    if not api or not api.get("data"):
        return send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ No videos on page {page}."
        })

    vids = api["data"]
    total = len(vids)

    if idx < 0 or idx >= total:
        idx = 0

    v = vids[idx]
    title = html.escape(str(v.get("title", "Untitled")))
    slug = html.escape(str(v.get("slug", "")))

    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"📄 Page: {page} | Item: {idx + 1}/{total}\n"
        f"🆔 <code>{slug}</code>"
    )

    row = []
    if idx > 0:
        row.append({
            "text": "⬅️ Prev",
            "callback_data": f"abrowse_{page}_{idx - 1}"
        })

    row.append({
        "text": "⬇️ Download",
        "callback_data": f"aget_{page}_{idx}"
    })

    if idx < total - 1:
        row.append({
            "text": "Next ➡️",
            "callback_data": f"abrowse_{page}_{idx + 1}"
        })

    send_to_telegram("deleteMessage", {
        "chat_id": chat_id,
        "message_id": delete_message_id,
    })

    send_to_telegram("sendPhoto", {
        "chat_id": chat_id,
        "photo": v.get("thumbnail", ""),
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [row]}),
    })


def admin_broadcast(chat_id: str, message_id: int) -> None:
    users = json_load(USERS_FILE, [])
    if not isinstance(users, list):
        users = []

    send_to_telegram("sendMessage", {
        "chat_id": chat_id,
        "text": f"🚀 Broadcasting to {len(users)} users..."
    })

    ok = 0
    for u in users:
        r = send_to_telegram("copyMessage", {
            "chat_id": u["id"],
            "from_chat_id": ADMIN_ID,
            "message_id": message_id,
        })
        if r.get("ok"):
            ok += 1
        time.sleep(0.05)

    send_to_telegram("sendMessage", {
        "chat_id": chat_id,
        "text": f"✅ Broadcast done. Sent to {ok} users.",
        "reply_markup": admin_keyboard(),
    })


def admin_status(chat_id: str, mode: str, tracking: dict) -> None:
    users = json_load(USERS_FILE, [])
    users_count = len(users) if isinstance(users, list) else 0

    q = json_load(QUEUE_FILE, [])
    queue_count = len(q) if isinstance(q, list) else 0

    text = (
        "📊 <b>Bot Status</b>\n\n"
        f"⚙️ <b>Mode:</b> {html.escape(mode)}\n"
        f"👥 <b>Users:</b> {users_count}\n"
        f"📄 <b>Old API Page:</b> {tracking.get('current_page', 203)}\n"
        f"✅ <b>Old API Processed:</b> {len(tracking.get('processed_slugs', []))}\n"
        f"🔄 <b>Old API In Progress:</b> {len(tracking.get('in_progress_slugs', []))}\n\n"
        f"📡 <b>VID65 Page:</b> {tracking.get('vid65_page', 1)}\n"
        f"✅ <b>VID65 Processed:</b> {len(tracking.get('vid65_processed_ids', []))}\n"
        f"🔄 <b>VID65 In Progress:</b> {len(tracking.get('vid65_in_progress_ids', []))}\n"
        f"📥 <b>Queue:</b> {queue_count}\n"
        f"🆔 <b>Admin:</b> <code>{ADMIN_ID}</code>"
    )

    send_to_telegram("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": admin_keyboard(),
    })


# ==========================================================
# VIDEO / CRON
# ==========================================================

def run_cron_with_progress(admin_chat_id: str, source: Optional[str] = None) -> None:
    if source is None:
        send_to_telegram("sendMessage", {
            "chat_id": admin_chat_id,
            "text": "📡 <b>Select API source</b>",
            "parse_mode": "HTML",
            "reply_markup": json.dumps({
                "inline_keyboard": [[
                    {"text": "1️⃣ Old API", "callback_data": "cron_source_old"},
                    {"text": "2️⃣ VID65 API", "callback_data": "cron_source_vid65"},
                ], [
                    {"text": "▶️ Run Both", "callback_data": "cron_source_both"}
                ]]
            }),
        })
        return

    lock = FileLock(CRON_LOCK, blocking=False)
    if not lock.acquire():
        send_to_telegram("sendMessage", {
            "chat_id": admin_chat_id,
            "text": "⚠️ A cron is already running."
        })
        return

    try:
        sources = ["old", "vid65"] if source == "both" else [source]
        for src in sources:
            cron_pipeline(admin_chat_id, GROUPS, CHANNELS, src)
    finally:
        lock.release()


def cron_source_info(source: str) -> dict:
    if source == "vid65":
        return {
            "label": "VID65 API",
            "api": API_VID65,
            "pageKey": "vid65_page",
            "processedKey": "vid65_processed_ids",
            "inProgressKey": "vid65_in_progress_ids",
        }

    return {
        "label": "Old API",
        "api": API_BASE,
        "pageKey": "current_page",
        "processedKey": "processed_slugs",
        "inProgressKey": "in_progress_slugs",
    }


def download_file_with_progress(
    url: str,
    destination: Path,
    admin_chat_id: str,
    message_id: int,
    use_photo: bool,
    title: str,
    slug: str,
    page: int,
    total: int,
    item_num: int,
    label: str,
) -> bool:
    try:
        with _session.get(url, stream=True, timeout=1800, verify=True) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get("content-length", 0))

            downloaded = 0
            last_pct = -1
            last_ts = 0

            with destination.open("wb") as fp:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    fp.write(chunk)
                    downloaded += len(chunk)

                    if total_bytes > 0:
                        pct = int(downloaded * 100 / total_bytes)
                        now = time.time()
                        if pct != last_pct and now - last_ts >= 2 and pct < 100:
                            last_pct = pct
                            last_ts = now
                            mb_now = round(downloaded / 1048576, 1)
                            mb_total = round(total_bytes / 1048576, 1)

                            progress_edit(
                                admin_chat_id,
                                message_id,
                                use_photo,
                                (
                                    f"📥 <b>Downloading...</b> {pct}%\n\n"
                                    f"📡 {label}\n"
                                    f"🎬 <b>{title}</b>\n"
                                    f"🆔 <code>{slug}</code>\n"
                                    f"📄 Page: {page} | Item: {item_num}/{total}\n"
                                    f"📦 {mb_now} MB / {mb_total} MB"
                                ),
                            )

        return destination.exists() and destination.stat().st_size > 0

    except Exception as e:
        blog(f"Video download: {e}")
        return False


def cron_pipeline(
    admin_chat_id: str,
    groups: list,
    channels: list,
    source: str = "old",
) -> None:
    info = cron_source_info(source)
    tracking = get_tracking()

    page = int(tracking.get(info["pageKey"], 1 if source == "vid65" else 203))
    mode = tracking.get("mode", "ON")
    processed = tracking.get(info["processedKey"], [])
    in_progress = tracking.get(info["inProgressKey"], [])

    if source == "vid65":
        home_url = f"{info['api']}?page={max(1, page)}"
    else:
        home_url = f"{info['api']}?action=home&page={max(1, page)}"

    home = fetch_api_data(home_url)

    if not home or not home.get("data"):
        send_to_telegram("sendMessage", {
            "chat_id": admin_chat_id,
            "text": f"❌ <b>{info['label']}</b>: no videos on page {page}.",
            "parse_mode": "HTML",
        })
        return

    videos = home["data"]
    total = len(videos)

    v = None
    current_idx = -1
    item_key = ""

    if source == "vid65":
        for i, item in enumerate(videos):
            item_key = "id:" + str(item.get("id", ""))
            if item.get("id") in (None, ""):
                continue
            if item_key in processed:
                continue
            if item_key in in_progress:
                continue
            v = item
            current_idx = i
            break
    else:
        for i in range(total - 1, -1, -1):
            s = videos[i].get("slug", "")
            if not s:
                continue
            if s in processed:
                continue
            if s in in_progress:
                continue
            v = videos[i]
            current_idx = i
            item_key = s
            break

    if not v:
        if source == "vid65":
            last_page = int(home.get("pagination", {}).get("total_pages", page))
            if page < last_page:
                tracking[info["pageKey"]] = page + 1
                save_tracking(tracking)
                send_to_telegram("sendMessage", {
                    "chat_id": admin_chat_id,
                    "text": (
                        f"✅ <b>VID65 page {page} done</b> → "
                        f"next page: {page + 1}"
                    ),
                    "parse_mode": "HTML",
                })
            else:
                send_to_telegram("sendMessage", {
                    "chat_id": admin_chat_id,
                    "text": "✅ <b>VID65 API finished.</b> All available pages are processed.",
                    "parse_mode": "HTML",
                })
        else:
            tracking[info["pageKey"]] = max(1, page - 1)
            save_tracking(tracking)
            send_to_telegram("sendMessage", {
                "chat_id": admin_chat_id,
                "text": (
                    f"✅ <b>Old API page {page} done</b> → "
                    f"next: {tracking[info['pageKey']]}"
                ),
                "parse_mode": "HTML",
            })
        return

    if source == "vid65":
        item_id = str(v["id"])
        title = html.escape(str(v.get("name", "Untitled")))
        slug = f"vid65-{item_id}"
        thumb_url = v.get("image", "")
        link = v.get("video", "")
    else:
        title = html.escape(str(v.get("title", "Untitled")))
        slug = str(v.get("slug", ""))
        thumb_url = v.get("thumbnail", "")
        link = ""

    item_num = current_idx + 1
    mode_label = "Video" if mode == "ON" else "Image"

    tracking.setdefault(info["inProgressKey"], [])
    tracking[info["inProgressKey"]].append(item_key)
    save_tracking(tracking)

    # Thumbnail
    thumb_path = None
    if thumb_url:
        try:
            r = _session.get(thumb_url, timeout=60, verify=True)
            raw = r.content
            if len(raw) > 100:
                t = BASE_DIR / f"thumb_init_{int(time.time())}_{int(time.time_ns() % 9000 + 1000)}.jpg"
                t.write_bytes(raw)
                if Image:
                    try:
                        Image.open(t).verify()
                        thumb_path = t
                    except Exception:
                        t.unlink(missing_ok=True)
        except Exception as e:
            blog(f"Thumbnail download: {e}")

    init_caption = (
        "⏳ <b>Cron started...</b>\n\n"
        f"📡 <b>Source:</b> {info['label']}\n"
        f"🎬 <b>{title}</b>\n"
        f"🆔 <code>{html.escape(slug)}</code>\n"
        f"📄 Page: {page} | Item: {item_num}/{total}\n"
        f"⚙️ Mode: {mode_label}"
    )

    if thumb_path:
        init = send_to_telegram_file(
            "sendPhoto",
            {
                "chat_id": admin_chat_id,
                "caption": init_caption,
                "parse_mode": "HTML",
            },
            "photo",
            thumb_path,
        )
        use_photo = True
    else:
        init = send_to_telegram("sendMessage", {
            "chat_id": admin_chat_id,
            "text": init_caption,
            "parse_mode": "HTML",
        })
        use_photo = False

    msg_id = init.get("result", {}).get("message_id")

    if not msg_id:
        tracking[info["inProgressKey"]] = [
            x for x in tracking[info["inProgressKey"]] if x != item_key
        ]
        save_tracking(tracking)
        if thumb_path:
            thumb_path.unlink(missing_ok=True)
        return

    # Old API needs second request
    if source == "old":
        vd = fetch_api_data(f"{API_BASE}?action=video&id={requests.utils.quote(slug)}")
        link = (vd or {}).get("data", {}).get("downloadLink", "")

    if not link:
        progress_edit(
            admin_chat_id,
            msg_id,
            use_photo,
            (
                "❌ <b>No download link</b>\n\n"
                f"📡 {info['label']}\n"
                f"🎬 <b>{title}</b>\n"
                f"🆔 <code>{html.escape(slug)}</code>"
            ),
        )

        tracking[info["processedKey"]].append(item_key)
        tracking[info["inProgressKey"]] = [
            x for x in tracking[info["inProgressKey"]] if x != item_key
        ]
        save_tracking(tracking)

        if thumb_path:
            thumb_path.unlink(missing_ok=True)
        return

    # Download
    tmp = BASE_DIR / f"cron_{int(time.time())}_{int(time.time_ns() % 9000 + 1000)}.mp4"

    ok_download = download_file_with_progress(
        link,
        tmp,
        admin_chat_id,
        msg_id,
        use_photo,
        title,
        slug,
        page,
        total,
        item_num,
        info["label"],
    )

    if not ok_download:
        progress_edit(
            admin_chat_id,
            msg_id,
            use_photo,
            (
                "❌ <b>Download failed</b>\n\n"
                f"📡 {info['label']}\n"
                f"🎬 <b>{title}</b>\n"
                f"🆔 <code>{html.escape(slug)}</code>"
            ),
        )

        tmp.unlink(missing_ok=True)
        tracking[info["inProgressKey"]] = [
            x for x in tracking[info["inProgressKey"]] if x != item_key
        ]
        save_tracking(tracking)

        if thumb_path:
            thumb_path.unlink(missing_ok=True)
        return

    # Blur thumbnail
    blur_thumb = BASE_DIR / f"thumb_blur_{int(time.time())}_{int(time.time_ns() % 9000 + 1000)}.jpg"
    has_thumb = False

    if thumb_path:
        has_thumb = True
        if not apply_blur(thumb_path, blur_thumb, BLUR_PERCENTAGE):
            try:
                blur_thumb.write_bytes(thumb_path.read_bytes())
            except Exception:
                pass

    # Upload
    channel_caption = "Join @virulvideopompom 🎬\n\nhttps://t.me/+Uj_xq6904lMzYWVl"
    group_caption = f"🎬 <b>{title}</b>\n\n🔥 @virulvideopompom"
    sent_as_video = False

    if mode == "ON":
        for g in groups:
            res = video_upload(
                "group", g, tmp, thumb_path, has_thumb, group_caption,
                admin_chat_id, msg_id, use_photo, title, slug,
                page, total, item_num
            )
            if res.get("ok"):
                pass
            time.sleep(1)

        for c in channels:
            res = video_upload(
                "channel", c, tmp, blur_thumb, has_thumb, channel_caption,
                admin_chat_id, msg_id, use_photo, title, slug,
                page, total, item_num
            )
            if res.get("ok") and res.get("result", {}).get("message_id") is not None:
                manage_channel_history(c, res["result"]["message_id"])
                sent_as_video = True

    if not sent_as_video:
        progress_edit(
            admin_chat_id,
            msg_id,
            use_photo,
            (
                "⚠️ <b>Falling back to photo/link...</b>\n\n"
                f"📡 {info['label']}\n"
                f"🎬 <b>{title}</b>\n"
                f"🆔 <code>{html.escape(slug)}</code>"
            ),
        )

        for g in groups:
            if mode == "OFF" and has_thumb and thumb_path:
                send_to_telegram_file(
                    "sendPhoto",
                    {
                        "chat_id": g,
                        "caption": group_caption,
                        "parse_mode": "HTML",
                        "protect_content": True,
                    },
                    "photo",
                    thumb_path,
                )
            else:
                send_to_telegram("sendMessage", {
                    "chat_id": g,
                    "text": f"🎬 <b>{title}</b>\n\n📥 {link}\n\n🔥 @virulvideopompom",
                    "parse_mode": "HTML",
                    "protect_content": True,
                })
            time.sleep(1)

        if has_thumb and blur_thumb.exists():
            for c in channels:
                res = send_to_telegram_file(
                    "sendPhoto",
                    {
                        "chat_id": c,
                        "caption": channel_caption,
                        "parse_mode": "HTML",
                    },
                    "photo",
                    blur_thumb,
                )
                if res.get("ok") and res.get("result", {}).get("message_id") is not None:
                    manage_channel_history(c, res["result"]["message_id"])

    # Cleanup
    tmp.unlink(missing_ok=True)
    if thumb_path:
        thumb_path.unlink(missing_ok=True)
    blur_thumb.unlink(missing_ok=True)

    # Finalize
    tracking = get_tracking()
    tracking.setdefault(info["processedKey"], [])
    tracking.setdefault(info["inProgressKey"], [])

    tracking[info["processedKey"]].append(item_key)
    if len(tracking[info["processedKey"]]) > 3000:
        tracking[info["processedKey"]].pop(0)

    tracking[info["inProgressKey"]] = [
        x for x in tracking[info["inProgressKey"]] if x != item_key
    ]
    save_tracking(tracking)

    progress_edit(
        admin_chat_id,
        msg_id,
        use_photo,
        (
            "✅ <b>Cron finished!</b>\n\n"
            f"📡 <b>{info['label']}</b>\n"
            f"🎬 <b>{title}</b>\n"
            f"🆔 <code>{html.escape(slug)}</code>\n"
            f"📄 <b>Page:</b> {page} | Item: {item_num}/{total}\n"
            f"📤 <b>Sent as video:</b> {'Yes' if sent_as_video else 'No'}"
        ),
    )


def video_upload(
    where: str,
    target: str,
    tmp: Path,
    thumb: Optional[Path],
    has_thumb: bool,
    caption: str,
    admin_chat_id: str,
    msg_id: int,
    use_photo: bool,
    title: str,
    slug: str,
    page: int,
    total: int,
    item_num: int,
) -> dict:
    # Telegram's HTTP Bot API handles the actual multipart upload.
    # Status is updated before and after the transfer to preserve the
    # original admin-visible workflow.
    progress_edit(
        admin_chat_id,
        msg_id,
        use_photo,
        (
            f"📤 <b>Uploading to {where}...</b>\n\n"
            f"🎬 <b>{title}</b>\n"
            f"🆔 <code>{html.escape(slug)}</code>\n"
            f"📄 Page: {page} | Item: {item_num}/{total}"
        ),
    )

    return send_to_telegram_file(
        "sendVideo",
        {
            "chat_id": target,
            "caption": caption,
            "parse_mode": "HTML",
            "protect_content": True,
            "supports_streaming": True,
        },
        "video",
        tmp,
        thumb if has_thumb and thumb and thumb.exists() else None,
    )


def admin_download_by_index(chat_id: str, page: int, index: int) -> None:
    data = fetch_api_data(f"{API_BASE}?action=home&page={int(page)}")
    if not data or not data.get("data") or index < 0 or index >= len(data["data"]):
        send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Video not found."
        })
        return

    v = data["data"][index]
    slug = v.get("slug", "")
    title = html.escape(str(v.get("title", "Untitled")))

    vd = fetch_api_data(f"{API_BASE}?action=video&id={requests.utils.quote(slug)}")
    link = (vd or {}).get("data", {}).get("downloadLink", "")

    if not link:
        send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"❌ No download link.\n\n"
                f"🎬 <b>{title}</b>\n"
                f"🆔 <code>{html.escape(slug)}</code>"
            ),
            "parse_mode": "HTML",
        })
        return

    tmp = BASE_DIR / f"browse_{int(time.time())}.mp4"

    try:
        with _session.get(link, stream=True, timeout=1800, verify=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as fp:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fp.write(chunk)
    except Exception as e:
        blog(f"Browse download: {e}")
        tmp.unlink(missing_ok=True)
        send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Download failed."
        })
        return

    if not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        send_to_telegram("sendMessage", {
            "chat_id": chat_id,
            "text": "❌ Download failed."
        })
        return

    thumb = None

    if v.get("thumbnail"):
        try:
            r = _session.get(v["thumbnail"], timeout=60, verify=True)
            if len(r.content) > 100:
                t = BASE_DIR / f"browse_thumb_{int(time.time())}.jpg"
                t.write_bytes(r.content)
                if Image:
                    try:
                        Image.open(t).verify()
                        thumb = t
                    except Exception:
                        t.unlink(missing_ok=True)
        except Exception:
            pass

    send_to_telegram_file(
        "sendVideo",
        {
            "chat_id": chat_id,
            "caption": f"🎬 <b>{title}</b>\n🆔 <code>{html.escape(slug)}</code>",
            "parse_mode": "HTML",
            "supports_streaming": True,
        },
        "video",
        tmp,
        thumb,
    )

    tmp.unlink(missing_ok=True)
    if thumb:
        thumb.unlink(missing_ok=True)


# ==========================================================
# ROUTER
# ==========================================================

def handle_update(update: dict) -> None:
    msg = update.get("message")
    cq = update.get("callback_query")

    chat_type = ""
    if msg:
        chat_type = msg.get("chat", {}).get("type", "")
    elif cq:
        chat_type = cq.get("message", {}).get("chat", {}).get("type", "")

    if chat_type != "private":
        blog(f"ignored chat type '{chat_type}'")
        return

    from_id = None
    if msg:
        from_id = msg.get("from", {}).get("id")
    elif cq:
        from_id = cq.get("from", {}).get("id")

    if str(from_id) == str(ADMIN_ID):
        admin_handle(update)
    else:
        user_handle(update)


# ==========================================================
# QUEUE PROCESSOR
# ==========================================================

def process_queue() -> None:
    lock = FileLock(PROCESSOR_LOCK, blocking=False)
    if not lock.acquire():
        return

    try:
        seen = json_load(SEEN_FILE, {})
        if not isinstance(seen, dict):
            seen = {}

        if len(seen) > 2000:
            keys = list(seen.keys())[-2000:]
            seen = {k: seen[k] for k in keys}

        count = 0

        while count < MAX_QUEUE_PROCESS:
            item = queue_pop()
            if not item:
                break

            uid = item.get("update_id")

            if uid is not None and str(uid) in seen:
                blog(f"skip seen {uid}")
                continue

            if uid is not None:
                seen[str(uid)] = int(time.time())
                json_save(SEEN_FILE, seen)

            try:
                handle_update(item)
            except Exception as e:
                blog(f"handle_update: {e}")

            count += 1

    finally:
        lock.release()


# ==========================================================
# WEBHOOK / EXTERNAL CRON
# ==========================================================

app = Flask(__name__) if Flask else None


if app:

    @app.route("/", methods=["GET", "POST"])
    def webhook():
        # External cron trigger
        if request.args.get("run_cron") is not None:
            if request.args.get("secret", "") != CRON_SECRET:
                return jsonify({"ok": False}), 403

            # Match the original: respond immediately, then run the work.
            def background():
                try:
                    run_cron_with_progress(ADMIN_ID)
                except Exception as e:
                    blog(f"external cron: {e}")

            threading.Thread(target=background, daemon=True).start()
            return jsonify({"ok": True})

        # Telegram webhook
        update = request.get_json(silent=True)
        if update:
            queue_push(update)

        # Process asynchronously so webhook response remains fast.
        threading.Thread(target=process_queue, daemon=True).start()

        return jsonify({"ok": True})


# ==========================================================
# MAIN
# ==========================================================

def main():
    if not app:
        raise SystemExit("Install Flask: pip install flask")

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))

    blog("Python bot started")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
