"""
Определение даты матча.

В Discord-сообщениях с прогнозами почти никогда нет даты — только время
(«18-00 Команда А — Команда Б»). Раньше парсер подставлял «сегодня» на момент
парсинга, поэтому вчерашнее сообщение, висящее в канале, каждый день
«превращалось» в прогноз на сегодня.

Теперь дата матча определяется один раз и привязывается к МОМЕНТУ, когда
матч появился в сообщении (anchor), а не к текущему времени:

  1. Явная дата в тексте / заголовке секции («21.10», «21 октября», «Завтра»)
     — относительные слова считаются от anchor, не от «сейчас».
  2. Правило «ближайшее вхождение времени после публикации» (прогноз —
     это про будущие матчи, поэтому матч не может быть раньше публикации):
       • время позже времени публикации            → в тот же день;
       • время раньше, публикация вечерняя (≥ 18)  → следующий день
         (вечерний пост «на завтра»);
       • время раньше, ночной матч (до 06:00)      → следующий день
         (матч после полуночи);
       • время раньше, публикация дневная          → НЕОПРЕДЕЛЕНО
         (сегодня и уже прошёл, или завтра — не угадываем).
  3. Неопределённые матчи в расписание не попадают, а логируются.

Модуль чистый (без Discord, Gist и текущего времени) — всё передаётся
параметрами, поэтому легко тестируется с контролируемыми датами.
Все datetime наружу — naive UTC (как datetime.utcnow() во всём проекте).
"""
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional

# Публикация начиная с этого часа (локального) считается «вечерней»:
# времена, которые РАНЬШЕ часа публикации, относятся к следующему дню.
EVENING_POST_FROM_HOUR = 18
# Матчи до этого часа в сообщении, опубликованном раньше, — «после полуночи».
NIGHT_UNTIL_HOUR = 6
# Матч, начавшийся за столько минут до публикации, ещё «в тот же день»
# (сообщение выложили, когда игра уже шла) — а не «завтра».
SAME_DAY_GRACE_MIN = 15
# Сообщения, последняя активность которых старше этого — не читаем вообще
# (чисто оптимизация: правило дат не пускает матчи дальше anchor + 24 ч,
# запас нужен только для явных дат вперёд).
HISTORY_HORIZON_HOURS = 72

# Мониторинг Фонбета после старта матча (лайв). Раньше окно было «20 минут» только
# на словах: планировщик напоминаний удалял прогноз через DELETE_AFTER_MINUTES (по
# умолчанию 5), и мониторинг обрывался. Теперь прогноз гарантированно живёт в
# расписании, пока идёт мониторинг: чистка и «уже прошёл» (storage) считаются от
# одного и того же значения — retention_minutes().
MONITOR_AFTER_START_MIN = 15


def retention_minutes(configured) -> int:
    """
    Сколько минут после старта матч остаётся в расписании: не меньше окна мониторинга
    (+1 минута запаса, чтобы последний тик мониторинга успел до чистки).
    DELETE_AFTER_MINUTES из окружения может только увеличить это значение.
    """
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 0
    return max(configured, MONITOR_AFTER_START_MIN + 1)


# Основания (basis) — пишутся в лог, в Prediction.date_basis и в реестр
BASIS_EXPLICIT = "explicit"
BASIS_SAME_DAY = "anchor:same-day"
BASIS_NEXT_EVENING = "anchor:next-day-evening-post"
BASIS_NEXT_NIGHT = "anchor:next-day-night-match"
BASIS_UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class DateHint:
    """Явная дата из текста. Либо (day, month[, year]), либо rel_days (сегодня=0, завтра=1)."""
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None
    rel_days: Optional[int] = None

    def describe(self) -> str:
        if self.rel_days is not None:
            return {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(self.rel_days, f"+{self.rel_days}д")
        y = f".{self.year}" if self.year else ""
        return f"{self.day:02d}.{self.month:02d}{y}"


@dataclass
class Resolution:
    dt: Optional[datetime]   # naive UTC, None если дату установить нельзя
    basis: str
    reason: str = ""


def _tz(tz_offset: int) -> timezone:
    return timezone(timedelta(hours=tz_offset))


def to_local(dt_utc: datetime, tz_offset: int) -> datetime:
    """naive UTC → naive локальное время."""
    return dt_utc + timedelta(hours=tz_offset)


def local_to_utc(local_naive: datetime, tz_offset: int) -> datetime:
    """naive локальное → naive UTC."""
    return local_naive - timedelta(hours=tz_offset)


def _pick_year(day: int, month: int, anchor_date: date) -> Optional[date]:
    """Для даты без года берём год, при котором она ближе всего к дате anchor."""
    best = None
    for y in (anchor_date.year - 1, anchor_date.year, anchor_date.year + 1):
        try:
            cand = date(y, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - anchor_date).days) < abs((best - anchor_date).days):
            best = cand
    return best


