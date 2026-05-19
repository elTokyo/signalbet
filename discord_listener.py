"""
Discord-бот: слушает один текстовый канал, парсит сообщения как прогнозы
и сохраняет в общую БД (storage.py). При новых прогнозах рассылает
подтверждение всем авторизованным пользователям TG.
"""
import logging
import discord
import aiohttp

import storage
import config
from parser import parse_predictions, format_time_local

logger = logging.getLogger(__name__)


def run_discord_listener():
    if not config.DISCORD_TOKEN or not config.DISCORD_CHANNEL_ID:
        logger.warning("Discord не настроен (нет DISCORD_TOKEN / DISCORD_CHANNEL_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True

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
        if message.author == client.user:
            return
        if message.channel.id != config.DISCORD_CHANNEL_ID:
            return

        # Парсим с дефолтным таймзоном — у каждого юзера потом сконвертируется при отображении
        preds = parse_predictions(message.content, config.DEFAULT_TZ_OFFSET, source="discord")
        if not preds:
            logger.debug("Discord: сообщение без времени матча, пропущено")
            return

        added = storage.add_predictions(new_preds=preds)
        logger.info(f"Discord → общий список: добавлено {added} прогнозов")

        if added <= 0:
            return

        # Broadcast уведомление всем авторизованным пользователям
        recipients = storage.get_all_recipient_chat_ids()
        if not recipients:
            return

        try:
            async with aiohttp.ClientSession() as session:
                for chat_id in recipients:
                    # У каждого получателя свой timezone
                    s = storage.load_settings(chat_id)
                    lines = [f"🤖 Из Discord: +{added} прогнозов"]
                    for p in preds[-added:]:
                        t = format_time_local(p, s.timezone_offset)
                        lines.append(f"⏰ {t}  {p.text}")
                    try:
                        await session.post(
                            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": "\n".join(lines),
                            },
                        )
                    except Exception as e:
                        logger.error(f"TG send to {chat_id} failed: {e}")
        except Exception as e:
            logger.error(f"Discord broadcast error: {e}")

    client.run(config.DISCORD_TOKEN, log_handler=None)
