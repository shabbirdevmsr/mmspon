"""
vid65 scraper — local PC, single file.

Downloads image + video from the API, uploads both to the local
Telegram Bot API server, saves the returned file_ids to vid65.json.

Handles 403 Forbidden from vidserv.cc / upserv.xyz via:
  - curl_cffi with Chrome TLS impersonation (bypasses Cloudflare fingerprinting)
  - realistic browser headers (User-Agent, Referer, Accept-Language, etc.)
  - retry with exponential backoff + jitter
  - session/cookie handling

Features:
  - 10 concurrent downloads (BATCH_SIZE)
  - resumable: reads vid65.json, skips done ids
  - atomic JSON save after every item
  - no admin notifications — console logs only

Run:
    pip install curl_cffi
    python scraper.py
"""

import os
import io
import json
import time
import signal
import random
import logging
import threading
from pathlib import Path
from typing import Iterator, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

# ---------------- CONFIG ----------------
BOT_TOKEN  = "6757665465:AAFHhZ6KjY0B62WpiedvVXRJPxAVLjinC6E"
CHAT_ID    = "5087403859"
LOCAL_API  = "https://telegram-bot-api-production-29e4.up.railway.app"
API_URL    = "https://shabbir.serv00.net/sex/vid65/get.php"

START_PAGE = 1
END_PAGE   = 60
PAGE_DELAY = 1.0
BATCH_SIZE = 10
DOWNLOAD_RETRY = 5
UPLOAD_RETRY   = 3
HTTP_TIMEOUT   = 120

# Impersonation profile — chrome124 works well for Cloudflare in 2026
IMPERSONATE = "chrome124"

