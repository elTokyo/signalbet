"""
Discord-бот: слушает текстовый канал, парсит сообщения как прогнозы
и сохраняет в общую БД (storage.py).

Три источника прогнозов:
1. on_message      — новое сообщение (мгновенно)
2. on_message_edit — редактирование сообщения (мгновенно)
3. periodic_recheck — перечит последних сообщений раз в 15 минут (страховка)

Дубли отсекаются автоматически в storage.add_predictions (по тексту).
"""
import asyncio
import logging
import discord
import aiohttp

import storage
import config
from parser import parse_predictions, format_time_local

logger = logging.getLogger(__name__)

RECHECK_INTERVAL_SEC = 15 * 60   # перечит раз в 15 минут
RECHECK_HISTORY_LIMIT = 15       # сколько последних сообщений перечитывать


def run_discord_listener():
    if not config.DISCORD_TOKEN or not config.DISCORD_CHANNEL_ID:
        logger.warning("Discord не настроен (нет DISCORD_TOKEN / DISCORD_CHANNEL_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)

    # ── Общая обработка текста сообщения ─────────────────────────────────────
    async def process_message_text(content: str, origin: str):
        """Парсит текст, добавляет новые прогнозы, рассылает уведомление."""
        preds = parse_predictions(content, config.DEFAULT_TZ_OFFSET, source="discord")
        if not preds:
            return

        added = storage.add_predictions(new_preds=preds)
        if added <= 0:
            return  # все прогнозы уже были (дубли)

        logger.info(f"Discord [{origin}]: добавлено {added} новых прогнозов")

        recipients = storage.get_all_recipient_chat_ids()
        if not recipients:
            return

        try:
            async with aiohttp.ClientSession() as session:
                for chat_id in recipients:
                    s = storage.load_settings(chat_id)
                    lines = [f"🤖 Из Discord: +{added} прогнозов"]
                    for p in preds[-added:]:
                        t = format_time_local(p, s.timezone_offset)
                        lines.append(f"⏰ {t}  {p.text}")
                    try:
                        await session.post(
                            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "\n".join(lines)},
                        )
                    except Exception as e:
                        logger.error(f"TG send to {chat_id} failed: {e}")
        except Exception as e:
            logger.error(f"Discord broadcast error: {e}")

    # ── Периодический перечит последних сообщений ────────────────────────────
    async def periodic_recheck():
        await client.wait_until_ready()
        ch = client.get_channel(config.DISCORD_CHANNEL_ID)
        if not ch:
            logger.error("Periodic recheck: канал не найден")
            return

        while not client.is_closed():
            await asyncio.sleep(RECHECK_INTERVAL_SEC)
            try:
                count = 0
                async for msg in ch.history(limit=RECHECK_HISTORY_LIMIT):
                    if msg.author == client.user:
                        continue
                    await process_message_text(msg.content, "recheck")
                    count += 1
                logger.info(f"Discord periodic recheck: проверено {count} сообщений")
            except Exception as e:
                logger.error(f"Periodic recheck error: {e}")

    # ── События ──────────────────────────────────────────────────────────────
    @client.event
    async def on_ready():
        logger.info(f"Discord listener запущен: {client.user}")
        ch = client.get_channel(config.DISCORD_CHANNEL_ID)
        if ch:
            logger.info(f"Слушаю канал: #{ch.name}")
        else:
            logger.error(f"Канал {config.DISCORD_CHANNEL_ID} не найден")
        # Запускаем фоновый перечит
        client.loop.create_task(periodic_recheck())

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if message.channel.id != config.DISCORD_CHANNEL_ID:
            return
        await process_message_text(message.content, "new")

    @client.event
    async def on_message_edit(before: discord.Message, after: discord.Message):
        if after.author == client.user:
            return
        if after.channel.id != config.DISCORD_CHANNEL_ID:
            return
        # Текст не изменился — игнорируем (могло быть изменение эмбеда/реакции)
        if before.content == after.content:
            return
        logger.info("Discord: обнаружено редактирование сообщения")
        await process_message_text(after.content, "edit")

    client.run(config.DISCORD_TOKEN, log_handler=None)
