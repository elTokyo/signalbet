import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import time
import storage
import config
import auth
import health
import userprefs
import fonbet as fonbet_mod
from parser import format_reminder
from match_dates import MONITOR_AFTER_START_MIN, retention_minutes
from fonbet import (
    fetch_events, find_matching_event, extract_teams_from_prediction, prediction_script,
    check_crookedness, check_close_match, build_match_url, format_bet_odds,
    has_relevant_odds, parse_bet_from_prediction,
)
import bookmakers

import asyncio

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Окно мониторинга Фонбета: за час до старта → +15 мин после старта.
# «После старта» теперь действительно 15 минут: прогноз не удаляется из расписания,
# пока окно не закончится (см. retention_minutes ниже и в storage).
FONBET_WINDOW_BEFORE_MIN = 60
FONBET_WINDOW_AFTER_MIN  = MONITOR_AFTER_START_MIN

# «Горячее» окно — частые проверки (10 мин до старта → 10 мин после)
HOT_WINDOW_BEFORE_MIN = 10
HOT_WINDOW_AFTER_MIN  = 10

# Тик Фонбета раз в 15 сек. «Холодные» матчи проверяем раз в 4 тика (=60 сек),
# «горячие» — каждый тик (=15 сек).
FONBET_TICK_SEC = 15
_tick_counter = 0

# Защита от дублей уведомлений при гонке тиков (Gist пишется с задержкой).
# Храним id прогнозов по которым уже отправили уведомление в этой сессии.
_sent_prematch: set = set()
_sent_live: set = set()
_sent_crooked: set = set()
_sent_close_match: set = set()

# Лок чтобы тики не накладывались друг на друга (тик может длиться >15 сек)
_fonbet_lock = asyncio.Lock()

