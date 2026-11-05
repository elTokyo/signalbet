"""
Discord-бот: слушает текстовый канал, парсит сообщения как прогнозы
и сохраняет в общую БД (storage.py).

Три источника прогнозов:
1. on_message          — новое сообщение (мгновенно)
2. on_raw_message_edit — редактирование сообщения (мгновенно; raw-событие приходит и для
                         сообщений вне кэша бота — например опубликованных до рестарта)
3. periodic_recheck    — перечит последних сообщений раз в 15 минут (страховка)

Все три пути идут через storage.apply_discord_messages: дата КАЖДОГО матча определяется
по моменту его появления в сообщении (публикация / правка), а не по возрасту сообщения
(см. match_dates.py). Повторная обработка и параллельные события дублей не создают.
"""
import asyncio
import logging
from datetime import datetime, timezone
import discord
import aiohttp

import storage
import config
from parser import format_time_local, display_text_without_time
from match_dates import can_still_matter, to_local
import health

logger = logging.getLogger(__name__)

RECHECK_INTERVAL_SEC = 15 * 60   # перечит раз в 15 минут
RECHECK_HISTORY_LIMIT = 50       # читаем больше сообщений чтобы охватить все прогнозы дня

# Выше этого числа прогнозов в одном синке шлём короткую сводку по лигам
# вместо полного построчного списка — полный список всегда есть в /list.
_SUMMARY_THRESHOLD = 15
_TG_MESSAGE_LIMIT = 3900

# Ссылки на работающий Discord-клиент и его event loop —
# нужны чтобы вызвать перечит из другого потока (Telegram-команда /syncdiscord)
_client_ref: discord.Client | None = None
_loop_ref = None


