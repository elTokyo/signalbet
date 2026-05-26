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
RECHECK_HISTORY_LIMIT = 50       # читаем больше сообщений чтобы охватить все прогнозы дня

# Ссылки на работающий Discord-клиент и его event loop —
# нужны чтобы вызвать перечит из другого потока (Telegram-команда /syncdiscord)
_client_ref: discord.Client | None = None
_loop_ref = None


# Свежесть сообщения: обрабатываем только сообщения за сегодня (по локальной дате).
# Защита от повторного добавления сыгранных матчей — в парсере (он игнорирует
# уже начавшиеся discord-матчи). Это убирает повторный спам старыми прогнозами.


def _is_fresh(message: discord.Message) -> bool:
    """
    True если сообщение создано или отредактировано СЕГОДНЯ (по локальной дате).
    Вчерашние неубранные сообщения игнорируются.
    """
    tz = timezone(timedelta(hours=config.DEFAULT_TZ_OFFSET))
    today = datetime.now(tz).date()

    if message.created_at.astimezone(tz).date() == today:
        return True
    if message.edited_at is not None and message.edited_at.astimezone(tz).date() == today:
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
        stats = future.result(timeout=30)   # ждём результат до 30 сек
        info = (
            f"Сообщений: {stats['messages']}\n"
            f"➕ Добавлено: {stats['added']}\n"
            f"✏️ Обновлено: {stats['updated']}\n"
            f"🗑 Удалено: {stats['removed']}"
        )
        return True, info
    except Exception as e:
        logger.error(f"Manual recheck error: {e}")
        return False, f"Ошибка: {e}"


async def _manual_recheck(label: str = "автопроверка") -> dict:
    """
    Полная синхронизация листа со свежими сообщениями Discord.
    label — пометка источника для уведомления о добавленных прогнозах.
    """
    ch = _client_ref.get_channel(config.DISCORD_CHANNEL_ID)
    if not ch:
        raise RuntimeError("канал не найден")

    # Собираем ВСЕ прогнозы из свежих сообщений в один список
    all_preds = []
    msg_count = 0
    async for msg in ch.history(limit=RECHECK_HISTORY_LIMIT):
        if msg.author == _client_ref.user:
            continue
        if not _is_fresh(msg):
            continue
        preds = parse_predictions(msg.content, config.DEFAULT_TZ_OFFSET, source="discord")
        all_preds.extend(preds)
        msg_count += 1

    # Синхронизируем: добавить новые, обновить изменённые, удалить пропавшие
    stats = storage.sync_from_discord(all_preds)

    # Уведомляем о добавленных прогнозах с пометкой источника
    added_preds = stats.get("added_preds", [])
    if added_preds:
        await _notify_added(added_preds, label)

    return {"messages": msg_count, **stats}


async def _notify_added(preds: list, label: str):
    """Шлёт уведомление о добавленных прогнозах с пометкой источника (в скобках)."""
    if not preds:
        return
    all_recipients = storage.get_all_recipient_chat_ids()
    # Только те у кого включены уведомления о новых прогнозах
    recipients = [
        rid for rid in all_recipients
        if getattr(storage.load_settings(rid), "notify_new_preds", True)
    ]
    if not recipients:
        return
    try:
        async with aiohttp.ClientSession() as session:
            for chat_id in recipients:
                s = storage.load_settings(chat_id)
                lines = [f"🤖 Из Discord: +{len(preds)} прогнозов ({label})"]
                for p in preds:
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


async def _process_static(content: str, origin: str, label: str = "новое сообщение"):
    """Обработка одного нового сообщения (on_message)."""
    preds = parse_predictions(content, config.DEFAULT_TZ_OFFSET, source="discord")
    if not preds:
        return
    added = storage.add_predictions(new_preds=preds)
    if added <= 0:
        return
    logger.info(f"Discord [{origin}]: добавлено {added} новых прогнозов")
    # Берём именно добавленные (последние added) и уведомляем с пометкой
    await _notify_added(preds[-added:], label)


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
                stats = await _manual_recheck("автопроверка")
                logger.info(
                    f"Discord periodic sync: сообщений {stats['messages']}, "
                    f"+{stats['added']} ~{stats['updated']} -{stats['removed']}"
                )
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
        logger.info("Discord: обнаружено редактирование — запускаю синхронизацию")
        try:
            await _manual_recheck("изменённое сообщение")
        except Exception as e:
            logger.error(f"on_message_edit sync error: {e}")

    client.run(config.DISCORD_TOKEN, log_handler=None)
