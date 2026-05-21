"""
Discord-бот: слушает один текстовый канал, парсит каждое сообщение как прогноз
и сохраняет в общую БД (storage.py). Уведомления отправляет основной TG-бот.
"""
import logging
import discord

import storage
import config
from parser import parse_predictions, format_time_local

logger = logging.getLogger(__name__)


def run_discord_listener():
    if not config.DISCORD_TOKEN or not config.DISCORD_CHANNEL_ID or not config.DISCORD_TARGET_TG_CHAT_ID:
        logger.warning("Discord не настроен (нет DISCORD_TOKEN / DISCORD_CHANNEL_ID / DISCORD_TARGET_TG_CHAT_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True   # обязательное право для чтения текста

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info(f"Discord listener запущен: {client.user}")
        ch = client.get_channel(config.DISCORD_CHANNEL_ID)
        if ch:
            logger.info(f"Слушаю канал: #{ch.name}")
        else:
            logger.error(f"Канал {config.DISCORD_CHANNEL_ID} не найден")

    @client.event
    async def on_message(message: discord.Message):
        # Игнорируем себя, чужие каналы, ботов
        if message.author == client.user:
            return
        if message.channel.id != config.DISCORD_CHANNEL_ID:
            return

        target_chat = config.DISCORD_TARGET_TG_CHAT_ID
        s = storage.load_settings(target_chat)

        # Парсим сообщение целиком как один или несколько прогнозов
        preds = parse_predictions(message.content, s.timezone_offset, source="discord")
        if not preds:
            logger.debug(f"Discord: сообщение без времени матча, пропущено")
            return

        added = storage.add_predictions(target_chat, preds)
        logger.info(f"Discord → TG {target_chat}: добавлено {added} прогнозов")

        # Подтверждение в TG
        if added > 0:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    lines = [f"🤖 *Из Discord: +{added} прогнозов*"]
                    for p in preds[-added:]:
                        t = format_time_local(p, s.timezone_offset)
                        lines.append(f"⏰ {t}  {p.text}")
                    await session.post(
                        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": target_chat,
                            "text": "\n".join(lines),
                            "parse_mode": "Markdown",
                        },
                    )
            except Exception as e:
                logger.error(f"TG confirmation error: {e}")

    client.run(config.DISCORD_TOKEN, log_handler=None)
