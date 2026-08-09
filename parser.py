import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import Prediction

# Триггеры начала нового прогноза (старый формат)
TRIGGERS = ("футбол.", "soccer.", "футбол ", "soccer ")

# Старый формат целиком начинается с "Футбол"/"Soccer" (без учёта регистра)
_OLD_FORMAT_TRIGGER = re.compile(r'(?i)^\s*(?:футбол|soccer)[\.\s]')

# Новый формат: "Страна.[ Лига.][ - Подлига.] <любой текст без точек> HH-MM/HH:MM ..."
# Примеры:
#   "Poland. 4 Liga. Warmia-Masuria Voivodeship 16-00 Sokol Ostroda — DKS Dobre Miasto 7+"      (2 точки)
#   "Belarus. Second League 18-30 Turkspor Belarus — BFSO-Dynamo Minsk п2 5+"                    (1 точка)
#   "Australia. State League 2 - West Australian 14-30 Kalamunda City — Gosnells City п1 4+"     (точка + дефис)
#   "Australia. State League 1 - West Australian. Women 14-30 Perth AFC W — Mandurah City W п2 4+" (точка + дефис + точка)
# Заголовок — это 1-3 сегмента, каждый сегмент завершается ЛИБО точкой ("Страна."),
# ЛИБО одиночным дефисом в пробелах (" - ", доп. "подлига" без своей точки перед следующим
# сегментом). Одиночный дефис здесь безопасно отличим от:
#   - длинного тире команд "—"/"–" — это другие символы, не ASCII "-";
#   - блочного разделителя (3+ дефиса подряд) — тот уже заменён на "\n" до сюда;
#   - дефиса внутри слова ("BFSO-Dynamo", без пробелов вокруг) — не совпадает с "\s-\s".
# Команды всегда разделены тире "—", но тире НЕ используется как обязательное условие
# границы — если у одного блока тире вдруг нет, это не должно ломать поиск соседних блоков.
# Слово с заглавной = кириллица/латиница/цифра в начале, дальше любые буквы/дефисы,
# может состоять из нескольких слов через пробел (например "4 Liga", "La Liga", "Second League").
# Круглые скобки допущены ВНУТРИ слова (не как отдельный токен) — они встречаются
# в реальных названиях лиг/дивизионов сразу после первого слова заголовка, например
# "Myanmar (Burma). Youth League U20" или "Australia. Premier League - Northern
# Territory (reserves)". Без этого паттерн обрывался на "Myanmar", не находил
# ожидаемый разделитель сегмента (". "/" - ") сразу после него из-за скобки,
# и вся строка считалась продолжением предыдущего блока, а не новым заголовком —
# из-за чего два разных прогноза (Belarus + Myanmar) склеивались в один.
_WORD_GROUP = r'[A-ZА-Я0-9\(][\w\-\(\)]*(?:\s[A-ZА-Я0-9\(][\w\-\(\)]*)*'
_HEADER_SEGMENT_SEP = r'(?:\.\s+|\s-\s)'   # конец сегмента: ". " или " - "
_NEW_FORMAT_HEADER = r'(?:' + _WORD_GROUP + _HEADER_SEGMENT_SEP + r'){1,3}'  # Страна.[ Лига.][ - Подлига.]


def parse_predictions(text: str, tz_offset: int = 3, source: str = "manual") -> list[Prediction]:
    """
    Парсер прогнозов. Поддерживает два формата сообщений (не смешиваются в одном
    сообщении — формат определяется по всему тексту целиком):

    Старый формат:
    1. Длинные тире (3+ подряд) разделяют блоки прогнозов на разное время
    2. Внутри блока новый прогноз начинается со слов "Футбол" или "Soccer"
    3. Каждый прогноз содержит время в формате HH-MM или HH:MM

    Новый формат:
    1. Длинные тире (3+ подряд) тоже разделяют блоки на разное время
    2. Внутри блока новый прогноз начинается с "Страна." или "Страна. Лига." (1-2 точки)
    3. Время — как и в старом формате, HH-MM или HH:MM

    ВАЖНО про переносы строк: реальные сообщения из Discord построчные —
    лига/время, потом команды, потом (опционально) ставка — БЕЗ пустой строки
    перед следующим прогнозом. Поэтому перенос строки — единственный надёжный
    сигнал границы между прогнозами, и мы его не теряем: разбиваем по строкам
    и ищем начало нового блока только в начале строки (см. _split_old_format /
    _split_new_format). Раньше весь текст сразу схлопывался в одну строку через
    пробел, из-за чего название второй команды (например "Kazincbarcikai SC" —
    с заглавных букв, как и заголовок) можно было спутать с началом следующего
    блока, и парсер обрезал прогноз посередине, склеивая хвост со следующим.
    """
    normalized = text.replace('\r\n', '\n').replace('\r', '\n')

    # Заменяем разделитель из тире на одинаковый маркер (общее для обоих форматов)
    normalized = re.sub(r'[-–—]{3,}', '\n', normalized)

    if not normalized.strip():
        return []

    # Для определения формата (старый/новый) достаточно взглянуть на текст в целом
    flat_probe = re.sub(r'\s+', ' ', normalized).strip()
    is_old_format = bool(_OLD_FORMAT_TRIGGER.match(flat_probe))

    lines = [ln.strip() for ln in normalized.split('\n')]
    lines = [ln for ln in lines if ln]  # убираем пустые строки

    if is_old_format:
        parts = _split_old_format(lines)
    else:
        parts = _split_new_format(lines)

    predictions = []
    for part in parts:
        pred = _parse_one(part, tz_offset, source)
        if pred:
            predictions.append(pred)

    return predictions


