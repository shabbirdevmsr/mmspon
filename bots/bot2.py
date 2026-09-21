# ==========================================================
# bots/bot2.py — Second Bot (edit token + admin_id)
# ==========================================================

BOT = {
    "id": "bot2",
    "name": "Second Bot",
    "token": "PUT-YOUR-SECOND-BOT-TOKEN-HERE",
    "admin_id": 5087403859,
    "enabled": True,

    "apis": [
        {
            "slug": "vid65",
            "name": "vid65 (id, up)",
            "base_url": "https://shabbir.serv00.net/sex/vid65/get.php",
            "home_url_template":  "{base_url}?page={page}",
            "video_url_template": "{base_url}?action=video&id={id}",
            "slug_field": "id",
            "title_field": "name",
            "thumb_field": "image",
            "page_direction": "up",
            "current_page": 1,
            "max_page": 60,
            "mode": "ON",
        },
    ],

    "targets": [],
    "welcome": {},
}
