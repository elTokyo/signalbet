import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import storage
import config
from parser import format_reminder

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def setup_scheduler(app: Application):
    scheduler.add_job(
        notification_tick,
        trigger=IntervalTrigger(seconds=30),
        args=[app],
        id="notification_tick",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler запущен: проверка каждые 30 секунд")


async def notification_tick(app: Application):
    """Каждые 30 секунд: проверка прогнозов и рассылка уведомлений всем."""
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=config.DELETE_AFTER_MINUTES)

    predictions = storage.load_predictions()
    if not predictions:
        return

    recipients = storage.get_all_recipient_chat_ids()
    if not recipients:
        return

    changed = False
    kept = []

    for pred in predictions:
        # Автоудаление прогноза если матч начался >5 минут назад
        if pred.match_time <= cutoff:
            logger.info(f"[cleanup] удалён: {pred.text[:50]}...")
            changed = True
            continue

        diff_min = (pred.match_time - now).total_seconds() / 60

        # За 30 минут
        if not pred.notified_30 and 28 <= diff_min <= 32:
            await _broadcast(app, recipients, format_reminder(pred, 30))
            pred.notified_30 = True
            changed = True
            logger.info(f"[30min] отправлено {len(recipients)} получателям: {pred.text[:50]}")

        # За 5 минут
        if not pred.notified_5 and 3 <= diff_min <= 7:
            await _broadcast(app, recipients, format_reminder(pred, 5))
            pred.notified_5 = True
            changed = True
            logger.info(f"[5min] отправлено {len(recipients)} получателям: {pred.text[:50]}")

        kept.append(pred)

    if changed:
        storage.save_predictions(predictions=kept)


async def _broadcast(app: Application, chat_ids: list[int], text: str):
    """Отправляет одно сообщение всем получателям."""
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error(f"send_message {chat_id} failed: {e}")