JSON_PATH    = Path("vid65.json")
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Realistic browser headers — Cloudflare checks these first
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Referer is critical — Cloudflare rejects requests with no referer
# Use the domain of the CDN itself as a safe default
REFERER_MAP = {
    "vidserv.cc": "https://vidserv.cc/",
    "upserv.xyz": "https://upserv.xyz/",
    "desitube.net": "https://desitube.net/",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vid65")

STOP = threading.Event()
_store_lock = threading.Lock()


def _on_sigint(signum, frame):
    log.warning("Ctrl+C — finishing current batch, then saving …")
    STOP.set()


signal.signal(signal.SIGINT, _on_sigint)


# ---------------- SESSION FACTORY ----------------
def make_session() -> curl_requests.Session:
    """Create a curl_cffi session with Chrome impersonation + browser headers."""
    session = curl_requests.Session(impersonate=IMPERSONATE)
    session.headers.update(BROWSER_HEADERS)
    return session


# ---------------- JSON STORE ----------------
def load_store() -> list:
    if JSON_PATH.exists():
        try:
            data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            log.warning(f"{JSON_PATH} unreadable ({e}), starting fresh")
    return []


def save_store(rows: list) -> None:
    with _store_lock:
        tmp = JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(JSON_PATH)


def append_row(row: dict) -> None:
    rows = load_store()
    rows.append(row)
    save_store(rows)


# ---------------- TELEGRAM HELPERS ----------------
def _bot_url(method: str) -> str:
    return f"{LOCAL_API}/bot{BOT_TOKEN}/{method}"


def verify_token() -> None:
    try:
        r = curl_requests.get(_bot_url("getMe"), timeout=15)
    except Exception as e:
        raise SystemExit(f"Can't reach local Bot API server: {e}")
    try:
        body = r.json()
    except Exception:
        raise SystemExit(f"Non-JSON reply: {r.text[:300]}")
    if r.status_code != 200 or not body.get("ok"):
        raise SystemExit(f"Token rejected: HTTP {r.status_code} — {r.text[:300]}")
    me = body["result"]
    log.info(f"Token OK — bot @{me.get('username')} (id={me.get('id')})")


# ---------------- API PAGINATION ----------------
def fetch_page(page: int) -> dict:
    """Fetch a page of items from the source API (no Cloudflare there)."""
    r = curl_requests.get(
        API_URL, params={"page": page},
        headers=BROWSER_HEADERS,
        timeout=HTTP_TIMEOUT,
        impersonate=IMPERSONATE,
    )
    r.raise_for_status()
    return r.json()


def iter_pages(start: int, end: int) -> Iterator[tuple[int, list]]:
    page = start
    while page is not None and page <= end and not STOP.is_set():
        log.info(f"→ Fetching page {page} …")
        try:
            payload = fetch_page(page)
        except Exception as e:
            log.error(f"page {page} fetch failed: {e}")
            break
        if not payload.get("success"):
            log.warning(f"page {page}: success=false, stopping.")
            break
        yield page, payload.get("data", []) or []
        nxt = payload.get("pagination", {}).get("next_page")
        if nxt is None:
            break
        page = nxt
        time.sleep(PAGE_DELAY)


# ---------------- DOWNLOAD (with 403 handling) ----------------
def _referer_for(url: str) -> str:
    """Pick a sensible Referer based on the CDN domain."""
    for domain, ref in REFERER_MAP.items():
        if domain in url:
            return ref
    return "https://www.google.com/"


def download_to_memory(url: str) -> bytes:
    """
    Download a URL into memory with:
      - Chrome TLS impersonation (bypasses Cloudflare fingerprint checks)
      - realistic browser headers
      - per-domain Referer
      - retry with exponential backoff + jitter
    """
    referer = _referer_for(url)
    last: Optional[Exception] = None

    for attempt in range(1, DOWNLOAD_RETRY + 1):
        try:
            session = make_session()
            session.headers.update({"Referer": referer})

            with session.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
                r.raise_for_status()
                buf = io.BytesIO()
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        buf.write(chunk)
                return buf.getvalue()

        except Exception as e:
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning(
                f"download attempt {attempt}/{DOWNLOAD_RETRY} failed "
                f"[{status or '?'}] {url.split('/')[-1]}: {e}"
            )

            # On 403: wait longer (Cloudflare rate-limits, needs cooldown)
            if status == 403:
                wait = (5 * attempt) + random.uniform(1, 3)
            # On 429: respect rate limit
            elif status == 429:
                wait = (10 * attempt) + random.uniform(2, 5)
            else:
                wait = (2 ** attempt) + random.uniform(0.5, 1.5)

            if attempt < DOWNLOAD_RETRY:
                log.info(f"   ↻ retrying in {wait:.1f}s …")
                time.sleep(wait)

    raise last  # type: ignore


# ---------------- UPLOAD ----------------
def upload_photo(image_bytes: bytes, filename: str, caption: str):
    for attempt in range(1, UPLOAD_RETRY + 1):
        try:
            files = {"photo": (filename, image_bytes)}
            data  = {"chat_id": CHAT_ID, "caption": caption[:1024]}
            r = curl_requests.post(_bot_url("sendPhoto"),
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
            r = curl_requests.post(_bot_url("sendVideo"),
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

    image_bytes = download_to_memory(image_url)
    video_bytes = download_to_memory(video_url)
    size_mb = len(video_bytes) / 1e6

    image_id, image_msg = upload_photo(image_bytes, img_name, name)
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


# ---------------- PER PAGE (batch of 10) ----------------
def process_page(page: int, items: list, done_ids: set):
    done = skipped = failed = 0
    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
        futures = {}
        for item in items:
            iid = int(item["id"])
            if iid in done_ids:
                skipped += 1
                continue
            futures[pool.submit(process_item, item)] = iid

        for fut in as_completed(futures):
            iid = futures[fut]
            try:
                row = fut.result()
                append_row(row)
                done_ids.add(iid)
                done += 1
                log.info(
                    f"   ✓ id={iid}  vid={row['size_mb']}MB  "
                    f"img_id={row['image_id'][:14]}…  "
                    f"vid_id={row['video_id'][:14]}…"
                )
            except Exception as e:
                failed += 1
                log.error(f"   ✗ id={iid} failed: {e}")
    return done, skipped, failed


# ---------------- MAIN ----------------
def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set BOT_TOKEN and CHAT_ID at the top of the file.")

    verify_token()
    log.info(f"Source: {API_URL}")
    log.info(f"Pages:  {START_PAGE}..{END_PAGE}  |  Batch: {BATCH_SIZE}")
    log.info(f"Store:  {JSON_PATH.resolve()}")

    rows = load_store()
    done_ids = {int(r["id"]) for r in rows if "id" in r}
    log.info(f"Resume: {len(done_ids)} item(s) already done — will skip those")

    total_done = total_skip = total_fail = 0
    pages_processed = 0

    for page, items in iter_pages(START_PAGE, END_PAGE):
        if STOP.is_set():
            break
        log.info(f"▶ Page {page}: {len(items)} item(s) — batch={BATCH_SIZE}")
        d, s, f = process_page(page, items, done_ids)
        pages_processed += 1
        total_done += d
        total_skip += s
        total_fail += f
        log.info(
            f"◀ Page {page} done — downloaded={d}  skipped={s}  failed={f}  "
            f"| total saved: {len(done_ids)}  failed so far: {total_fail}"
        )

    log.info("=" * 60)
    log.info(f"Finished. pages={pages_processed}  "
             f"downloaded={total_done}  skipped={total_skip}  failed={total_fail}")
    log.info(f"Total in {JSON_PATH.name}: {len(load_store())}")
    if STOP.is_set():
        log.info("(Stopped early — rerun to resume)")


if __name__ == "__main__":
    main()
