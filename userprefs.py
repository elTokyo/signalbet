"""
Личные фильтры уведомлений и статус матча для /today. Чистые функции (без Gist/Telegram).

  • тихие часы — не слать уведомления в заданные часы (по местному времени пользователя).
"""
from datetime import datetime, timedelta
from typing import Optional


def in_quiet_hours(quiet_from: Optional[int], quiet_to: Optional[int], now_utc: datetime, tz_offset: int) -> bool:
    """Попадает ли сейчас (по местному времени) в тихие часы. Поддерживает переход через полночь (23 → 8)."""
    if quiet_from is None or quiet_to is None or quiet_from == quiet_to:
        return False
    h = (now_utc + timedelta(hours=tz_offset)).hour
    if quiet_from < quiet_to:
        return quiet_from <= h < quiet_to
    return h >= quiet_from or h < quiet_to


def user_allows(settings, now_utc: datetime) -> bool:
    """Можно ли сейчас слать этому пользователю уведомление (не попадает ли в его тихие часы)."""
    return not in_quiet_hours(settings.quiet_from, settings.quiet_to, now_utc, settings.timezone_offset)


def parse_quiet_args(args: list[str]):
    """
    '/quiet 23 8' → ('set', 23, 8); '/quiet off' → ('off', None, None);
    без аргументов → ('show', None, None); мусор → ('error', None, None).
    """
    if not args:
        return "show", None, None
    if len(args) == 1 and args[0].lower() in ("off", "выкл", "0", "нет"):
        return "off", None, None
    if len(args) == 2:
        try:
            a, b = int(args[0]), int(args[1])
        except ValueError:
            return "error", None, None
        if 0 <= a <= 23 and 0 <= b <= 23 and a != b:
            return "set", a, b
    return "error", None, None


def match_status(pred, now_utc: datetime) -> tuple[str, str]:
    """(эмодзи, текст) — что бот знает про матч на Фонбете."""
    mins = (pred.match_time - now_utc).total_seconds() / 60
    if pred.fonbet_state == "live" or pred.fonbet_notified_live:
        return "🔴", "в лайве"
    if pred.fonbet_matched or pred.fonbet_notified_prematch:
        return "📋", "в линии"
    if mins > 60:
        return "⏳", "ещё рано (поиск за 60 мин до старта)"
    if mins < -10:
        return "❓", "на Фонбете не найден"
    return "🔎", "ищу на Фонбете"