def resolve_match_datetime(
    hour: int,
    minute: int,
    hint: Optional[DateHint],
    anchor_utc: datetime,
    tz_offset: int = 3,
) -> Resolution:
    """
    Дата+время матча по времени из текста, явной подсказке и моменту anchor
    (когда матч появился в сообщении: публикация или правка). Возвращает naive UTC.
    """
    anchor_local = to_local(anchor_utc, tz_offset)
    anchor_date = anchor_local.date()

    # 1. Явная дата
    if hint is not None:
        if hint.rel_days is not None:
            d = anchor_date + timedelta(days=hint.rel_days)
        else:
            d = _pick_year(hint.day, hint.month, anchor_date) if hint.year is None else None
            if hint.year is not None:
                try:
                    d = date(hint.year, hint.month, hint.day)
                except ValueError:
                    d = None
            if d is None:
                return Resolution(None, BASIS_UNDETERMINED,
                                  f"некорректная дата в тексте: {hint.describe()}")
        dt_local = datetime.combine(d, time(hour, minute))
        return Resolution(local_to_utc(dt_local, tz_offset), BASIS_EXPLICIT,
                          f"явная дата {hint.describe()} → {d.isoformat()}")

    # 2. Ближайшее вхождение времени после публикации
    same_day = datetime.combine(anchor_date, time(hour, minute))
    if same_day >= anchor_local - timedelta(minutes=SAME_DAY_GRACE_MIN):
        return Resolution(local_to_utc(same_day, tz_offset), BASIS_SAME_DAY,
                          f"{hour:02d}:{minute:02d} не раньше момента {anchor_local:%d.%m %H:%M} → тот же день")

    next_day = same_day + timedelta(days=1)
    if anchor_local.hour >= EVENING_POST_FROM_HOUR:
        return Resolution(local_to_utc(next_day, tz_offset), BASIS_NEXT_EVENING,
                          f"{hour:02d}:{minute:02d} раньше вечерней публикации {anchor_local:%d.%m %H:%M} → следующий день")
    if hour < NIGHT_UNTIL_HOUR:
        return Resolution(local_to_utc(next_day, tz_offset), BASIS_NEXT_NIGHT,
                          f"{hour:02d}:{minute:02d} — ночной матч после полуночи → следующий день")

    return Resolution(
        None, BASIS_UNDETERMINED,
        f"{hour:02d}:{minute:02d} раньше момента {anchor_local:%d.%m %H:%M}, публикация дневная и матч не ночной: "
        f"либо сегодня (уже прошёл), либо завтра — достоверно не определить",
    )


def can_still_matter(created_at: datetime, edited_at: Optional[datetime], now: datetime) -> bool:
    """
    Стоит ли вообще читать сообщение. Это НЕ решение об актуальности матчей
    (оно принимается по дате каждого матча) — только отсечка заведомо мёртвой
    истории канала: сообщение, которое не создавалось и не правилось дольше
    HISTORY_HORIZON_HOURS, ничего нового дать не может.
    """
    last_activity = max(created_at, edited_at) if edited_at else created_at
    return last_activity >= now - timedelta(hours=HISTORY_HORIZON_HOURS)
