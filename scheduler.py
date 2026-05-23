import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import storage
import config
import auth
from parser import format_reminder
from fonbet import (
    fetch_events, find_matching_event, extract_teams_from_prediction,
    check_crookedness, build_match_url,
)

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Окно мониторинга Фонбета: за час до старта → +20 мин после старта
FONBET_WINDOW_BEFORE_MIN = 60
FONBET_WINDOW_AFTER_MIN  = 20

# «Горячее» окно — частые проверки (10 мин до старта → 10 мин после)
HOT_WINDOW_BEFORE_MIN = 10
HOT_WINDOW_AFTER_MIN  = 10

# Тик Фонбета раз в 15 сек. «Холодные» матчи проверяем раз в 4 тика (=60 сек),
# «горячие» — каждый тик (=15 сек).
FONBET_TICK_SEC = 15
_tick_counter = 0


def setup_scheduler(app: Application):
    # Job 1: таймер-напоминания + автоудаление (каждые 30 сек)
    scheduler.add_job(
        notification_tick,
        trigger=IntervalTrigger(seconds=30),
        args=[app],
        id="notification_tick",
        replace_existing=True,
    )
    # Job 2: проверка Фонбета (каждые 15 сек, с горячим/холодным окном внутри)
    scheduler.add_job(
        fonbet_tick,
        trigger=IntervalTrigger(seconds=FONBET_TICK_SEC),
        args=[app],
        id="fonbet_tick",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler запущен: notifications 30с, fonbet {FONBET_TICK_SEC}с (горячее окно ±10мин)")


# ── Уведомления за 30/5 мин + автоудаление ───────────────────────────────────

async def notification_tick(app: Application):
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
        if pred.match_time <= cutoff:
            logger.info(f"[cleanup] {pred.text[:50]}...")
            changed = True
            continue

        diff_min = (pred.match_time - now).total_seconds() / 60

        if not pred.notified_30 and 28 <= diff_min <= 32:
            await _broadcast(app, recipients, format_reminder(pred, 30))
            pred.notified_30 = True
            changed = True

        if not pred.notified_5 and 3 <= diff_min <= 7:
            await _broadcast(app, recipients, format_reminder(pred, 5))
            pred.notified_5 = True
            changed = True

        kept.append(pred)

    if changed:
        storage.save_predictions(predictions=kept)


# ── Fonbet: prematch + live ──────────────────────────────────────────────────

async def fonbet_tick(app: Application):
    """
    Каждые 15 сек: проверяет Fonbet.
    Горячие матчи (±10 мин от старта) — каждый тик.
    Холодные (остальное окно) — раз в 60 сек (каждый 4-й тик).
    """
    global _tick_counter
    _tick_counter += 1
    is_cold_tick = (_tick_counter % 4 == 0)  # каждый 4-й тик (раз в 60с) проверяем и холодные

    now = datetime.utcnow()
    predictions = storage.load_predictions()
    if not predictions:
        return

    # Отбираем прогнозы которые нужно мониторить
    to_check = []
    for p in predictions:
        if p.fonbet_notified_prematch and p.fonbet_notified_live and p.crooked_notified:
            continue  # уже всё уведомлено
        diff_min = (p.match_time - now).total_seconds() / 60

        # В общем окне мониторинга?
        if not (-FONBET_WINDOW_AFTER_MIN <= diff_min <= FONBET_WINDOW_BEFORE_MIN):
            continue

        # Горячее окно (±10 мин) — проверяем каждый тик
        is_hot = (-HOT_WINDOW_AFTER_MIN <= diff_min <= HOT_WINDOW_BEFORE_MIN)

        # Горячие — всегда; холодные — только на каждом 4-м тике
        if is_hot or is_cold_tick:
            to_check.append(p)

    if not to_check:
        return

    # Один запрос к Fonbet для всех
    events = fetch_events()
    if not events:
        return

    # Получатели — только те, у кого включены уведомления Fonbet
    all_recipients = storage.get_all_recipient_chat_ids()
    recipients = [
        rid for rid in all_recipients
        if storage.load_settings(rid).fonbet_notifications
    ]
    if not recipients:
        return

    changed = False
    for pred in to_check:
        # Передаём ожидаемое время матча (UTC) для проверки соответствия
        event = find_matching_event(pred.text, events, expected_utc=pred.match_time)
        if not event:
            continue

        # Берём названия команд из ПРОГНОЗА (как просил)
        team1, team2 = extract_teams_from_prediction(pred.text)
        odds_line = _format_odds(event.get("odd_p1"), event.get("odd_p2"))
        match_url = build_match_url(event)

        if event["is_live"] and not pred.fonbet_notified_live:
            msg = (
                f"🔴 Матч вышел в лайв!\n"
                f"{team1} — {team2}\n"
                f"{odds_line}"
            )
            await _broadcast(app, recipients, msg, url=match_url)
            pred.fonbet_notified_live = True
            # Раз дошли до live — prematch тоже больше не нужен
            pred.fonbet_notified_prematch = True
            changed = True
            logger.info(f"[fonbet live] {team1} — {team2}")

        elif (not event["is_live"]) and not pred.fonbet_notified_prematch:
            has_odds = event.get("odd_p1") is not None or event.get("odd_p2") is not None
            diff_min = (pred.match_time - now).total_seconds() / 60
            # Прошло ли 10 минут после старта? (diff_min < -10)
            past_deadline = diff_min < -10

            if has_odds:
                # Коэффициенты есть — уведомляем сразу
                msg = (
                    f"📋 Матч вышел в прематч!\n"
                    f"{team1} — {team2}\n"
                    f"{odds_line}"
                )
                await _broadcast(app, recipients, msg, url=match_url)
                pred.fonbet_notified_prematch = True
                changed = True
                logger.info(f"[fonbet prematch] {team1} — {team2}")
            elif past_deadline:
                # Коэффициентов так и нет, но 10 мин после старта прошло —
                # уведомляем без коэффициентов (вариант А) и закрываем
                msg = (
                    f"📋 Матч вышел в прематч!\n"
                    f"{team1} — {team2}\n"
                    f"(коэф. так и не появились)"
                )
                await _broadcast(app, recipients, msg, url=match_url)
                pred.fonbet_notified_prematch = True
                changed = True
                logger.info(f"[fonbet prematch no-odds timeout] {team1} — {team2}")
            else:
                # Матч найден, но коэф ещё нет и дедлайн не прошёл —
                # НЕ ставим флаг, продолжим проверять в следующих тиках
                logger.info(f"[fonbet prematch waiting odds] {team1} — {team2}")

        # ── Проверка на «кривой» матч (value) ──
        if not pred.crooked_notified:
            crooked = check_crookedness(pred.text, event)
            if crooked:
                status = "🔴 LIVE" if crooked["is_live"] else "📋 Прематч"
                msg = (
                    f"💰 КРИВОЙ МАТЧ! ({status})\n"
                    f"{crooked['team1']} — {crooked['team2']}\n"
                    f"⚡ {crooked['reason']}\n"
                    f"{crooked['odds_info']}"
                )
                await _broadcast(app, recipients, msg, url=crooked.get("url"))
                pred.crooked_notified = True
                changed = True
                logger.info(f"[fonbet CROOKED] {team1} — {team2}: {crooked['reason']}")

    if changed:
        storage.save_predictions(predictions=predictions)


def _format_odds(odd_p1, odd_p2) -> str:
    """Форматирует строку с коэффициентами."""
    if odd_p1 is None and odd_p2 is None:
        return "(коэф. недоступны)"
    p1 = f"{odd_p1:.2f}" if odd_p1 is not None else "—"
    p2 = f"{odd_p2:.2f}" if odd_p2 is not None else "—"
    return f"П1: {p1}  |  П2: {p2}"


# ── Утилита broadcast ────────────────────────────────────────────────────────

async def _broadcast(app: Application, chat_ids: list[int], text: str, url: str = None):
    """Отправляет сообщение всем получателям. Если задан url — добавляет кнопку."""
    markup = None
    if url:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📲 Открыть на Фонбете", url=url)]])
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception as e:
            logger.error(f"send_message {chat_id} failed: {e}")
