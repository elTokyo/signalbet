import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import Prediction

# Триггеры начала нового прогноза
TRIGGERS = ("футбол.", "soccer.", "футбол ", "soccer ")


def parse_predictions(text: str, tz_offset: int = 3, source: str = "manual") -> list[Prediction]:
    """
    Парсер прогнозов:
    1. Длинные тире (3+ подряд) разделяют блоки прогнозов на разное время
    2. Внутри блока новый прогноз начинается со слов "Футбол" или "Soccer"
    3. Каждый прогноз содержит время в формате HH-MM или HH:MM
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')

    # Заменяем разделитель из тире на одинаковый маркер
    normalized = re.sub(r'[-–—]{3,}', '\n', normalized)

    # Склеиваем всё в одну строку через пробел, чтобы было удобно искать триггеры
    flat = re.sub(r'\s+', ' ', normalized).strip()

    # Разбиваем по триггерам, сохраняя их в начале каждого фрагмента
    # Паттерн ищет "Футбол." или "Soccer." как начало (case-insensitive)
    parts = re.split(r'(?i)(?=(?:футбол|soccer)[\.\s])', flat)

    predictions = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Должен начинаться с триггера
        if not part.lower().startswith(("футбол", "soccer")):
            continue
        pred = _parse_one(part, tz_offset, source)
        if pred:
            predictions.append(pred)

    return predictions


def _parse_one(text: str, tz_offset: int, source: str) -> Optional[Prediction]:
    """Парсит одну строку прогноза. Время обязательно."""
    text = text.strip().rstrip('.')

    # Ищем первое валидное время: 18-00, 19:00, 2-30
    hour = minute = None
    time_pos = None
    for m in re.finditer(r'\b(\d{1,2})[-:](\d{2})\b', text):
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            hour, minute = h, mn
            time_pos = m
            break

    if hour is None:
        return None

    # Конвертация локального времени → UTC
    user_tz = timezone(timedelta(hours=tz_offset))
    local_now = datetime.now(timezone.utc).astimezone(user_tz)
    today = local_now.date()

    try:
        local_dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=user_tz)
    except ValueError:
        return None

    match_time_utc = local_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # Логика выбора дня зависит от источника:
    # - source="discord" (автопарсинг): если матч уже начался — ИГНОРИРУЕМ (None),
    #   чтобы старые прогнозы из неубранного DC-канала не засоряли список.
    # - source="manual" (ручной /add): НЕ игнорируем — пользователь знает что делает.
    #   Только ночные матчи переносим на завтра.
    utc_now = datetime.utcnow()

    if match_time_utc < utc_now - timedelta(minutes=2):
        is_night_match = hour < 6
        is_evening_now = local_now.hour >= 18

        if is_night_match and is_evening_now:
            # Ночной матч на завтра (для любого источника)
            match_time_utc += timedelta(days=1)
        elif source == "discord":
            # Сыгранный матч из Discord — игнорируем
            return None
        else:
            # Ручной /add: матч в прошлом — всё равно добавляем на сегодня.
            # Возможно пользователь тестирует или добавляет недавно начавшийся матч.
            pass

    return Prediction(
        text=text,
        match_time=match_time_utc,
        source=source,
    )


def _local_time_str(pred: Prediction, tz_offset: int) -> str:
    return (pred.match_time + timedelta(hours=tz_offset)).strftime("%H:%M")


def format_prediction_line(pred: Prediction, tz_offset: int, index: int = None) -> str:
    num = f"{index}. " if index else ""
    return f"{num}{pred.text}"


def format_time_local(pred: Prediction, tz_offset: int) -> str:
    return _local_time_str(pred, tz_offset)


def format_reminder(pred: Prediction, minutes_before: int) -> str:
    emoji = "🔥" if minutes_before <= 5 else "⏰"
    return f"{emoji} Через {minutes_before} мин!\n\n{pred.text}"
