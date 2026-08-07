import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from models import Prediction

# ===========================================================================
# Публичные константы
# ===========================================================================

# Триггеры начала нового прогноза (старый формат)
TRIGGERS = ("футбол.", "soccer.", "футбол ", "soccer ")

# ===========================================================================
# Регэкспы форматов
# ===========================================================================

# Старый формат целиком начинается с "Футбол"/"Soccer" (без учёта регистра)
_OLD_FORMAT_TRIGGER = re.compile(r'(?i)^\s*(?:футбол|soccer)[.\s]')

# Новый формат: "Страна.[ Лига.][ - Подлига.] <любой текст без точек> HH-MM/HH:MM ..."
#
# Примеры:
#   "Poland. 4 Liga. Warmia-Masuria Voivodeship 16-00 Sokol Ostroda — DKS Dobre Miasto 7+"
#   "Belarus. Second League 18-30 Turkspor Belarus — BFSO-Dynamo Minsk п2 5+"
#   "Australia. State League 2 - West Australian 14-30 Kalamunda City — Gosnells City п1 4+"
#   "Australia. State League 1 - West Australian. Women 14-30 Perth AFC W — Mandurah City W п2 4+"
#
# Заголовок — 1-3 сегмента; каждый сегмент завершается ЛИБО точкой ("Страна."),
# ЛИБО одиночным дефисом в пробелах (" - ", подлига без своей точки).
# Одиночный дефис безопасно отличим от:
#   - длинного тире команд "—"/"–" — это другие символы, не ASCII "-";
#   - блочного разделителя (3+ дефиса подряд) — он заменён на "\n" до этого места;
#   - дефиса внутри слова ("BFSO-Dynamo") — не совпадает с "\s-\s".
#
# Слово с заглавной: кириллица/латиница/цифра в начале, дальше буквы/дефисы;
# может состоять из нескольких слов через пробел ("4 Liga", "Second League",
# "Northern Ireland").
_WORD_GROUP = r'[A-ZА-Я0-9][\w-]*(?:\s+[A-ZА-Я0-9][\w-]*)*'
_HEADER_SEGMENT_SEP = r'(?:\.\s+|\s-\s)'   # конец сегмента: ". " или " - "
_NEW_FORMAT_HEADER = r'(?:' + _WORD_GROUP + _HEADER_SEGMENT_SEP + r'){1,3}'
_NEW_FORMAT_START = re.compile(
    r'(?:^|(?<=\s))'                    # начало текста или после пробела — граница блока
    r'(?=' + _NEW_FORMAT_HEADER +       # Страна.[ Лига.][ - Подлига.]  (1-3 сегмента)
    r'[^.]*?\d{1,2}[-:]\d{2}\b)'        # ...любой текст без точек... время
)

# Тире-разделители команд: новый блок НИКОГДА не начинается сразу после них.
_TEAM_DASHES = ("—", "–")

_TIME_RE = re.compile(r'\b(\d{1,2})[-:](\d{2})\b')


# ===========================================================================
# Точка входа
# ===========================================================================