_OLD_FORMAT_LINE_START = re.compile(r'(?i)^(?:футбол|soccer)[\.\s]')

# Заголовок нового прогноза должен начинаться В НАЧАЛЕ СТРОКИ — это то, что
# в реальных Discord-сообщениях отличает "Jordan. First Division. Women" (новая
# строка, новый прогноз) от "Kazincbarcikai SC" (конец строки с командами
# предыдущего прогноза, тоже с заглавных букв, но НЕ в начале своей строки).
_NEW_FORMAT_LINE_START = re.compile(r'^' + _NEW_FORMAT_HEADER)


_OLD_FORMAT_INLINE_TRIGGER = re.compile(r'(?i)(?<=\s)(?:футбол|soccer)[\.\s]')


def _split_old_format(lines: list[str]) -> list[str]:
    """
    Старый формат: новый прогноз начинается с 'Футбол'/'Soccer'. Триггер —
    фиксированное слово (не открытая грамматика заголовка, как в новом
    формате), поэтому в отличие от _split_new_format искать его безопасно
    в любом месте строки, не только в начале — команда с названием
    "Футбол ..." практически невозможна, ложных срабатываний на названиях
    команд не бывает. Это восстанавливает старое поведение (весь текст
    целиком) для сообщений, слитых в одну строку без переносов, и
    одновременно уважает построчную структуру там, где она есть.
    """
    blocks = []
    current = []
    for raw_line in lines:
        line = re.sub(r'[ \t]+', ' ', raw_line).strip()
        if not line:
            continue

        # Строка может содержать несколько триггеров подряд (сообщение
        # слито в одну строку) — режем по каждому вхождению.
        cut_points = [m.start() for m in _OLD_FORMAT_INLINE_TRIGGER.finditer(line)]
        chunks = []
        prev = 0
        for pos in cut_points:
            chunks.append(line[prev:pos].strip())
            prev = pos
        chunks.append(line[prev:].strip())
        chunks = [c for c in chunks if c]

        for chunk in chunks:
            if _OLD_FORMAT_LINE_START.match(chunk):
                if current:
                    blocks.append(' '.join(current))
                current = [chunk]
            elif current:
                current.append(chunk)
            # чанки до первого валидного заголовка отбрасываются
    if current:
        blocks.append(' '.join(current))
    return blocks


def _split_new_format(lines: list[str]) -> list[str]:
    """
    Новый формат: новый прогноз начинается с 'Страна.[ Лига.]... HH-MM ...'.

    Заголовок ищем ДВУМЯ способами одновременно, потому что реальные
    Discord-сообщения бывают и построчными (лига/время, команды, ставка —
    каждое на своей строке), и слитыми в один абзац (весь прогноз одной
    строкой, несколько прогнозов подряд без переносов вовсе):
    1. В начале очередной строки (естественная граница построчного формата).
    2. В любом месте ВНУТРИ строки — но только если накопленный текущий
       блок уже "закрыт" (см. _block_is_closed): у него уже есть и время,
       и обе команды. Это тот же сигнал, что раньше использовался для
       всего текста целиком, но здесь применяется точечно — только когда
       переноса строки не случилось, а не как основной механизм. Именно
       "закрытость" (не просто заглавная буква) отличает реальный новый
       заголовок от названия команды предыдущего прогноза ("Kazincbarcikai
       SC") — та тоже с заглавной, но стоит ДО тире, а не после него.
    """
    blocks = []
    current = []
    for raw_line in lines:
        # Схлопываем внутристрочные пробелы/табы (в Discord между лигой и
        # временем часто стоит несколько пробелов/таб — раньше это убирал
        # общий re.sub на всём тексте, теперь делаем это здесь, построчно).
        line = re.sub(r'[ \t]+', ' ', raw_line).strip()
        if not line:
            continue

        for chunk in _split_line_on_closed_headers(line):
            is_header_chunk = bool(_NEW_FORMAT_LINE_START.match(chunk))

            if is_header_chunk and _block_is_closed(current):
                if current:
                    blocks.append(' '.join(current))
                current = [chunk]
            elif current:
                current.append(chunk)
            elif is_header_chunk:
                # первая строка блока, даже если время придёт отдельной строкой позже
                current = [chunk]
            # чанки до первого валидного заголовка отбрасываются
    if current:
        blocks.append(' '.join(current))
    return blocks


