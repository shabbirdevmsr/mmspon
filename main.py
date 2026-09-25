"""
vid65 scraper — Railway deploy version.

Flow per item:
  download image + video → upload to local Bot API server → get file_ids
  → append to vid65.json → notify admin

Env vars (optional, defaults are baked in):
  BOT_TOKEN       default: 6757665465:AAHo-0Avmg36zGH5vmHt44Fhl-LehHwfzVw
  ADMIN_ID        default: 5087403859
  LOCAL_API       default: https://telegram-bot-api-production-29e4.up.railway.app
  CHAT_ID         default: ADMIN_ID  (upload target)
  START_PAGE      default: 1
  END_PAGE        default: 60
  PAGE_DELAY      default: 1.0
"""

import os
import io
import json
import time
import logging
from pathlib import Path
from typing import Iterator, Optional

import requests

# ---------------- CONFIG ----------------
BOT_TOKEN  = os.environ.get("BOT_TOKEN",
    "6757665465:AAHo-0Avmg36zGH5vmHt44Fhl-LehHwfzVw")
ADMIN_ID   = os.environ.get("ADMIN_ID", "5087403859")
LOCAL_API  = os.environ.get("LOCAL_API",
    "https://telegram-bot-api-production-29e4.up.railway.app")
CHAT_ID    = os.environ.get("CHAT_ID", ADMIN_ID)

START_PAGE = int(os.environ.get("START_PAGE", "1"))
END_PAGE   = int(os.environ.get("END_PAGE",   "60"))
PAGE_DELAY = float(os.environ.get("PAGE_DELAY", "1.0"))

API_URL      = "https://shabbir.serv00.net/sex/vid65/get.php"
JSON_PATH    = Path("vid65.json")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

HTTP_TIMEOUT   = 120
DOWNLOAD_RETRY = 3
UPLOAD_RETRY   = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; vid65-scraper/1.0)",
    "Accept": "application/json, */*",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vid65")


# ---------------- JSON STORAGE ----------------
def load_store() -> list:
    if JSON_PATH.exists():
        try:
            return json.loads(JSON_PATH.read_text(encoding="utf-8"))
        except Exception:
            log.warning("vid65.json is corrupt, starting fresh")
    return []


def save_store(rows: list) -> None:
    JSON_PATH.write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def already_done(rows: list, item_id: int) -> bool:
    return any(r.get("id") == item_id for r in rows)


