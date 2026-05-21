import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import Prediction


def parse_predictions(text: str, tz_offset: int = 3, source: str = "manual") -> list[Prediction]:
    """
    Разбивает текст на блоки по пустым строкам — каждый блок = один прогноз.
    Из блока вытаскивается только время (для планировщика), остальное сохраняется as-is.

    Пример блока (любые 1-N строк):
        Soccer. Brazil. Acreano U20. 2-00
        Santa Cruz Acre U20 — Independencia FC U20
        ф1-4,5
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", normalized.strip())

    predictions = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        pred = _parse_block(block, tz_offset, source)
        if pred:
            predictions.append(pred)
    return predictions


def _parse_block(block: str, tz_offset: int, source: str) -> Optional[Prediction]:
    # Убираем нумерацию "1." / "1)" в самом начале блока
    cleaned = re.sub(r"^\d+[\.\)]\s*", "", block.strip())

    # Склеиваем многострочный блок в одну строку через пробел
    single_line = " ".join(line.strip() for line in cleaned.split("\n") if line.strip())

    # Ищем первое валидное время: 2-00, 14:30
    hour = minute = None
    for m in re.finditer(r"\b(\d{1,2})[-:](\d{2})\b", single_line):
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            hour, minute = h, mn
            break

    if hour is None:
        return None

    # Конвертируем локальное время пользователя → UTC
    user_tz = timezone(timedelta(hours=tz_offset))
    local_now = datetime.now(timezone.utc).astimezone(user_tz)
    today = local_now.date()

    try:
        local_dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=user_tz)
    except ValueError:
        return None

    match_time_utc = local_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # Если матч уже прошёл сегодня — значит на завтра
    utc_now = datetime.utcnow()
    if match_time_utc < utc_now - timedelta(minutes=5):
        match_time_utc += timedelta(days=1)

    return Prediction(
        text=single_line,
        match_time=match_time_utc,
        source=source,
    )


def format_time_local(pred: Prediction, tz_offset: int) -> str:
    return (pred.match_time + timedelta(hours=tz_offset)).strftime("%H:%M")


def format_prediction_line(pred: Prediction, tz_offset: int, index: int = None) -> str:
    num = f"{index}. " if index else ""
    return f"{num}{pred.text}"


def format_reminder(pred: Prediction, minutes_before: int) -> str:
    emoji = "🔥" if minutes_before <= 5 else "⏰"
    return f"{emoji} Через {minutes_before} мин!\n\n{pred.text}"
