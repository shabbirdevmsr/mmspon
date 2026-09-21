# ==========================================================
# bots/bot1.py — Main Bot
#   Add a new bot: copy this file to bots/botN.py and edit.
# ==========================================================

BOT = {
    "id": "bot1",
    "name": "Main Bot",
    "token": "6984914369:AAGeBDtPE6P001ilBTSedKhxpVFfUPurXh0",
    "admin_id": 5087403859,
    "enabled": True,

    "apis": [
        {
            "slug": "mmsbaba",
            "name": "mmsbaba (slug, down)",
            "base_url": "https://shabbir.serv00.net/sex/mmsbaba/get.php",
            "home_url_template":  "{base_url}?action=home&page={page}",
            "video_url_template": "{base_url}?action=video&id={id}",
            "slug_field": "slug",
            "title_field": "title",
            "thumb_field": "thumbnail",
            "page_direction": "down",
            "current_page": 203,
            "max_page": 300,
            "mode": "ON",
        },
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

    "targets": [
        # {"api": "mmsbaba", "type": "channel", "chat_id": "@virulvideopompom"},
        # {"api": "mmsbaba", "type": "group",   "chat_id": -1003708527420},
    ],

    "welcome": {
        # "-1001234567890": "👋 Welcome {mention} to <b>{chat}</b>!",
    },
}