# ---------------- API PAGINATION ----------------
def fetch_page(page: int) -> dict:
    r = requests.get(API_URL, params={"page": page},
                     headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def iter_items(start: int, end: int) -> Iterator[dict]:
    page = start
    while page is not None and page <= end:
        log.info(f"Fetching page {page} …")
        try:
            payload = fetch_page(page)
        except Exception as e:
            log.error(f"page {page} fetch failed: {e}")
            break

        if not payload.get("success"):
            log.warning(f"page {page}: success=false, stopping.")
            break

        for item in payload.get("data", []):
            yield item

        nxt = payload.get("pagination", {}).get("next_page")
        if nxt is None:
            break
        page = nxt
        time.sleep(PAGE_DELAY)


# ---------------- DOWNLOAD ----------------
def download_to_memory(url: str) -> bytes:
    last_err: Optional[Exception] = None
    for attempt in range(1, DOWNLOAD_RETRY + 1):
        try:
            with requests.get(url, stream=True,
                              headers=HEADERS, timeout=HTTP_TIMEOUT) as r:
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        buf.write(chunk)
                return buf.getvalue()
        except Exception as e:
            last_err = e
            log.warning(f"download attempt {attempt} failed: {e}")
            time.sleep(2 ** attempt)
    raise last_err  # type: ignore


# ---------------- TELEGRAM UPLOAD ----------------
def _bot_url(method: str) -> str:
    return f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"


def send_message(chat_id, text: str) -> None:
    try:
        requests.post(_bot_url("sendMessage"),
                      data={"chat_id": chat_id, "text": text[:4000],
                            "parse_mode": "HTML"},
                      timeout=30)
    except Exception as e:
        log.warning(f"sendMessage failed: {e}")


def upload_photo(image_bytes: bytes, filename: str, caption: str):
    for attempt in range(1, UPLOAD_RETRY + 1):
        try:
            files = {"photo": (filename, image_bytes)}
            data  = {"chat_id": CHAT_ID, "caption": caption[:1024]}
            r = requests.post(_bot_url("sendPhoto"),
                              files=files, data=data, timeout=None)
            r.raise_for_status()
            res = r.json()
            if not res.get("ok"):
                raise RuntimeError(res)
            return (res["result"]["photo"][-1]["file_id"],
                    res["result"]["message_id"])
        except Exception as e:
            log.warning(f"upload_photo attempt {attempt} failed: {e}")
            if attempt == UPLOAD_RETRY:
                raise
            time.sleep(2 ** attempt)


def upload_video(video_bytes: bytes, filename: str, caption: str):
    for attempt in range(1, UPLOAD_RETRY + 1):
        try:
            files = {"video": (filename, video_bytes)}
            data  = {"chat_id": CHAT_ID,
                     "caption": caption[:1024],
                     "supports_streaming": "true"}
            r = requests.post(_bot_url("sendVideo"),
                              files=files, data=data, timeout=None)
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
                raise RuntimeError(f"no video/document: {result}")
            return fid, result["message_id"]
        except Exception as e:
            log.warning(f"upload_video attempt {attempt} failed: {e}")
            if attempt == UPLOAD_RETRY:
                raise
            time.sleep(2 ** attempt)


# ---------------- PER ITEM ----------------
def process_item(item: dict) -> dict:
    item_id   = int(item["id"])
    name      = item["name"]
    image_url = item["image"]
    video_url = item["video"]

    img_name = Path(image_url.split("?")[0]).name or f"{item_id}.jpg"
    vid_name = Path(video_url.split("?")[0]).name or f"{item_id}.mp4"

    log.info(f"[{item_id}] downloading image …")
    image_bytes = download_to_memory(image_url)

    log.info(f"[{item_id}] downloading video …")
    video_bytes = download_to_memory(video_url)
    size_mb = len(video_bytes) / 1e6

    log.info(f"[{item_id}] uploading image …")
    image_id, image_msg = upload_photo(image_bytes, img_name, name)

    log.info(f"[{item_id}] uploading video ({size_mb:.1f} MB) …")
    video_id, video_msg = upload_video(video_bytes, vid_name, name)

    return {
        "id":           item_id,
        "name":         name,
        "image_id":     image_id,
        "video_id":     video_id,
        "image_msg_id": image_msg,
        "video_msg_id": video_msg,
        "chat_id":      CHAT_ID,
        "size_mb":      round(size_mb, 2),
        "create_time":  time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------- ENTRY ----------------
def main() -> None:
    log.info(f"Bot: {BOT_TOKEN.split(':')[0]} | Admin: {ADMIN_ID} | Chat: {CHAT_ID}")
    log.info(f"Pages {START_PAGE}..{END_PAGE}")

    send_message(ADMIN_ID, "🚀 <b>vid65 scraper started</b>")

    rows = load_store()
    done = {r["id"] for r in rows}
    log.info(f"Loaded {len(rows)} existing row(s)")

    processed = 0
    failed    = 0

    for item in iter_items(START_PAGE, END_PAGE):
        item_id = int(item["id"])
        if item_id in done:
            log.info(f"[skip] id={item_id} already done")
            continue

        try:
            row = process_item(item)
            rows.append(row)
            save_store(rows)              # save immediately after each item
            done.add(item_id)
            processed += 1

            notify = (
                f"✅ <b>New item saved</b>\n"
                f"<b>ID:</b> <code>{row['id']}</code>\n"
                f"<b>Name:</b> {row['name']}\n"
                f"<b>Size:</b> {row['size_mb']} MB\n"
                f"<b>image_id:</b> <code>{row['image_id']}</code>\n"
                f"<b>video_id:</b> <code>{row['video_id']}</code>\n"
                f"<b>Total done:</b> {len(rows)}"
            )
            send_message(ADMIN_ID, notify)
            log.info(f"[{item_id}] saved & notified")

        except Exception as e:
            failed += 1
            log.error(f"[{item_id}] failed: {e}", exc_info=True)
            send_message(ADMIN_ID,
                f"❌ <b>Item failed</b>\n"
                f"ID: <code>{item_id}</code>\n"
                f"Error: <code>{str(e)[:300]}</code>")

    send_message(ADMIN_ID,
        f"🏁 <b>Scraper finished</b>\n"
        f"Processed: {processed}\n"
        f"Failed: {failed}\n"
        f"Total rows: {len(rows)}")
    log.info(f"Done. processed={processed} failed={failed} total={len(rows)}")


if __name__ == "__main__":
    main()
