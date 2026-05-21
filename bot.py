import logging
import threading
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
)

import config
import handlers
from scheduler import setup_scheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def start_discord_in_background():
    if not config.DISCORD_TOKEN:
        logger.info("Discord listener отключён (нет DISCORD_TOKEN)")
        return

    from discord_listener import run_discord_listener

    def runner():
        try:
            run_discord_listener()
        except Exception as e:
            logger.exception(f"Discord listener crashed: {e}")

    t = threading.Thread(target=runner, daemon=True, name="discord-listener")
    t.start()
    logger.info("Discord listener запущен в фоне")


def main():
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not config.ACCESS_PASSWORD:
        logger.warning("⚠️ ACCESS_PASSWORD не задан — бот открыт для всех!")
    if not config.ADMIN_CHAT_IDS:
        logger.warning("⚠️ ADMIN_CHAT_IDS не задан — админ-команды недоступны")
    else:
        logger.info(f"Админов: {len(config.ADMIN_CHAT_IDS)}")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Пользовательские команды
    app.add_handler(CommandHandler("start",    handlers.cmd_start))
    app.add_handler(CommandHandler("add",      handlers.cmd_add))
    app.add_handler(CommandHandler("list",     handlers.cmd_list))
    app.add_handler(CommandHandler("delete",   handlers.cmd_delete))
    app.add_handler(CommandHandler("clear",    handlers.cmd_clear))
    app.add_handler(CommandHandler("settings", handlers.cmd_settings))

    # Админ-команды
    app.add_handler(CommandHandler("users",    handlers.cmd_users))
    app.add_handler(CommandHandler("ban",      handlers.cmd_ban))
    app.add_handler(CommandHandler("unban",    handlers.cmd_unban))
    app.add_handler(CommandHandler("remove",   handlers.cmd_remove))

    # Кнопки + текст
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))

    setup_scheduler(app)
    start_discord_in_background()

    logger.info("TG бот запущен")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