# Диагностика промахов матчинга: «почему не нашёл». Подробный разбор дорогой (fuzzy по всем
# событиям), поэтому не чаще раза в DIAG_EVERY_SEC на прогноз; результат живёт в памяти и
# попадает в журнал, только если матч так и не нашёлся к концу окна мониторинга.
DIAG_EVERY_SEC = 120
_diag_ts: dict = {}      # pred.id → монотонное время последней диагностики
_last_diag: dict = {}    # pred.id → последний diag


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
    # Job 3: сторож — алерт админам, если Фонбет/Discord молчат (и «починилось»)
    scheduler.add_job(
        health_tick,
        trigger=IntervalTrigger(seconds=60),
        args=[app],
        id="health_tick",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler запущен: notifications 30с, fonbet {FONBET_TICK_SEC}с (горячее окно ±10мин)")


# ── Уведомления за 30/5 мин + автоудаление ───────────────────────────────────

async def notification_tick(app: Application):
    now = datetime.utcnow()
    # Не раньше конца окна мониторинга: иначе прогноз исчезал через DELETE_AFTER_MINUTES
    # (5 мин) и лайв-уведомление уже не могло прийти.
    cutoff = now - timedelta(minutes=retention_minutes(config.DELETE_AFTER_MINUTES))

    predictions = storage.load_predictions()
    if not predictions:
        return

    all_recipients = storage.get_all_recipient_chat_ids()
    if not all_recipients:
        return
    cache: dict = {}
    # Только те у кого включены напоминания
    recipients = [
        rid for rid in all_recipients
        if getattr(_user_settings(rid, cache), "notify_reminders", True)
    ]

    changed = False
    kept = []

    for pred in predictions:
        if pred.match_time <= cutoff:
            logger.info(f"[cleanup] {pred.text[:50]}...")
            await _journal_miss_if_any(pred)
            changed = True
            continue

        diff_min = (pred.match_time - now).total_seconds() / 60

        if not pred.notified_30 and 28 <= diff_min <= 32:
            rcpt = _allowed(recipients, pred, now, cache)
            if rcpt:
                await _broadcast(app, rcpt, format_reminder(pred, 30), pred=pred)
            pred.notified_30 = True
            changed = True

        if not pred.notified_5 and 3 <= diff_min <= 7:
            rcpt = _allowed(recipients, pred, now, cache)
            if rcpt:
                await _broadcast(app, rcpt, format_reminder(pred, 5), pred=pred)
            pred.notified_5 = True
            changed = True

        kept.append(pred)

    if changed:
        storage.save_predictions(predictions=kept)


def _user_settings(rid: int, cache: dict):
    """Настройки пользователя с кэшем на время одного тика (меньше обращений к Gist)."""
    if rid not in cache:
        cache[rid] = storage.load_settings(rid)
    return cache[rid]


def _allowed(rids: list[int], pred, now: datetime, cache: dict) -> list[int]:
    """Получатели, которым по личным фильтрам (тихие часы, заглушка матча, страна) можно слать."""
    return [r for r in rids if userprefs.user_allows(_user_settings(r, cache), pred, now)]


async def _journal_miss_if_any(pred):
    """Матч покинул расписание, так и не найденный на Фонбете → запись в журнал с причиной."""
    diag = _last_diag.pop(pred.id, None)
    _diag_ts.pop(pred.id, None)
    if pred.fonbet_matched or pred.fonbet_notified_prematch or pred.fonbet_notified_live or pred.crooked_notified:
        return
    entry = {
        "kind": "miss", "pred_id": pred.id, "text": pred.text[:160],
        "match_time": pred.match_time.isoformat(),
        "reason": (diag or {}).get("reason") or "unknown",
        "why": (diag or {}).get("text") or "диагностики нет (бот перезапускался или матч не попал в окно мониторинга)",
        "best": (diag or {}).get("best") or [],
    }
    try:
        await asyncio.to_thread(storage.journal_add, entry)
        logger.info(f"[fonbet MISS] не найден: {pred.text[:60]} — {entry['why']}")
    except Exception as e:
        logger.error(f"journal_add(miss) failed: {e}")


async def health_tick(app: Application):
    """Раз в минуту: проверяет здоровье и шлёт админам переходы «сломалось» / «починилось»."""
    try:
        for _name, kind, text in health.evaluate():
            logger.warning(f"[health {kind}] {text}")
            await _broadcast(app, list(config.ADMIN_CHAT_IDS), text)
    except Exception as e:
        logger.error(f"health_tick error: {e}")


# ── Fonbet: prematch + live ──────────────────────────────────────────────────

async def fonbet_tick(app: Application):
    """
    Каждые 15 сек: проверяет Fonbet.
    Горячие матчи (±10 мин от старта) — каждый тик.
    Холодные (остальное окно) — раз в 60 сек (каждый 4-й тик).
    """
    # Если предыдущий тик ещё выполняется — пропускаем (защита от наложения)
    if _fonbet_lock.locked():
        logger.debug("fonbet_tick: предыдущий тик ещё идёт, пропуск")
        return

    async with _fonbet_lock:
        await _fonbet_tick_inner(app)


async def _fonbet_tick_inner(app: Application):
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

    # Один запрос к Fonbet для всех. Синхронный requests уносим в поток: пока Фонбет
    # тормозит (таймаут 15 с), бот не должен зависать для команд и кнопок.
    events = await asyncio.to_thread(fetch_events)
    health.fonbet_result(bool(events), len(events), fonbet_mod.LAST_ERROR.get("msg"))
    if not events:
        return

    # Прогнозы на русском ищем среди русских названий (второй запрос — только если нужен)
    events_ru = None
    if any(prediction_script(p.text) == "ru" for p in to_check):
        events_ru = await asyncio.to_thread(fetch_events, "ru") or None
        if not events_ru:
            logger.warning("Fonbet: русский список событий не получен — ищем прогнозы на русском в английском")

    all_recipients = storage.get_all_recipient_chat_ids()
    if not all_recipients:
        return

    try:
        aliases = await asyncio.to_thread(storage.load_aliases)
    except Exception as e:
        logger.error(f"load_aliases failed: {e}")
        aliases = {}

    settings_cache: dict = {}

    # Хелпер: получатели по категории уведомления + личные фильтры (тихие часы, заглушка, страна)
    def recipients_for(field: str, pred=None) -> list[int]:
        out = []
        for rid in all_recipients:
            st = _user_settings(rid, settings_cache)
            if getattr(st, field, True) and userprefs.user_allows(st, pred, now):
                out.append(rid)
        return out

    changed = False
    for pred in to_check:
        # Подробную диагностику промаха — первый раз сразу, дальше не чаще DIAG_EVERY_SEC.
        # (None = «ещё не делали»; нельзя считать от 0: time.monotonic() — это аптайм машины.)
        last_diag = _diag_ts.get(pred.id)
        diag = {} if (last_diag is None or time.monotonic() - last_diag >= DIAG_EVERY_SEC) else None
        pool = events_ru if (events_ru and prediction_script(pred.text) == "ru") else events
        # Передаём ожидаемое время матча (UTC) для проверки соответствия
        event = find_matching_event(pred.text, pool, expected_utc=pred.match_time,
                                    diag=diag, aliases=aliases)
        if not event:
            if diag is not None:
                _diag_ts[pred.id] = time.monotonic()
                if diag.get("reason"):
                    prev = _last_diag.get(pred.id, {}).get("reason")
                    _last_diag[pred.id] = diag
                    if prev != diag["reason"]:
                        logger.info(f"[fonbet MISS?] {pred.text[:60]} — {diag.get('text')}")
            continue
        _last_diag.pop(pred.id, None)

        # Первое совпадение: журнал, запоминаем пары названий, помечаем состояние для /today
        diff_min = (pred.match_time - now).total_seconds() / 60
        if not pred.fonbet_matched:
            pred.fonbet_matched = True
            changed = True
            try:
                await asyncio.to_thread(storage.journal_add, {
                    "kind": "found", "pred_id": pred.id, "text": pred.text[:160],
                    "match": f"{event['team1']} — {event['team2']}", "score": event.get("match_score"),
                    "tier": event.get("match_tier"), "league": event.get("league_name"),
                    "live": bool(event["is_live"]), "mins_before_start": round(diff_min),
                })
                if event.get("alias_pairs"):
                    n = await asyncio.to_thread(storage.add_aliases, event["alias_pairs"])
                    logger.info(f"[fonbet ALIAS] запомнено пар названий: {n} — {event['alias_pairs']}")
            except Exception as e:
                logger.error(f"journal/alias write failed: {e}")
        state = "live" if event["is_live"] else "line"
        if pred.fonbet_state != state:
            pred.fonbet_state = state
            changed = True

        # Берём названия команд из ПРОГНОЗА (как просил)
        team1, team2 = extract_teams_from_prediction(pred.text)
        # Коэффициенты под тип ставки (победитель → П1/П2, фора → кэф форы)
        odds_line = format_bet_odds(pred.text, event)
        match_url = build_match_url(event)

        if event["is_live"] and not pred.fonbet_notified_live and pred.id not in _sent_live:
            # Как и для линии — ждём релевантные коэффициенты, не шлём уведомление без кэфов.
            has_odds = has_relevant_odds(pred.text, event)
            # Дедлайн ожидания — синхронизирован с окном мониторинга (после этого матч
            # всё равно перестанем проверять), чтобы не ждать кэфы вечно.
            past_deadline = diff_min < -(FONBET_WINDOW_AFTER_MIN - 5)

            if has_odds or past_deadline:
                # Помечаем в in-memory множествах ДО отправки (защита от дублей в процессе)
                _sent_live.add(pred.id)
                _sent_prematch.add(pred.id)
                was_in_line = pred.fonbet_notified_prematch
                pred.fonbet_notified_live = True
                # Матч уже не вернётся в линию — закрываем и этот флаг
                pred.fonbet_notified_prematch = True
                changed = True
                odds_part = odds_line if has_odds else "(коэф. так и не появились)"
                msg = _format_live_message(team1, team2, odds_part, pred.text)
                await _broadcast(app, recipients_for("notify_match_out", pred), msg, url=match_url, pred=pred)
                logger.info(
                    f"[fonbet LIVE{'' if has_odds else ' no-odds timeout'}] {team1} — {team2} "
                    f"(признак: {event.get('live_reason')}, линия была: {was_in_line})"
                )
            else:
                logger.info(f"[fonbet LIVE waiting odds] {team1} — {team2}")

        elif (not event["is_live"]) and not pred.fonbet_notified_prematch and pred.id not in _sent_prematch:
            # Есть ли релевантные коэффициенты под ставку прогноза?
            has_odds = has_relevant_odds(pred.text, event)
            past_deadline = diff_min < -10

            if has_odds:
                _sent_prematch.add(pred.id)
                pred.fonbet_notified_prematch = True
                changed = True
                msg = (
                    f"📋 Матч вышел в ЛИНИЮ!\n"
                    f"{team1} — {team2}\n"
                    f"{odds_line}\n\n"
                    f"📝 Прогноз:\n{pred.text}"
                )
                await _broadcast(app, recipients_for("notify_match_out", pred), msg, url=match_url, pred=pred)
                logger.info(f"[fonbet LINE] {team1} — {team2}")
            elif past_deadline:
                _sent_prematch.add(pred.id)
                pred.fonbet_notified_prematch = True
                changed = True
                msg = (
                    f"📋 Матч вышел в ЛИНИЮ!\n"
                    f"{team1} — {team2}\n"
                    f"(коэф. так и не появились)\n\n"
                    f"📝 Прогноз:\n{pred.text}"
                )
                await _broadcast(app, recipients_for("notify_match_out", pred), msg, url=match_url, pred=pred)
                logger.info(f"[fonbet LINE no-odds timeout] {team1} — {team2}")
            else:
                logger.info(f"[fonbet LINE waiting odds] {team1} — {team2}")

        # ── Кривой матч / андердог ──
        if not pred.crooked_notified and pred.id not in _sent_crooked:
            crooked = check_crookedness(pred.text, event)
            if crooked:
                _sent_crooked.add(pred.id)
                pred.crooked_notified = True
                changed = True
                status = "🔴 ЛАЙВ" if crooked["is_live"] else "📋 ЛИНИЯ"

                if crooked.get("kind") == "underdog":
                    head, field = "🐶 АНДЕРДОГ!", "notify_underdog"
                else:
                    head, field = "💰 КРИВОЙ МАТЧ!", "notify_crooked"

                msg = (
                    f"{head} ({status})\n"
                    f"{crooked['team1']} — {crooked['team2']}\n"
                    f"⚡ {crooked['reason']}\n"
                    f"{crooked['odds_info']}"
                )
                await _broadcast(app, recipients_for(field, pred), msg, url=crooked.get("url"), pred=pred)
                logger.info(
                    f"[fonbet {crooked.get('kind', 'crooked').upper()}] "
                    f"{team1} — {team2}: {crooked['reason']}"
                )

                # Вдогонку — ищем этот же матч и рынок на трёх других конторах.
                # Запускаем в фоне: три HTTP-запроса не должны тормозить тик.
                asyncio.create_task(
                    _bookmakers_followup(app, pred, team1, team2, field)
                )
            elif not pred.close_match_notified and pred.id not in _sent_close_match:
                # Матч ещё не "кривой", но кэф уже близко подошёл к порогу —
                # даём знать заранее, до того как сработает основное уведомление.
                close = check_close_match(pred.text, event)
                if close:
                    _sent_close_match.add(pred.id)
                    pred.close_match_notified = True
                    changed = True
                    status = "🔴 ЛАЙВ" if close["is_live"] else "📋 ЛИНИЯ"
                    msg = (
                        f"🎯 Высокое совпадение коэффициента! ({status})\n"
                        f"{close['team1']} — {close['team2']}\n"
                        f"⚡ {close['reason']}\n"
                        f"{close['odds_info']}"
                    )
                    await _broadcast(app, recipients_for("notify_close_match", pred), msg, url=close.get("url"), pred=pred)
                    logger.info(
                        f"[fonbet CLOSE_MATCH] {team1} — {team2}: {close['reason']}"
                    )

    # Сохраняем флаги ОДИН раз в конце тика (батч) — снижает нагрузку на Gist API
    if changed:
        storage.save_predictions(predictions=predictions)


async def _bookmakers_followup(app: Application, pred, team1: str, team2: str, field: str):
    """
    Догоняющее сообщение после «кривого»/«андердога»: есть ли этот матч и тот же
    рынок на BetBoom / Winline / Лига Ставок.

    Запросы синхронные (requests), поэтому уносим их в отдельный поток,
    чтобы не блокировать event loop бота.
    """
    try:
        bet = parse_bet_from_prediction(pred.text)
        if not bet:
            return
        results = await asyncio.to_thread(
            bookmakers.check_all, team1, team2, bet, pred.match_time
        )
        text = bookmakers.format_results(results)
        if not text:
            logger.info(f"[BK] {team1} — {team2}: ни одна контора матч не дала")
            return
        now_f = datetime.utcnow()
        cache: dict = {}
        recipients = [
            rid for rid in storage.get_all_recipient_chat_ids()
            if getattr(_user_settings(rid, cache), field, True)
            and userprefs.user_allows(_user_settings(rid, cache), pred, now_f)
        ]
        await _broadcast(app, recipients, f"{team1} — {team2}\n{text}", pred=pred)
        logger.info(f"[BK] {team1} — {team2}: отправлен довесок по конторам")
    except Exception as e:
        logger.error(f"Bookmakers followup error: {e}")


def _format_live_message(team1: str, team2: str, odds_part: str, pred_text: str) -> str:
    """
    Уведомление «матч вышел в лайв». Прогноз дублируем ВСЕГДА.

    Раньше прогноз добавлялся только если матч не проходил через «линию»
    (там он уже был в сообщении) — из-за этого часть лайв-уведомлений
    приходила без прогноза, и приходилось искать его в предыдущем сообщении.
    """
    return (
        f"🔴 Матч вышел в лайв!\n"
        f"{team1} — {team2}\n"
        f"{odds_part}\n\n"
        f"📝 Прогноз:\n{pred_text}"
    )


def _format_odds(odd_p1, odd_p2) -> str:
    """Форматирует строку с коэффициентами."""
    if odd_p1 is None and odd_p2 is None:
        return "(коэф. недоступны)"
    p1 = f"{odd_p1:.2f}" if odd_p1 is not None else "—"
    p2 = f"{odd_p2:.2f}" if odd_p2 is not None else "—"
    return f"П1: {p1}  |  П2: {p2}"


# ── Утилита broadcast ────────────────────────────────────────────────────────

async def _broadcast(app: Application, chat_ids: list[int], text: str, url: str = None, pred=None):
    """
    Отправляет сообщение всем получателям. Если задан url — кнопка «Открыть на Фонбете».
    Если передан прогноз — кнопки «🔕 Заглушить матч» и «🚫 <страна>» (личные фильтры
    каждого нажавшего, см. handlers.on_action_callback).
    """
    rows = []
    if url:
        rows.append([InlineKeyboardButton("📲 Открыть на Фонбете", url=url)])
    if pred is not None:
        row = [InlineKeyboardButton("🔕 Заглушить матч", callback_data=f"mute|{pred.id}")]
        country = userprefs.country_key(pred.text)
        if country:
            row.append(InlineKeyboardButton(f"🚫 {country.title()[:22]}", callback_data=f"blk|{pred.id}"))
        rows.append(row)
    markup = InlineKeyboardMarkup(rows) if rows else None
    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception as e:
            logger.error(f"send_message {chat_id} failed: {e}")
