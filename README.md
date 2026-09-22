# Telegram Bot Manager — Modular Version

## Structure

- `main.py` — starts MongoDB, job worker and all enabled Telegram bots.
- `user.py` — `/start`, `/help`, user registration and callback handling.
- `button.py` — Telegram inline keyboards/buttons.
- `video_handler.py` — API pagination, index/slug state, duplicate detection and Telegram video posting.
- `admin.py` — local web admin panel; connects directly to MongoDB and never calls `main.py`.

## Bot library

The runtime uses `python-telegram-bot`. The custom `TG_API_SERVER` is used as the Bot API base server.

## Run

```bash
pip install -r requirements.txt
python admin.py
```

For Railway worker:

```bash
python main.py
```

Keep the existing MongoDB credentials in `.env`. Do not commit `.env` to Git.

The admin panel automatically creates the two configured API sources if they are missing. Bots and channels are managed from the panel. Only channels where the bot is an administrator can be added.