def parse_predictions(text: str, tz_offset: int = 3, source: str = "manual") -> list[Prediction]:
    """
    Парсер прогнозов. Поддерживает два формата сообщений (в одном сообщении
    форматы не смешиваются — формат определяется по всему тексту целиком):

    Старый формат:
      1. Длинные тире (3+ подряд) разделяют блоки прогнозов на разное время
      2. Внутри блока новый прогноз начинается со слов "Футбол" или "Soccer"
      3. Каждый прогноз содержит время в формате HH-MM или HH:MM

    Новый формат:
      1. Длинные тире (3+ подряд) тоже разделяют блоки на разное время
      2. Внутри блока новый прогноз начинается с "Страна." / "Страна. Лига."
      3. Время — как и в старом формате, HH-MM или HH:MM
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')
    # Заменяем разделитель из тире на одинаковый маркер (общее для обоих форматов)
    normalized = re.sub(r'[-–—]{3,}', '\n', normalized)
    # Склеиваем всё в одну строку через пробел, чтобы было удобно искать триггеры
    flat = re.sub(r'\s+', ' ', normalized).strip()
    if not flat:
        return []

    if _OLD_FORMAT_TRIGGER.match(flat):
        parts = _split_old_format(flat)
    else:
        parts = _split_new_format(flat)

    predictions = []
    for part in parts:
        pred = _parse_one(part, tz_offset, source)
        if pred:
            predictions.append(pred)
    return predictions


# ===========================================================================
# Разбиение текста на блоки
# ===========================================================================

def _split_old_format(flat: str) -> list[str]:
    """Старый формат: делит по вхождениям 'Футбол'/'Soccer'."""
    parts = re.split(r'(?i)(?=(?:футбол|soccer)[.\s])', flat)
    result = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not part.lower().startswith(("футбол", "soccer")):
            continue
        result.append(part)
    return result


def _prev_non_space(flat: str, pos: int) -> str:
    """Ближайший непробельный символ слева от pos (или '' в начале текста)."""
    i = pos - 1
    while i >= 0 and flat[i] == ' ':
        i -= 1
    return flat[i] if i >= 0 else ''


def _split_new_format(flat: str) -> list[str]:
    """
    Новый формат: делит по вхождениям 'Страна.[ Лига.]'.

    Работает в три шага:
      1. Находим ВСЕ позиции, где lookahead похож на начало блока.
      2. Отбрасываем позиции сразу после тире команд ("—"/"–") — это вторая
         команда предыдущего матча, а не начало нового блока (например
         "... Ilirija U19 — NK Aluminij U19 Finland. ...").
      3. Отбрасываем позиции, попавшие "посреди" заголовка предыдущего
         принятого блока (например "Liga." внутри "Peru. Liga Nacional. ...").
    """
    candidates = []
    for m in _NEW_FORMAT_START.finditer(flat):
        pos = m.start()
        if _prev_non_space(flat, pos) in _TEAM_DASHES:
            continue  # ложный старт на второй команде предыдущего матча
        candidates.append(pos)
    if not candidates:
        return []

    valid_starts = [candidates[0]]
    for pos in candidates[1:]:
        prev = valid_starts[-1]
        header_m = re.match(_NEW_FORMAT_HEADER, flat[prev:])
        header_end = prev + header_m.end() if header_m else prev
        if pos >= header_end:
            valid_starts.append(pos)
        # иначе pos — ложный старт внутри заголовка предыдущего блока

    result = []
    for i, pos in enumerate(valid_starts):
        end = valid_starts[i + 1] if i + 1 < len(valid_starts) else len(flat)
        block = flat[pos:end].strip()
        if block:
            result.append(block)
    return result


# ===========================================================================
# Разбор одного блока
# ===========================================================================

def _parse_one(text: str, tz_offset: int, source: str) -> Optional[Prediction]:
    """Парсит одну строку прогноза. Время обязательно."""
    text = text.strip().rstrip('.')

    # Ищем первое валидное время: 18-00, 19:00, 2-30
    hour = minute = None
    for m in _TIME_RE.finditer(text):
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            hour, minute = h, mn
            break
    if hour is None:
        return None

    # Конвертация локального времени → UTC
    user_tz = timezone(timedelta(hours=tz_offset))
    local_now = datetime.now(timezone.utc).astimezone(user_tz)
    today = local_now.date()
    try:
        local_dt = datetime(today.year, today.month, today.day,
                            hour, minute, tzinfo=user_tz)
    except ValueError:
        return None
    match_time_utc = local_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # Логика выбора дня зависит от источника:
    # - source="discord" (автопарсинг): если матч уже начался — ИГНОРИРУЕМ,
    #   чтобы старые прогнозы из неубранного DC-канала не засоряли список.
    # - source="manual" (ручной /add): НЕ игнорируем — пользователь знает,
    #   что делает. Только ночные матчи переносим на завтра.
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    if match_time_utc < utc_now - timedelta(minutes=2):
        is_night_match = hour < 6
        is_evening_now = local_now.hour >= 18
        if is_night_match and is_evening_now:
            # Ночной матч — на завтра (для любого источника)
            match_time_utc += timedelta(days=1)
        elif source == "discord":
            # Сыгранный матч из Discord — игнорируем
            return None
        else:
            # Ручной /add: матч в прошлом — всё равно добавляем на сегодня
            # (пользователь тестирует или добавляет недавно начавшийся матч).
            pass

    return Prediction(
        text=text,
        match_time=match_time_utc,
        source=source,
    )


# ===========================================================================
# Форматирование вывода
# ===========================================================================

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
