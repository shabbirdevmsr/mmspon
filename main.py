import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from telegram import Bot
from telegram.ext import ApplicationBuilder

from user import register_user_handlers
from video_handler import process_job

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("telegram-manager")


def env(key, default=""):
    return os.getenv(key, default)


MONGO_USER = env("MONGO_USER")
MONGO_PASS = env("MONGO_PASS")
MONGO_HOST = env("MONGO_HOST", "localhost")
MONGO_PORT = int(env("MONGO_PORT", "27017"))
MONGO_DB = env("MONGO_DB", "mo8022_bachelor")
TG_API_SERVER = env("TG_API_SERVER", "https://api.telegram.org").rstrip("/")

MONGO_URI = (
    f"mongodb://{quote_plus(MONGO_USER)}:{quote_plus(MONGO_PASS)}"
    f"@{MONGO_HOST}:{MONGO_PORT}/?authSource={quote_plus(MONGO_DB)}"
)
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
db = mongo[MONGO_DB]

bots = db.bots
sources = db.sources
users = db.users
jobs = db.jobs
videos = db.videos
channel_posts = db.channel_posts

auto_indexes = [
    (jobs, [("status", ASCENDING), ("created_at", ASCENDING)]),
    (videos, [("bot_id", ASCENDING), ("source_id", ASCENDING), ("external_id", ASCENDING)]),
    (users, [("bot_id", ASCENDING), ("telegram_id", ASCENDING)]),
]
for collection, spec in auto_indexes:
    try:
        collection.create_index(spec)
    except Exception:
        pass


def now():
    return datetime.now(timezone.utc)


def run_bot(bot_doc):
    """Run one Telegram bot using python-telegram-bot."""
    token = bot_doc["token"]
    bot_id = str(bot_doc["_id"])
    try:
        # TG_API_SERVER is expected to be the root of a Telegram Bot API server.
        app = (
            ApplicationBuilder()
            .token(token)
            .base_url(f"{TG_API_SERVER}/bot")
            .build()
        )
        register_user_handlers(app, bot_id, db)
        log.info("Starting Telegram bot @%s", bot_doc.get("username", bot_id))
        app.run_polling(drop_pending_updates=False, allowed_updates=["message", "callback_query"])
    except Exception:
        log.exception("Bot %s stopped", bot_id)


def bot_supervisor():
    """Refresh enabled bots periodically so adding a bot in admin does not require code changes."""
    started = set()
    while True:
        try:
            for b in bots.find({"enabled": True}):
                bid = str(b["_id"])
                if bid in started:
                    continue
                started.add(bid)
                threading.Thread(target=run_bot, args=(b,), daemon=True, name=f"bot-{bid}").start()
        except Exception:
            log.exception("Bot supervisor error")
        time.sleep(int(env("BOT_RELOAD_INTERVAL", "15")))


def job_worker():
    """Process queued API jobs. The video handler handles API state + Telegram uploads."""
    while True:
        try:
            job = jobs.find_one_and_update(
                {"status": "queued"},
                {"$set": {"status": "claimed", "claimed_at": now()}},
                sort=[("created_at", ASCENDING)],
            )
            if not job:
                time.sleep(2)
                continue
            process_job(db, job, TG_API_SERVER)
        except Exception:
            log.exception("Job worker error")
            time.sleep(2)


def main():
    mongo.admin.command("ping")
    log.info("MongoDB connected: %s", MONGO_DB)
    threading.Thread(target=job_worker, daemon=True, name="job-worker").start()
    bot_supervisor()


if __name__ == "__main__":
    main()
