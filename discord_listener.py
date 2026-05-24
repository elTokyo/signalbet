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
from datetime import datetime, timezone, timedelta
import discord
import aiohttp

import storage
import config
from parser import parse_predictions, format_time_local

logger = logging.getLogger(__name__)

RECHECK_INTERVAL_SEC = 15 * 60   # перечит раз в 15 минут
RECHECK_HISTORY_LIMIT = 15       # сколько последних сообщений перечитывать

# Ссылки на работающий Discord-клиент и его event loop —
# нужны чтобы вызвать перечит из другого потока (Telegram-команда /syncdiscord)
_client_ref: discord.Client | None = None
_loop_ref = None


# Окно «свежести» Discord-сообщения. Прогнозы на сегодняшние матчи могут
# публиковаться накануне вечером, поэтому фильтр по календарной дате слишком жёсткий.
# Берём сообщения за последние N часов — покрывает вечерние публикации накануне,
# но отсекает действительно старые. Доп. защита от сыгранных матчей — в парсере.
MESSAGE_FRESHNESS_HOURS = 18


def _is_fresh(message: discord.Message) -> bool:
    """
    True если сообщение создано ИЛИ отредактировано за последние MESSAGE_FRESHNESS_HOURS часов.
    Это покрывает прогнозы запощенные накануне вечером на сегодняшние матчи.
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=MESSAGE_FRESHNESS_HOURS)

    if message.created_at >= threshold:
        return True
    if message.edited_at is not None and message.edited_at >= threshold:
        return True
    return False


def trigger_manual_recheck() -> tuple[bool, str]:
    """
    Вызывается из Telegram-потока (команда /syncdiscord).
    Планирует перечит канала в Discord event loop.
    Возвращает (успех, сообщение).
    """
    if _client_ref is None or _loop_ref is None:
        return False, "Discord-бот не запущен (проверь DISCORD_TOKEN)"

    if _client_ref.is_closed():
        return False, "Discord-соединение закрыто"

    try:
        # Планируем корутину в loop Discord-потока
        future = asyncio.run_coroutine_threadsafe(_manual_recheck(), _loop_ref)
        count = future.result(timeout=30)   # ждём результат до 30 сек
        return True, f"Проверено сообщений: {count}"
    except Exception as e:
        logger.error(f"Manual recheck error: {e}")
        return False, f"Ошибка: {e}"


async def _manual_recheck() -> int:
    """Перечитывает последние сообщения канала. Возвращает кол-во проверенных."""
    ch = _client_ref.get_channel(config.DISCORD_CHANNEL_ID)
    if not ch:
        raise RuntimeError("канал не найден")

    count = 0
    async for msg in ch.history(limit=RECHECK_HISTORY_LIMIT):
        if msg.author == _client_ref.user:
            continue
        if not _is_fresh(msg):
            continue  # пропускаем вчерашние неубранные прогнозы
        await _process_static(msg.content, "manual")
        count += 1
    return count


async def _process_static(content: str, origin: str):
    """Версия process_message_text доступная вне замыкания (для manual recheck)."""
    preds = parse_predictions(content, config.DEFAULT_TZ_OFFSET, source="discord")
    if not preds:
        return
    added = storage.add_predictions(new_preds=preds)
    if added <= 0:
        return
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


def run_discord_listener():
    if not config.DISCORD_TOKEN or not config.DISCORD_CHANNEL_ID:
        logger.warning("Discord не настроен (нет DISCORD_TOKEN / DISCORD_CHANNEL_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)

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
                    if not _is_fresh(msg):
                        continue
                    await _process_static(msg.content, "recheck")
                    count += 1
                logger.info(f"Discord periodic recheck: проверено {count} сообщений")
            except Exception as e:
                logger.error(f"Periodic recheck error: {e}")

    # ── События ──────────────────────────────────────────────────────────────
    @client.event
    async def on_ready():
        global _client_ref, _loop_ref
        _client_ref = client
        _loop_ref = client.loop
        logger.info(f"Discord listener запущен: {client.user}")
        ch = client.get_channel(config.DISCORD_CHANNEL_ID)
        if ch:
            logger.info(f"Слушаю канал: #{ch.name}")
        else:
            logger.error(f"Канал {config.DISCORD_CHANNEL_ID} не найден")
        client.loop.create_task(periodic_recheck())

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if message.channel.id != config.DISCORD_CHANNEL_ID:
            return
        if not _is_fresh(message):
            return
        await _process_static(message.content, "new")

    @client.event
    async def on_message_edit(before: discord.Message, after: discord.Message):
        if after.author == client.user:
            return
        if after.channel.id != config.DISCORD_CHANNEL_ID:
            return
        if before.content == after.content:
            return
        logger.info("Discord: обнаружено редактирование сообщения")
        await _process_static(after.content, "edit")

    client.run(config.DISCORD_TOKEN, log_handler=None)
