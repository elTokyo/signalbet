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
    """Каждые 30 секунд: уведомления + автоудаление."""
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=config.DELETE_AFTER_MINUTES)

    for chat_id in storage.get_all_chat_ids():
        predictions = storage.load_predictions(chat_id)
        changed = False
        kept = []

        for pred in predictions:
            # ── автоудаление: матч начался >5 минут назад ───────────────────
            if pred.match_time <= cutoff:
                logger.info(f"[cleanup] {chat_id}: удалён '{pred.text[:50]}...'")
                changed = True
                continue

            diff_min = (pred.match_time - now).total_seconds() / 60

            # ── уведомление за 30 минут ─────────────────────────────────────
            if not pred.notified_30 and 28 <= diff_min <= 32:
                if await _send(app, chat_id, format_reminder(pred, 30)):
                    pred.notified_30 = True
                    changed = True

            # ── уведомление за 5 минут ──────────────────────────────────────
            if not pred.notified_5 and 3 <= diff_min <= 7:
                if await _send(app, chat_id, format_reminder(pred, 5)):
                    pred.notified_5 = True
                    changed = True

            kept.append(pred)

        if changed:
            storage.save_predictions(chat_id, kept)


async def _send(app: Application, chat_id: int, text: str) -> bool:
    try:
        await app.bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as e:
        logger.error(f"send_message error to {chat_id}: {e}")
        return False