def _naive_utc(dt):
    """aware datetime из discord.py → naive UTC (так хранится время во всём проекте)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _snapshot(message: discord.Message) -> dict:
    """Снимок сообщения: постоянный id, время публикации/правки (отдельно!) и текст."""
    return {
        "id": str(message.id),
        "created_at": _naive_utc(message.created_at),
        "edited_at": _naive_utc(message.edited_at),
        "content": message.content or "",
    }


async def _apply_and_notify(snapshots: list[dict], label: str, **kwargs) -> dict:
    """
    Применяет снимки к расписанию и шлёт уведомление о реально добавленных прогнозах.
    storage ходит в Gist синхронным requests — уносим из event loop Discord.
    """
    stats = await asyncio.to_thread(storage.apply_discord_messages, snapshots, **kwargs)
    added = stats.get("added_preds", [])
    if added:
        await _notify_added(added, label)
    undetermined = stats.get("undetermined_new") or []
    if undetermined:
        await _ask_admins_undetermined(undetermined)
    return stats


MAX_UNDETERMINED_ASKS = 6   # не больше стольких вопросов за один проход, остальное — в /undetermined


async def _ask_admins_undetermined(items: list[dict]):
    """
    Матч, дату которого бот не смог определить достоверно, не пропадает молча:
    админам уходит вопрос «Сегодня / Завтра / Пропустить». Ответ обрабатывает
    handlers.on_action_callback (storage.resolve_undetermined). Спрашиваем один раз на матч.
    """
    admins = list(config.ADMIN_CHAT_IDS)
    if not admins:
        return
    now = datetime.utcnow()
    for item in items[:MAX_UNDETERMINED_ASKS]:
        created = item.get("created")
        when = f"{to_local(created, config.DEFAULT_TZ_OFFSET):%d.%m %H:%M}" if created else "?"
        text = (
            f"❓ Не удалось определить дату матча\n\n"
            f"{item['text'][:300]}\n\n"
            f"Сообщение в Discord от {when}. {item['reason']}.\n\n"
            f"Когда он играет?"
        )
        btns = storage.ud_buttons(item, now)
        await _send_tg(admins, text, buttons=[btns[:2], btns[2:]])
    rest = len(items) - MAX_UNDETERMINED_ASKS
    if rest > 0:
        await _send_tg(admins, f"❓ Ещё матчей без даты: {rest}. Список с кнопками: /undetermined")


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
            f"🗑 Удалено: {stats['removed']}\n"
            f"⏭ Пропущено: прошедших {stats['skipped_stale']}, "
            f"с неясной датой {stats['skipped_undetermined']}, дублей {stats['skipped_duplicate']}"
        )
        return True, info
    except Exception as e:
        logger.error(f"Manual recheck error: {e}")
        return False, f"Ошибка: {e}"


async def _manual_recheck(label: str = "автопроверка") -> dict:
    """
    Перечит последних сообщений канала. Актуальность решается не возрастом сообщения,
    а датой каждого матча (storage.apply_discord_messages); здесь отсекается только
    заведомо мёртвая история (match_dates.can_still_matter).
    label — пометка источника для уведомления о добавленных прогнозах.
    """
    ch = _client_ref.get_channel(config.DISCORD_CHANNEL_ID)
    if not ch:
        raise RuntimeError("канал не найден")

    snapshots = []
    fetched = 0
    oldest = None
    now = datetime.utcnow()
    async for msg in ch.history(limit=RECHECK_HISTORY_LIMIT):
        fetched += 1
        oldest = _naive_utc(msg.created_at)
        if msg.author == _client_ref.user:
            continue
        snap = _snapshot(msg)
        if not can_still_matter(snap["created_at"], snap["edited_at"], now):
            continue
        snapshots.append(snap)

    # Если история прочитана не целиком, удалённые сообщения старше oldest отличить нельзя
    scan_floor = oldest if fetched >= RECHECK_HISTORY_LIMIT else datetime.min

    stats = await _apply_and_notify(
        snapshots, label, full_scan=True, scan_floor=scan_floor,
    )
    health.discord_sync(True, stats)    # перечит прошёл — для /status и сторожа
    return {"messages": len(snapshots), **stats}


async def _notify_added(preds: list, label: str):
    """
    Шлёт уведомление о добавленных прогнозах с пометкой источника (в скобках).

    Прогнозы группируются по времени матча (одна временная метка — одна строка
    с эмодзи, дальше список матчей на это время без повтора времени в каждой
    строке) — раньше время дублировалось в каждой строке (⏰ {t} {p.text}, а
    p.text уже сам по себе начинается с "Лига ... HH-MM ..."), что при большом
    числе прогнозов (например после миграции/переезда на новый парсер, когда
    Discord присылает разом весь актуальный список) превращало сообщение в
    нечитаемую простыню.

    При очень большом числе прогнозов (> _SUMMARY_THRESHOLD) шлём короткую
    сводку с разбивкой по лигам вместо полного списка — полный список всегда
    доступен через /list.

    Сообщение разбивается на чанки по лимиту Telegram (см. _chunk_lines) —
    раньше это не делалось, и большая пачка прогнозов рисковала быть обрезана
    или не отправлена вовсе (Telegram отклоняет сообщения длиннее ~4096 символов).
    """
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
                messages = _build_notify_messages(preds, s.timezone_offset, label)
                for text in messages:
                    try:
                        await session.post(
                            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                        )
                    except Exception as e:
                        logger.error(f"TG send to {chat_id} failed: {e}")
    except Exception as e:
        logger.error(f"Discord broadcast error: {e}")


async def _send_tg(recipients: list[int], text: str, buttons: list = None) -> int:
    """
    Отправка простого текста списку чатов через HTTP API Telegram.
    Возвращает число реально доставленных. Раньше HTTP-статус не проверялся:
    если Telegram отвечал ошибкой (400/403/429), в логе всё равно писалось
    «уведомление отправлено».
    """
    if not recipients:
        return 0
    delivered = 0
    payload_extra = {}
    if buttons:
        # buttons: [[(подпись, callback_data), ...], ...] — рядами
        payload_extra["reply_markup"] = {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in buttons
        ]}
    try:
        async with aiohttp.ClientSession() as session:
            for chat_id in recipients:
                try:
                    async with session.post(
                        f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": text, **payload_extra},
                    ) as resp:
                        if resp.status == 200:
                            delivered += 1
                        else:
                            body = (await resp.text())[:200]
                            logger.error(f"TG send to {chat_id}: HTTP {resp.status} {body}")
                except Exception as e:
                    logger.error(f"TG send to {chat_id} failed: {e}")
    except Exception as e:
        logger.error(f"TG broadcast error: {e}")
    return delivered


def _recipients_for(field: str) -> list[int]:
    """Авторизованные пользователи, у которых включён нужный тумблер."""
    return [
        rid for rid in storage.get_all_recipient_chat_ids()
        if getattr(storage.load_settings(rid), field, True)
    ]


# ── Голосовой канал: заход / выход ───────────────────────────────────────────

# Время последних отправленных уведомлений (антиспам-кулдаун)
_voice_last_sent: dict[str, float] = {"join": 0.0, "leave": 0.0}
# Отложенная задача «вышел» — отменяется если человек вернулся за grace-период
_voice_leave_task = None
# Для диагностики (/voice): последнее событие цели и время последних отправок
_voice_diag: dict = {"last_event": None, "last_event_at": None, "sent_at": {}}


def _voice_enabled() -> bool:
    return bool(config.DISCORD_VOICE_USER_ID and config.DISCORD_VOICE_CHANNEL_ID)


def _fmt_utc(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "—"


async def _voice_notify(kind: str, member_name: str, channel_name: str):
    """Шлёт уведомление о заходе/выходе с учётом кулдауна."""
    now = asyncio.get_event_loop().time()
    prev = _voice_last_sent.get(kind, 0.0)
    if prev and (now - prev) < config.VOICE_COOLDOWN_SEC:
        logger.info(f"Voice {kind}: пропуск, кулдаун ({int(now - prev)}с назад было)")
        return
    # Занимаем слот сразу (защита от двойной отправки при гонке событий),
    # но откатываем, если не дошло вообще никому.
    _voice_last_sent[kind] = now

    if kind == "join":
        text = f"🎧 {member_name} зашёл в голосовой «{channel_name}»"
    else:
        text = f"👋 {member_name} вышел из голосового «{channel_name}»"

    # storage ходит в Gist синхронным requests — уносим из event loop Discord,
    # чтобы не задерживать heartbeat гейтвея.
    recipients = await asyncio.to_thread(_recipients_for, "notify_voice")
    if not recipients:
        _voice_last_sent[kind] = prev
        logger.warning(
            f"Voice {kind}: некому слать — нет авторизованных или у всех выключен "
            f"«🎧 Заход в войс Discord» в /settings"
        )
        return

    delivered = await _send_tg(recipients, text)
    if delivered:
        _voice_diag["sent_at"][kind] = datetime.now(timezone.utc)
        logger.info(f"Voice {kind}: отправлено {delivered}/{len(recipients)} ({member_name})")
    else:
        _voice_last_sent[kind] = prev
        logger.error(f"Voice {kind}: Telegram не принял ни одного сообщения ({len(recipients)} получателей)")


def _find_target_channel():
    """Где цель сидит СЕЙЧАС среди каналов, которые видит бот (или None)."""
    if _client_ref is None:
        return None
    for g in _client_ref.guilds:
        for vc in g.voice_channels:
            if config.DISCORD_VOICE_USER_ID in vc.voice_states:
                return vc
    return None


def get_voice_status() -> str:
    """Текст для /voice: включено ли слежение и что бот реально видит."""
    lines = ["🎧 Слежение за войсом Discord", ""]
    if not _voice_enabled():
        lines.append("Выключено: не заданы DISCORD_VOICE_USER_ID и/или DISCORD_VOICE_CHANNEL_ID.")
        return "\n".join(lines)

    lines.append(f"Цель (user): {config.DISCORD_VOICE_USER_ID}")
    lines.append(f"Канал (id):  {config.DISCORD_VOICE_CHANNEL_ID}")
    if _client_ref is None or _client_ref.is_closed():
        lines.append("\n❌ Discord-клиент не запущен / соединение закрыто.")
        return "\n".join(lines)

    vch = _client_ref.get_channel(config.DISCORD_VOICE_CHANNEL_ID)
    if vch is None:
        lines.append(
            "\n❌ Бот НЕ видит этот канал: неверный ID или у бота нет права View Channel "
            "на голосовом канале. Пока так — события захода/выхода до бота не доходят."
        )
    else:
        inside = config.DISCORD_VOICE_USER_ID in vch.voice_states
        lines.append(f"\n✅ Канал найден: «{vch.name}», сейчас внутри: {len(vch.voice_states)}")
        lines.append(f"Цель в этом канале: {'ДА' if inside else 'нет'}")
        if not inside:
            other = _find_target_channel()
            if other is not None:
                lines.append(f"⚠️ Но цель сидит в другом канале: «{other.name}» (id {other.id})")
            else:
                lines.append("Ни в одном видимом боту войсе цели нет.")

    ev, ev_at = _voice_diag["last_event"], _voice_diag["last_event_at"]
    lines.append(f"\nПоследнее событие цели: {ev or 'не было с момента запуска'} ({_fmt_utc(ev_at)})")
    sent = _voice_diag["sent_at"]
    lines.append(f"Последнее «зашёл»: {_fmt_utc(sent.get('join'))}")
    lines.append(f"Последнее «вышел»: {_fmt_utc(sent.get('leave'))}")

    try:
        n = len(_recipients_for("notify_voice"))
        lines.append(f"Получателей с включённым тумблером: {n}")
    except Exception as e:
        lines.append(f"Получатели: ошибка ({e})")
    return "\n".join(lines)


def _pluralize_predictions(n: int) -> str:
    """Русское склонение 'прогноз/прогноза/прогнозов' по числу n."""
    n_abs = abs(n) % 100
    last = n_abs % 10
    if 11 <= n_abs <= 14:
        return "прогнозов"
    if last == 1:
        return "прогноз"
    if 2 <= last <= 4:
        return "прогноза"
    return "прогнозов"


def _build_notify_messages(preds: list, tz_offset: int, label: str) -> list[str]:
    """Строит одно или несколько готовых к отправке сообщений Telegram."""
    header = f"🤖 Из Discord: +{len(preds)} {_pluralize_predictions(len(preds))} ({label})"

    if len(preds) > _SUMMARY_THRESHOLD:
        return _chunk_lines([header, ""] + _summary_lines(preds), _TG_MESSAGE_LIMIT)

    lines = [header, ""]
    for time_label, group in _group_by_time(preds, tz_offset):
        lines.append(f"⏰ {time_label}")
        for p in group:
            lines.append(f"   {display_text_without_time(p, tz_offset)}")
        lines.append("")

    return _chunk_lines(lines, _TG_MESSAGE_LIMIT)


def _group_by_time(preds: list, tz_offset: int) -> list[tuple[str, list]]:
    """
    Группирует прогнозы по локальному времени матча, сортируя группы по
    фактическому match_time (а не по порядку появления в preds — тот
    определяется порядком чтения истории Discord, не временем матчей,
    из-за чего группы шли вперемешку: 11:30, 12:00, ..., 14:00, 10:30).
    Сортировка по datetime, а не по строке времени, корректно обрабатывает
    переход через полночь (нельзя просто сравнить "09:00" < "23:30" как строки).
    """
    groups: dict[str, list] = {}
    for p in preds:
        t = format_time_local(p, tz_offset)
        groups.setdefault(t, []).append(p)
    ordered_labels = sorted(groups, key=lambda t: min(p.match_time for p in groups[t]))
    return [(t, groups[t]) for t in ordered_labels]


def _summary_lines(preds: list) -> list[str]:
    """
    Короткая сводка вместо полного списка: количество прогнозов по лиге.
    Лигу берём как текст до первого времени в p.text (то же самое, что
    видит пользователь в начале каждой строки прогноза).
    """
    import re
    counts: dict[str, int] = {}
    order: list[str] = []
    for p in preds:
        m = re.match(r'^(.*?)\s*\d{1,2}[-:]\d{2}\b', p.text)
        league = m.group(1).strip() if m else p.text[:40].strip()
        if league not in counts:
            counts[league] = 0
            order.append(league)
        counts[league] += 1
    lines = [f"• {league} — {counts[league]}" for league in order]
    lines.append("")
    lines.append("Полный список: /list")
    return lines


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    """Склеивает строки в сообщения, не превышая limit символов каждое."""
    messages = []
    buffer = ""
    for line in lines:
        candidate = f"{buffer}\n{line}" if buffer else line
        if len(candidate) > limit and buffer:
            messages.append(buffer)
            buffer = line
        else:
            buffer = candidate
    if buffer.strip():
        messages.append(buffer)
    return messages


async def _process_message(message: discord.Message, label: str):
    """Обработка одного нового или изменённого сообщения."""
    stats = await _apply_and_notify([_snapshot(message)], label)
    if stats["added"] or stats["updated"] or stats["removed"]:
        logger.info(f"Discord [{label}] msg {message.id}: +{stats['added']} ~{stats['updated']} -{stats['removed']}")


def run_discord_listener():
    if not config.DISCORD_TOKEN or not config.DISCORD_CHANNEL_ID:
        logger.warning("Discord не настроен (нет DISCORD_TOKEN / DISCORD_CHANNEL_ID)")
        return

    intents = discord.Intents.default()
    intents.message_content = True
    # Нужен для on_voice_state_update (в Intents.default() уже включён,
    # но фиксируем явно чтобы не отвалилось при смене defaults в discord.py)
    intents.voice_states = True

    # Голосовые *подключения* нам не нужны (только события входа/выхода), поэтому
    # предупреждение «PyNaCl is not installed, voice will NOT be supported» — шум.
    discord.VoiceClient.warn_nacl = False
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
                health.discord_sync(False, error=str(e)[:120])

    # ── События ──────────────────────────────────────────────────────────────
    health.discord_enabled(True)

    @client.event
    async def on_ready():
        global _client_ref, _loop_ref
        _client_ref = client
        _loop_ref = client.loop
        health.discord_connected(True)
        logger.info(f"Discord listener запущен: {client.user}")
        ch = client.get_channel(config.DISCORD_CHANNEL_ID)
        if ch:
            logger.info(f"Слушаю канал: #{ch.name}")
        else:
            logger.error(f"Канал {config.DISCORD_CHANNEL_ID} не найден")
        if _voice_enabled():
            vch = client.get_channel(config.DISCORD_VOICE_CHANNEL_ID)
            logger.info(
                f"Слежу за войсом: {vch.name if vch else config.DISCORD_VOICE_CHANNEL_ID}, "
                f"юзер {config.DISCORD_VOICE_USER_ID}"
            )
            if not vch:
                logger.warning(
                    "Голосовой канал не найден — проверь DISCORD_VOICE_CHANNEL_ID "
                    "и доступ бота к каналу (View Channel)"
                )
            else:
                # Если цель уже сидела в войсе к моменту старта бота (например,
                # во время редеплоя) — события «зашёл» не будет вообще, это норма.
                inside = config.DISCORD_VOICE_USER_ID in vch.voice_states
                logger.info(
                    f"Войс «{vch.name}»: внутри {len(vch.voice_states)}, "
                    f"цель {'УЖЕ ВНУТРИ (уведомления о заходе не будет)' if inside else 'не в канале'}"
                )
        else:
            logger.info("Слежение за войсом выключено (нет DISCORD_VOICE_* переменных)")
        client.loop.create_task(periodic_recheck())

    @client.event
    async def on_disconnect():
        health.discord_connected(False)

    @client.event
    async def on_resumed():
        health.discord_connected(True)

    @client.event
    async def on_voice_state_update(member, before, after):
        """
        Следим за одним конкретным пользователем в одном конкретном войсе.

        Антиспам:
          • уведомление о выходе откладывается на VOICE_LEAVE_GRACE_SEC —
            если человек вернулся за это время, это реконнект и не шлём ничего;
          • на каждый тип события (заход/выход) действует кулдаун
            VOICE_COOLDOWN_SEC (по умолчанию 10 минут).
        """
        global _voice_leave_task

        if not _voice_enabled():
            return
        if member.id != config.DISCORD_VOICE_USER_ID:
            return

        target = config.DISCORD_VOICE_CHANNEL_ID
        was_in = before.channel is not None and before.channel.id == target
        now_in = after.channel is not None and after.channel.id == target

        # Каждое перемещение цели между каналами пишем в лог — иначе при «не пришло
        # уведомление» не видно, дошло ли событие вообще и в какой канал он заходил.
        b_id = before.channel.id if before.channel else None
        a_id = after.channel.id if after.channel else None
        if b_id != a_id:
            _voice_diag["last_event"] = f"{b_id} → {a_id}"
            _voice_diag["last_event_at"] = datetime.now(timezone.utc)
            logger.info(
                f"Voice: цель {b_id} → {a_id} (следим за каналом {target}"
                f"{'' if (was_in or now_in) else ', это ДРУГОЙ канал — игнор'})"
            )

        if now_in == was_in:
            return  # мут/деаф/стрим и прочие изменения внутри канала — игнор

        name = getattr(member, "display_name", None) or str(member)

        if now_in:
            # Вернулся до истечения grace → это был реконнект: гасим «вышел»
            if _voice_leave_task and not _voice_leave_task.done():
                _voice_leave_task.cancel()
                _voice_leave_task = None
                logger.info("Voice: реконнект, уведомления не шлём")
                return
            await _voice_notify("join", name, after.channel.name)
        else:
            channel_name = before.channel.name

            async def delayed_leave():
                try:
                    await asyncio.sleep(config.VOICE_LEAVE_GRACE_SEC)
                    await _voice_notify("leave", name, channel_name)
                except asyncio.CancelledError:
                    pass

            if _voice_leave_task and not _voice_leave_task.done():
                _voice_leave_task.cancel()
            _voice_leave_task = client.loop.create_task(delayed_leave())

    @client.event
    async def on_message(message: discord.Message):
        if message.author == client.user:
            return
        if message.channel.id != config.DISCORD_CHANNEL_ID:
            return
        try:
            await _process_message(message, "новое сообщение")
        except Exception as e:
            logger.exception(f"on_message error (msg {message.id}): {e}")

    @client.event
    async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
        # raw-событие, а не on_message_edit: то приходит только для сообщений из кэша
        # бота, и правка сообщения, опубликованного до рестарта, терялась до перечита.
        if payload.channel_id != config.DISCORD_CHANNEL_ID:
            return
        try:
            ch = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
            try:
                # актуальная версия сообщения (в payload.data может быть неполная)
                message = await ch.fetch_message(payload.message_id)
            except discord.NotFound:
                removed = await asyncio.to_thread(storage.retract_discord_message, payload.message_id)
                logger.info(f"Discord: сообщение {payload.message_id} удалено до обработки правки, убрано {removed}")
                return
            if message.author == client.user:
                return
            logger.info(f"Discord: правка сообщения {message.id} — обрабатываю")
            await _process_message(message, "изменённое сообщение")
        except Exception as e:
            logger.exception(f"on_raw_message_edit error (msg {payload.message_id}): {e}")

    @client.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        if payload.channel_id != config.DISCORD_CHANNEL_ID:
            return
        try:
            removed = await asyncio.to_thread(storage.retract_discord_message, payload.message_id)
            logger.info(f"Discord: сообщение {payload.message_id} удалено, убрано из расписания: {removed}")
        except Exception as e:
            logger.exception(f"on_raw_message_delete error (msg {payload.message_id}): {e}")

    client.run(config.DISCORD_TOKEN, log_handler=None)