def _split_line_on_closed_headers(line: str) -> list[str]:
    """
    Разбивает ОДНУ строку на куски там, где внутри неё встречается новый
    заголовок после уже "закрытого" (время + обе команды) фрагмента.
    Нужно для редкого случая, когда несколько прогнозов слиты в одну строку
    без переноса вообще (например текст вставлен без сохранения форматирования).
    Для обычных построчных сообщений просто вернёт [line] без изменений —
    внутри одной строки Discord-сообщения второй заголовок не встречается.
    """
    matches = list(re.finditer(r'(?<=\s)' + _NEW_FORMAT_HEADER, line))
    if not matches:
        return [line]

    cut_points = []
    for m in matches:
        prefix = line[:m.start()]
        if _block_is_closed([prefix]):
            cut_points.append(m.start())

    if not cut_points:
        return [line]

    chunks = []
    prev = 0
    for pos in cut_points:
        chunks.append(line[prev:pos].strip())
        prev = pos
    chunks.append(line[prev:].strip())
    return [c for c in chunks if c]


def _block_is_closed(current: list[str]) -> bool:
    """
    True если накопленный текст уже содержит и время, и обе команды —
    то есть прогноз уже "полный", и следующий встреченный заголовок точно
    относится к НОВОМУ прогнозу, а не является названием команды/лиги
    текущего (например "Kazincbarcikai SC" перед "Jordan. First Division").
    """
    if not current:
        return True
    joined = ' '.join(current)
    has_time = bool(re.search(r'\d{1,2}[-:]\d{2}\b', joined))
    has_teams = bool(re.search(r'\s[—–]\s', joined))
    return has_time and has_teams


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


def display_text_without_time(pred: Prediction, tz_offset: int) -> str:
    """
    Возвращает pred.text БЕЗ времени матча — для отображения рядом с уже
    отдельно показанным временем (например "⏰ 12:00" перед строкой прогноза
    в /list и в уведомлениях), где время иначе дублируется: один раз как
    "⏰ HH:MM" префикс, второй раз внутри самого текста (Discord-сообщения
    сами по себе идут в формате "Лига ... HH-MM ... команды ... ставка").

    ВАЖНО: это только для показа пользователю. pred.text в базе НЕ меняется —
    вся остальная логика (extract_teams_from_prediction, _match_key, проверка
    кривизны в fonbet.py) продолжает работать с оригинальным полным текстом.
    Это осознанное разделение: трогать сам pred.text рискованно, потому что
    он единственный источник, из которого заново парсятся команды/лига при
    каждой сверке с букмекером — здесь мы меняем только то, что видит человек.

    Убираем именно ТО время, которое соответствует pred.match_time (уже
    посчитанное через format_time_local), а не "первое похожее на время"
    вхождение в тексте — это безопаснее: числа вроде "4" в "4 Liga" или "20"
    в "U20" не подходят под точный паттерн HH-MM/HH:MM с границами не-цифра
    по краям, так что случайно задеть их нельзя. Если по какой-то причине
    искомое время в тексте не найдено (нестандартный формат сообщения),
    возвращаем текст как есть — не пытаемся угадывать другое время вместо него.
    """
    t = _local_time_str(pred, tz_offset)
    pattern = re.compile(r'(?<!\d)' + re.escape(t).replace(':', '[-:]') + r'(?!\d)')
    stripped, n = pattern.subn('', pred.text, count=1)
    if n == 0:
        return pred.text
    return re.sub(r'\s+', ' ', stripped).strip()


def format_reminder(pred: Prediction, minutes_before: int) -> str:
    emoji = "🔥" if minutes_before <= 5 else "⏰"
    return f"{emoji} Через {minutes_before} мин!\n\n{pred.text}"
