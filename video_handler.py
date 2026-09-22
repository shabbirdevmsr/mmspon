import logging
from datetime import datetime, timezone

import requests
from telegram import Bot

log = logging.getLogger("video-handler")


def now():
    return datetime.now(timezone.utc)


def api_items(source, page):
    r = requests.get(source["base_url"], params={"page": page}, timeout=90)
    r.raise_for_status()
    data = r.json()
    return data.get("data") or [], data.get("pagination") or {}


def source_state(source):
    return source.get("state") or {
        "current_page": 1,
        "current_index": 1,
        "slug": "",
        "total_pages": 0,
        "total_items": 0,
        "has_next": True,
    }


def bot_api(token, api_server):
    return Bot(token=token, base_url=f"{api_server}/bot")


def bot_is_cancelled(jobs, job_id):
    j = jobs.find_one({"_id": job_id}, {"status": 1})
    return not j or j.get("status") == "cancelled"


def save_discovered(videos, bot_id, source, item, index):
    external_id = str(item.get("id") or item.get("slug") or item.get("video") or "")
    if not external_id:
        return None, False
    result = videos.update_one(
        {"bot_id": bot_id, "source_id": source["_id"], "external_id": external_id},
        {"$setOnInsert": {
            "bot_id": bot_id,
            "source_id": source["_id"],
            "external_id": external_id,
            "title": item.get("name") or item.get("title") or "Untitled",
            "slug": item.get("slug") or item.get("name") or "",
            "image_url": item.get("image") or "",
            "video_url": item.get("video") or item.get("downloadLink") or "",
            "status": "discovered",
            "created_at": now(),
        }},
        upsert=True,
    )
    return videos.find_one({"bot_id": bot_id, "source_id": source["_id"], "external_id": external_id}), bool(result.upserted_id)


def send_video_to_destinations(db, bot, video, destinations):
    sent = 0
    telegram = bot_api(bot["token"], db.api_server if hasattr(db, "api_server") else "https://api.telegram.org")
    # api_server is attached by process_job; normal PyMongo DB objects allow attributes only poorly,
    # so process_job passes a Bot instance instead in production path.
    for d in destinations:
        if not d.get("enabled", True) or not d.get("bot_is_admin", False):
            continue
        try:
            msg = telegram.send_video(
                chat_id=d["chat_id"],
                video=video["video_url"],
                caption=video.get("title", ""),
                supports_streaming=True,
            )
            db.channel_posts.insert_one({
                "bot_id": bot["_id"],
                "chat_id": str(d["chat_id"]),
                "message_id": msg.message_id,
                "video_id": video["_id"],
                "created_at": now(),
            })
            db.destinations.update_one({"_id": d["_id"]}, {"$set": {"last_post_id": msg.message_id, "updated_at": now()}})
            sent += 1
        except Exception as exc:
            log.exception("Upload failed for %s: %s", d.get("chat_id"), exc)
    return sent


def process_source(db, bot, source, job_id, api_server):
    sources, videos, jobs, destinations = db.sources, db.videos, db.jobs, db.destinations
    state = source_state(source)
    page = int(state.get("current_page", 1) or 1)
    index = int(state.get("current_index", 1) or 1)
    telegram = Bot(token=bot["token"], base_url=f"{api_server}/bot")

    while True:
        if bot_is_cancelled(jobs, job_id):
            return "cancelled"
        items, pagination = api_items(source, page)
        if not items:
            sources.update_one({"_id": source["_id"]}, {"$set": {"state.updated_at": now()}})
            return "completed"

        for item in items:
            if bot_is_cancelled(jobs, job_id):
                return "cancelled"
            current = save_discovered(videos, bot["_id"], source, item, index)
            video, is_new = current
            sources.update_one({"_id": source["_id"]}, {"$set": {
                "state.current_page": page,
                "state.current_index": index,
                "state.slug": item.get("slug") or item.get("name") or "",
                "state.total_pages": pagination.get("total_pages", 0),
                "state.total_items": pagination.get("total_items", 0),
                "state.has_next": bool(pagination.get("has_next")),
                "state.updated_at": now(),
            }})
            index += 1
            if is_new and video and video.get("video_url"):
                for d in destinations.find({"bot_id": bot["_id"], "enabled": True, "bot_is_admin": True}):
                    try:
                        msg = telegram.send_video(
                            chat_id=d["chat_id"],
                            video=video["video_url"],
                            caption=video.get("title", ""),
                            supports_streaming=True,
                        )
                        db.channel_posts.insert_one({
                            "bot_id": bot["_id"], "chat_id": str(d["chat_id"]),
                            "message_id": msg.message_id, "video_id": video["_id"], "created_at": now()
                        })
                        destinations.update_one({"_id": d["_id"]}, {"$set": {"last_post_id": msg.message_id, "updated_at": now()}})
                    except Exception as exc:
                        log.exception("Could not send video to %s: %s", d.get("chat_id"), exc)

        next_page = pagination.get("next_page")
        sources.update_one({"_id": source["_id"]}, {"$set": {
            "state.current_page": page,
            "state.current_index": index,
            "state.slug": pagination.get("slug") or state.get("slug", ""),
            "state.total_pages": pagination.get("total_pages", 0),
            "state.total_items": pagination.get("total_items", 0),
            "state.has_next": bool(pagination.get("has_next")),
            "state.updated_at": now(),
        }})
        if not pagination.get("has_next"):
            return "completed"
        page = int(next_page or page + 1)


def process_job(db, job, api_server):
    jobs, bots, sources = db.jobs, db.bots, db.sources
    jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "running", "started_at": now()}})
    try:
        bot = bots.find_one({"_id": job["bot_id"], "enabled": True})
        if not bot:
            raise RuntimeError("Bot not found or disabled")
        for sid in job.get("source_ids", []):
            source = sources.find_one({"_id": sid, "enabled": True})
            if source:
                status = process_source(db, bot, source, job["_id"], api_server)
                if status == "cancelled":
                    jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "cancelled", "finished_at": now()}})
                    return
        jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "completed", "finished_at": now()}})
    except Exception as exc:
        log.exception("Job failed")
        jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "failed", "error": str(exc), "finished_at": now()}})
