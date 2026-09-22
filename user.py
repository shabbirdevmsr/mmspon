from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler

from button import main_keyboard


def now():
    return datetime.now(timezone.utc)


def register_user_handlers(application, bot_id, db):
    users = db.users

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        u = update.effective_user
        users.update_one(
            {"bot_id": bot_id, "telegram_id": u.id},
            {"$set": {
                "username": u.username or "",
                "first_name": u.first_name or "",
                "last_name": u.last_name or "",
                "last_seen": now(),
            }, "$setOnInsert": {"created_at": now()}},
            upsert=True,
        )
        await update.effective_message.reply_text(
            f"Welcome {u.first_name or 'there'}!\nChoose an option:",
            reply_markup=main_keyboard(),
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(
            "Use /start to open the bot menu.",
            reply_markup=main_keyboard(),
        )

    async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.data == "home":
            await query.edit_message_text("Main menu", reply_markup=main_keyboard())
        elif query.data == "status":
            await query.edit_message_text("Bot is online.\nYour account is registered.", reply_markup=main_keyboard())
        elif query.data == "help":
            await query.edit_message_text("Use the buttons or /start.", reply_markup=main_keyboard())

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(buttons))
