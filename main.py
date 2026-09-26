#!/usr/bin/env python3
"""
vid65 bot — single-file scraper + Telegram control bot.

One JSON file: vid65.json (cumulative, atomic writes).

Per page:
  1. Fetch items from source API
  2. Send progress message: every ID on the page, all ⏳
  3. One item at a time:
       download → upload → grab file_id → delete media → append to vid65.json
       → EDIT progress message to mark ✅
  4. When page done: send vid65.json (the single, full file)

Auto Mode:
  Walk pages one by one automatically. Same flow per page.
  Stop any time with 🛑 Stop Auto or /stop.
"""

import io
import os
import json
import time
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests

# ================== CONFIG ==================
BOT_TOKEN   = "6757665465:AAEG8h6CRtDvm_5tVihxhcOewze9TcrsvwM"
ADMIN_ID    = 5087403859

STORAGE_CHAT_ID     = "-1003916426485"      # e.g. "-1001234567890" to hide media from admin chat
DELETE_AFTER_UPLOAD = True

LOCAL_API   = "https://telegram-bot-api-production-2276.up.railway.app"
API_URL     = "https://shabbir.serv00.net/sex/vid65/get.php"

TOTAL_PAGES = 60
STATE_FILE  = "vid65.json"
ITEM_DELAY  = 2.0
PAGE_DELAY  = 3.0

PAGE_FETCH_TIMEOUT = 30
DOWNLOAD_TIMEOUT   = 120
DOWNLOAD_RETRIES   = 3
UPLOAD_RETRIES     = 3

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vid65")

# ================== RUNTIME FLAGS ==================
_processing = threading.Event()
_auto_mode  = threading.Event()
_stop_auto  = threading.Event()

# ================== STATE ==================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"current_page": 1, "items": [], "updated_at": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("current_page", 1)
        s.setdefault("items", [])
        s.setdefault("updated_at", None)
        return s
    except Exception as e:
        log.error(f"state load failed ({e}); starting fresh")
        return {"current_page": 1, "items": [], "updated_at": None}


def save_state(s: Dict[str, Any]) -> None:
    s["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def done_ids(state: Dict[str, Any]) -> set:
    return {it["id"] for it in state.get("items", [])}

# ================== TELEGRAM API ==================
def _url(method: str) -> str:
    return f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"


def tg(method: str, timeout: Optional[int] = 60, **kwargs) -> dict:
    r = requests.post(_url(method), timeout=timeout, **kwargs)
    r.raise_for_status()
    return r.json()


def tg_send(chat_id, text: str, reply_markup: Optional[dict] = None):
    data = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        res = tg("sendMessage", data=data, timeout=30)
        return res["result"]["message_id"] if res.get("ok") else None
    except Exception as e:
        log.warning(f"sendMessage failed: {e}")
        return None


def tg_delete(chat_id, message_id: int) -> None:
    try:
        tg("deleteMessage",
           data={"chat_id": chat_id, "message_id": message_id},
           timeout=15)
    except Exception as e:
        log.debug(f"deleteMessage failed: {e}")


def tg_edit(chat_id, message_id, text: str, reply_markup: Optional[dict] = None) -> bool:
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    for attempt in range(3):
        try:
            r = requests.post(_url("editMessageText"), data=data, timeout=30)
            if r.status_code == 429:
                retry = r.json().get("parameters", {}).get("retry_after", 2)
                time.sleep(retry + 0.5)
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            log.debug(f"editMessageText failed (try {attempt+1}): {e}")
            time.sleep(1)
    return False


def tg_answer_cb(cb_id: str, text: str = ""):
    try:
        tg("answerCallbackQuery",
           data={"callback_query_id": cb_id, "text": text[:200]},
           timeout=15)
    except Exception:
        pass


def tg_send_document(chat_id: int, path: str, caption: str = "") -> None:
    try:
        with open(path, "rb") as f:
            files = {"document": (os.path.basename(path), f)}
            data = {"chat_id": chat_id,
                    "caption": caption[:1024],
                    "parse_mode": "HTML"}
            r = requests.post(_url("sendDocument"),
                              files=files, data=data, timeout=None)
            r.raise_for_status()
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(res)
    except Exception as e:
        log.warning(f"sendDocument failed: {e}")
        tg_send(chat_id, f"⚠️ Could not send {os.path.basename(path)}: <code>{str(e)[:200]}</code>")


def send_full_file(chat_id: int, caption_prefix: str = "") -> None:
    """Send the single cumulative vid65.json."""
    state = load_state()
    total = len(state.get("items", []))
    head = f"{caption_prefix}".strip()
    head = (head + "  ") if head else ""
    tg_send_document(
        chat_id, STATE_FILE,
        caption=(f"{head}📁 <b>{STATE_FILE}</b>\n"
                 f"Total items: <b>{total}</b>")
    )


def tg_get_updates(offset: Optional[int] = None, timeout: int = 30) -> List[dict]:
    params = {"timeout": timeout,
              "allowed_updates": '["message","callback_query"]'}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(_url("getUpdates"), params=params, timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result", [])


def verify_token() -> None:
    try:
        r = requests.get(_url("getMe"), timeout=15)
    except Exception as e:
        raise SystemExit(f"Can't reach local Bot API server: {e}")
    try:
        body = r.json()
    except Exception:
        raise SystemExit(f"Local server returned non-JSON: {r.text[:200]}")
    if r.status_code != 200 or not body.get("ok"):
        raise SystemExit(f"Bot token rejected: HTTP {r.status_code} — {r.text[:300]}")
    me = body["result"]
    log.info(f"Token OK — bot @{me.get('username')} (id={me.get('id')})")

# ================== DOWNLOAD ==================
def download_bytes(url: str, retries: int = DOWNLOAD_RETRIES) -> bytes:
    last = None
    for i in range(1, retries + 1):
        try:
            with requests.get(url, stream=True,
                              timeout=DOWNLOAD_TIMEOUT,
                              headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        buf.write(chunk)
                return buf.getvalue()
        except Exception as e:
            last = e
            log.warning(f"download try {i}/{retries} failed: {e}")
            time.sleep(2 ** i)
    raise last  # type: ignore

# ================== SCRAPER API ==================
def fetch_page(page: int) -> List[dict]:
    r = requests.get(API_URL, params={"page": page},
                     timeout=PAGE_FETCH_TIMEOUT,
                     headers={"User-Agent": "Mozilla/5.0",
                              "Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        return []
    return data.get("data", [])

# ================== UPLOAD ==================
def _target_chat() -> int:
    return int(STORAGE_CHAT_ID) if STORAGE_CHAT_ID else ADMIN_ID


def _should_delete() -> bool:
    return (not STORAGE_CHAT_ID) and DELETE_AFTER_UPLOAD


def upload_photo(image_bytes: bytes, filename: str, caption: str):
    for i in range(1, UPLOAD_RETRIES + 1):
        try:
            files = {"photo": (filename, image_bytes)}
            data = {"chat_id": _target_chat(), "caption": caption[:1024]}
            r = requests.post(_url("sendPhoto"), files=files, data=data, timeout=None)
            r.raise_for_status()
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(res)
            msg_id = res["result"]["message_id"]
            fid = res["result"]["photo"][-1]["file_id"]
            if _should_delete():
                tg_delete(_target_chat(), msg_id)
            return fid, msg_id
        except Exception as e:
            log.warning(f"upload_photo try {i}/{UPLOAD_RETRIES} failed: {e}")
            if i == UPLOAD_RETRIES:
                raise
            time.sleep(2 ** i)


def upload_video(video_bytes: bytes, filename: str, caption: str):
    for i in range(1, UPLOAD_RETRIES + 1):
        try:
            files = {"video": (filename, video_bytes)}
            data = {"chat_id": _target_chat(),
                    "caption": caption[:1024],
                    "supports_streaming": "true"}
            r = requests.post(_url("sendVideo"), files=files, data=data, timeout=None)
            r.raise_for_status()
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(res)
            result = res["result"]
            if "video" in result:
                fid = result["video"]["file_id"]
            elif "document" in result:
                fid = result["document"]["file_id"]
            elif "animation" in result:
                fid = result["animation"]["file_id"]
            else:
                raise RuntimeError(f"no video/document in response: {result}")
            msg_id = result["message_id"]
            if _should_delete():
                tg_delete(_target_chat(), msg_id)
            return fid, msg_id
        except Exception as e:
            log.warning(f"upload_video try {i}/{UPLOAD_RETRIES} failed: {e}")
            if i == UPLOAD_RETRIES:
                raise
            time.sleep(2 ** i)

# ================== PROCESSING ==================
def process_item(item: dict, page: int) -> dict:
    item_id = int(item["id"])
    name = item["name"]
    image_url = item["image"]
    video_url = item["video"]

    img_name = os.path.basename(image_url.split("?")[0]) or f"{item_id}.jpg"
    vid_name = os.path.basename(video_url.split("?")[0]) or f"{item_id}.mp4"

    log.info(f"[{item_id}] downloading image…")
    image_bytes = download_bytes(image_url)

    log.info(f"[{item_id}] downloading video…")
    video_bytes = download_bytes(video_url)
    size_mb = round(len(video_bytes) / 1e6, 2)

    log.info(f"[{item_id}] uploading image (temp)…")
    image_id, image_msg = upload_photo(image_bytes, img_name, name)

    log.info(f"[{item_id}] uploading video ({size_mb} MB, temp)…")
    video_id, video_msg = upload_video(video_bytes, vid_name, name)

    return {
        "id": item_id,
        "name": name,
        "image_id": image_id,
        "video_id": video_id,
        "image_msg_id": image_msg,
        "video_msg_id": video_msg,
        "chat_id": _target_chat(),
        "size_mb": size_mb,
        "page": page,
        "create_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------- progress message rendering ----------
def render_progress(page: int, items: List[dict],
                    status: Dict[int, dict],
                    total_saved: int,
                    prefix: str = "") -> str:
    done_n = sum(1 for v in status.values() if v["state"] == "done")
    fail_n = sum(1 for v in status.values() if v["state"] == "failed")
    total_n = len(items)

    head = f"📄 <b>Page {page}</b>  —  {done_n}/{total_n} done"
    if fail_n:
        head += f"  (❌ {fail_n})"
    if prefix:
        head = f"{prefix} {head}"

    lines = [head, "━━━━━━━━━━━━━━━"]

    for it in items:
        iid = int(it["id"])
        st = status.get(iid, {"state": "pending"})
        if st["state"] == "pending":
            lines.append(f"⏳ <code>{iid}</code>  {it['name'][:48]}")
        elif st["state"] == "done":
            lines.append(
                f"✅ <code>{iid}</code>  {it['name'][:48]}\n"
                f"    📦 {st.get('size_mb')} MB  "
                f"🖼 <code>{(st.get('image_id') or '')[:12]}…</code>  "
                f"🎬 <code>{(st.get('video_id') or '')[:12]}…</code>"
            )
        else:
            lines.append(
                f"❌ <code>{iid}</code>  {it['name'][:48]}\n"
                f"    <i>{(st.get('error') or 'error')[:120]}</i>"
            )

    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"💾 Total saved in {STATE_FILE}: <b>{total_saved}</b>")
    return "\n".join(lines)


def process_page(chat_id: int, page: int,
                 auto: bool = False,
                 auto_counter: Optional[tuple] = None) -> bool:
    """
    Process one page. Returns True if it ran to completion,
    False if it was aborted by Stop Auto.
    """
    state = load_state()
    seen = done_ids(state)

    try:
        items = fetch_page(page)
    except Exception as e:
        tg_send(chat_id, f"❌ Could not fetch page {page}: <code>{e}</code>")
        return True

    if not items:
        tg_send(chat_id, f"⚠️ Page {page} returned no items.")
        return True

    status: Dict[int, dict] = {}
    for it in items:
        iid = int(it["id"])
        if iid in seen:
            status[iid] = {"state": "done", "size_mb": None,
                           "image_id": None, "video_id": None}
        else:
            status[iid] = {"state": "pending"}

    prefix = ""
    if auto and auto_counter:
        cur, total = auto_counter
        prefix = f"🟢 <b>AUTO {cur}/{total}</b>"

    total_saved_now = len(state["items"])
    text = render_progress(page, items, status, total_saved_now, prefix)
    progress_msg_id = tg_send(chat_id, text)

    todo = [it for it in items if status[int(it["id"])]["state"] == "pending"]

    if not todo:
        tg_send(chat_id, f"✅ Page {page}: all <b>{len(items)}</b> items already saved.")
        send_full_file(chat_id, caption_prefix=f"ℹ️ page {page} — no new items")
        return True

    ok = 0
    bad = 0
    aborted = False
    for idx, item in enumerate(todo, 1):
        if _stop_auto.is_set() and auto:
            aborted = True
            break

        iid = int(item["id"])
        try:
            row = process_item(item, page)

            state = load_state()
            state["items"].append(row)
            state["current_page"] = page
            save_state(state)

            status[iid] = {
                "state": "done",
                "size_mb": row["size_mb"],
                "image_id": row["image_id"],
                "video_id": row["video_id"],
            }
            ok += 1
        except Exception as e:
            bad += 1
            log.error(f"item {iid} failed: {e}", exc_info=True)
            status[iid] = {"state": "failed", "error": str(e)[:250]}

        state = load_state()
        if progress_msg_id is not None:
            tg_edit(chat_id, progress_msg_id,
                    render_progress(page, items, status, len(state["items"]), prefix))

        t = 0.0
        while t < ITEM_DELAY:
            if _stop_auto.is_set() and auto:
                aborted = True
                break
            time.sleep(0.2)
            t += 0.2
        if aborted:
            break

    state = load_state()
    tail = " (aborted by Stop)" if aborted else ""
    tg_send(
        chat_id,
        f"🏁 <b>Page {page} complete</b>{tail}\n"
        f"✅ {ok}   ❌ {bad}   💾 total: <b>{len(state['items'])}</b>"
    )

    # Send the single cumulative file
    send_full_file(chat_id, caption_prefix=f"🏁 page {page} done")

    return not aborted

# ================== AUTO MODE ==================
def auto_worker(chat_id: int, start_page: int) -> None:
    _processing.set()
    _auto_mode.set()
    _stop_auto.clear()

    try:
        total_planned = TOTAL_PAGES - start_page + 1
        tg_send(chat_id,
                f"🟢 <b>Auto Mode started</b>\n"
                f"From page <b>{start_page}</b> to <b>{TOTAL_PAGES}</b> "
                f"({total_planned} page(s)).\n"
                f"Send /stop or tap 🛑 to abort.")

        counter = 0
        page = start_page
        while page <= TOTAL_PAGES:
            if _stop_auto.is_set():
                break

            counter += 1
            state = load_state()
            state["current_page"] = page
            save_state(state)

            completed = process_page(
                chat_id, page,
                auto=True,
                auto_counter=(counter, total_planned),
            )

            if not completed:
                log.info(f"Auto mode stopped at page {page}")
                break

            state = load_state()
            state["current_page"] = min(page + 1, TOTAL_PAGES)
            save_state(state)

            page += 1
            t = 0.0
            while t < PAGE_DELAY:
                if _stop_auto.is_set():
                    break
                time.sleep(0.2)
                t += 0.2

        state = load_state()
        if _stop_auto.is_set():
            tg_send(chat_id,
                    f"🛑 <b>Auto Mode stopped.</b>\n"
                    f"Next page will be <b>{state['current_page']}</b>.\n"
                    f"Total saved: <b>{len(state['items'])}</b>")
            send_full_file(chat_id, caption_prefix="🛑 auto stopped")
        else:
            tg_send(chat_id,
                    f"🎉 <b>Auto Mode finished.</b>\n"
                    f"Pages processed: <b>{counter}</b>\n"
                    f"Total saved: <b>{len(state['items'])}</b>")
            send_full_file(chat_id, caption_prefix="🎉 auto complete")

    except Exception as e:
        log.error(f"auto_worker error: {e}", exc_info=True)
        tg_send(chat_id, f"❌ Auto mode error: <code>{str(e)[:250]}</code>")
    finally:
        _auto_mode.clear()
        _processing.clear()
        _stop_auto.clear()
        try:
            send_menu(chat_id)
        except Exception:
            pass


def start_auto_mode(chat_id: int, start_page: int) -> bool:
    if _processing.is_set():
        tg_send(chat_id, "⏳ Something is already running. Stop it first.")
        return False
    threading.Thread(target=auto_worker,
                     args=(chat_id, start_page),
                     daemon=True).start()
    return True

# ================== UI ==================
def menu_keyboard(page: int) -> dict:
    auto_running = _auto_mode.is_set()
    row2 = (
        [{"text": "🛑 Stop Auto", "callback_data": "auto:stop"},
         {"text": "📊 Status", "callback_data": "action:status"},
         {"text": "📁 Send JSON", "callback_data": "action:json"}]
        if auto_running else
        [{"text": f"▶️ Process Page {page}", "callback_data": "action:process"},
         {"text": "🚀 Auto Mode", "callback_data": "auto:start"},
         {"text": "📊 Status", "callback_data": "action:status"}]
    )
    rows = [
        [
            {"text": "⬅️ Prev", "callback_data": "nav:prev"},
            {"text": f"📄 Page {page}/{TOTAL_PAGES}", "callback_data": "noop"},
            {"text": "Next & Process ➡️", "callback_data": "nav:next"},
        ],
        row2,
    ]
    if not auto_running:
        rows.append([{"text": "📁 Send JSON", "callback_data": "action:json"}])
    return {"inline_keyboard": rows}


def menu_text(state: Dict[str, Any]) -> str:
    page = state.get("current_page", 1)
    n = len(state.get("items", []))
    last = state.get("updated_at") or "—"

    if _auto_mode.is_set():
        status_line = "🟢 <b>AUTO MODE RUNNING</b>"
    elif _processing.is_set():
        status_line = "🟡 Processing…"
    else:
        status_line = "⚪ Idle"

    storage = STORAGE_CHAT_ID or ("admin chat (auto-deleted)"
                                  if DELETE_AFTER_UPLOAD else "admin chat")

    return (
        f"🤖 <b>vid65 Scraper Bot</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Status:           {status_line}\n"
        f"📄 Current page:  <b>{page}</b> / {TOTAL_PAGES}\n"
        f"✅ Items saved:   <b>{n}</b>\n"
        f"🗄 Upload target: <code>{storage}</code>\n"
        f"🕒 Last update:   <b>{last}</b>\n\n"
        f"<i>After each page: bot sends {STATE_FILE} (all data, one file).</i>"
    )


def send_menu(chat_id: int) -> None:
    state = load_state()
    tg_send(chat_id, menu_text(state),
            reply_markup=menu_keyboard(state["current_page"]))


def edit_menu(chat_id: int, message_id: int) -> None:
    state = load_state()
    tg_edit(chat_id, message_id, menu_text(state),
            reply_markup=menu_keyboard(state["current_page"]))

# ================== HANDLERS ==================
def start_page_processing(chat_id: int, page: int) -> bool:
    if _processing.is_set():
        tg_send(chat_id, "⏳ Already processing something — please wait.")
        return False

    def worker():
        _processing.set()
        try:
            process_page(chat_id, page, auto=False)
        except Exception as e:
            log.error(f"worker error: {e}", exc_info=True)
            tg_send(chat_id, f"❌ Worker error: <code>{str(e)[:250]}</code>")
        finally:
            _processing.clear()
            try:
                send_menu(chat_id)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return True


def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if chat_id != ADMIN_ID:
        return
    text = (msg.get("text") or "").strip()

    if text.startswith("/start") or text.startswith("/menu"):
        send_menu(chat_id)
    elif text.startswith("/status"):
        tg_send(chat_id, menu_text(load_state()))
    elif text.startswith("/json"):
        send_full_file(chat_id, caption_prefix="📁 on demand")
    elif text.startswith("/auto"):
        parts = text.split()
        state = load_state()
        start = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() \
                else state["current_page"]
        start = max(1, min(TOTAL_PAGES, start))
        start_auto_mode(chat_id, start)
    elif text.startswith("/stop"):
        if _auto_mode.is_set():
            _stop_auto.set()
            tg_send(chat_id, "🛑 Stop requested — will halt after current item.")
        else:
            tg_send(chat_id, "Nothing is running in auto mode.")
    elif text.startswith("/process"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            page = max(1, min(TOTAL_PAGES, int(parts[1])))
            start_page_processing(chat_id, page)
        else:
            state = load_state()
            start_page_processing(chat_id, state["current_page"])
    elif text.startswith("/goto"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            page = max(1, min(TOTAL_PAGES, int(parts[1])))
            state = load_state()
            state["current_page"] = page
            save_state(state)
            send_menu(chat_id)
    elif text.startswith("/help"):
        tg_send(chat_id,
                "Commands:\n"
                "/start — show menu\n"
                "/status — show stats\n"
                "/json — send vid65.json\n"
                "/process [N] — process page N (default: current)\n"
                "/auto [N] — start Auto Mode from page N (default: current)\n"
                "/stop — stop Auto Mode\n"
                "/goto N — jump to page N\n"
                "/help — this message")
    else:
        send_menu(chat_id)


def handle_callback(cb: dict) -> None:
    cb_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    data = cb.get("data", "")

    if chat_id != ADMIN_ID:
        tg_answer_cb(cb_id)
        return

    state = load_state()
    page = state["current_page"]

    if data == "noop":
        tg_answer_cb(cb_id)
        return

    if data == "nav:prev":
        if _auto_mode.is_set():
            tg_answer_cb(cb_id, "Auto mode running — stop first")
            return
        state["current_page"] = max(1, page - 1)
        save_state(state)
        edit_menu(chat_id, message_id)
        tg_answer_cb(cb_id, f"Page {state['current_page']}")

    elif data == "nav:next":
        if _auto_mode.is_set():
            tg_answer_cb(cb_id, "Auto mode running")
            return
        if page >= TOTAL_PAGES:
            tg_answer_cb(cb_id, "Already at last page")
            return
        new_page = page + 1
        state["current_page"] = new_page
        save_state(state)
        edit_menu(chat_id, message_id)
        tg_answer_cb(cb_id, f"Processing page {new_page}…")
        start_page_processing(chat_id, new_page)

    elif data == "action:process":
        if _auto_mode.is_set():
            tg_answer_cb(cb_id, "Auto mode running")
            return
        tg_answer_cb(cb_id, f"Processing page {page}…")
        start_page_processing(chat_id, page)

    elif data == "auto:start":
        if _auto_mode.is_set():
            tg_answer_cb(cb_id, "Already running")
            return
        tg_answer_cb(cb_id, f"Auto Mode from page {page}…")
        start_auto_mode(chat_id, page)
        edit_menu(chat_id, message_id)

    elif data == "auto:stop":
        if _auto_mode.is_set():
            _stop_auto.set()
            tg_answer_cb(cb_id, "Stopping after current item…")
        else:
            tg_answer_cb(cb_id, "Auto mode not running")

    elif data == "action:status":
        tg_answer_cb(cb_id, "Status sent")
        tg_send(chat_id, menu_text(load_state()))

    elif data == "action:json":
        tg_answer_cb(cb_id, "Sending file…")
        send_full_file(chat_id, caption_prefix="📁 on demand")

    else:
        tg_answer_cb(cb_id)

# ================== MAIN LOOP ==================
def bot_loop() -> None:
    log.info("Bot loop started — Ctrl+C to stop")
    offset = None
    while True:
        try:
            updates = tg_get_updates(offset, timeout=30)
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    if "message" in u:
                        handle_message(u["message"])
                    elif "callback_query" in u:
                        handle_callback(u["callback_query"])
                except Exception as e:
                    log.error(f"handler error: {e}", exc_info=True)
        except KeyboardInterrupt:
            log.info("Shutting down…")
            return
        except Exception as e:
            log.error(f"bot loop error: {e}")
            time.sleep(5)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is empty")
    if not ADMIN_ID:
        raise SystemExit("ADMIN_ID is empty")

    verify_token()

    if STORAGE_CHAT_ID:
        log.info(f"Storage chat: {STORAGE_CHAT_ID} (media won't touch admin chat)")
    elif DELETE_AFTER_UPLOAD:
        log.info("No storage chat set — media uploaded to admin chat then deleted")
    else:
        log.info("⚠️ Media will stay in admin chat (DELETE_AFTER_UPLOAD=False)")

    state = load_state()
    log.info(f"Loaded state: page={state['current_page']}  items={len(state['items'])}")
    log.info(f"State file: {os.path.abspath(STATE_FILE)}")

    tg_send(ADMIN_ID, "🚀 <b>vid65 bot online.</b>  Send /start to open the menu.")
    send_menu(ADMIN_ID)

    bot_loop()


if __name__ == "__main__":
    main()
