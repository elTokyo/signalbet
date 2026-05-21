"""
Fonbet API клиент:
- Автодетект рабочего URL (главная страница → парсинг → кэш на час) с fallback
- Запрос списка событий с коэффициентами
- Fuzzy-матчинг прогнозов с событиями Фонбета
"""
import re
import time
import logging
import threading
import requests
from typing import Optional
from fuzzywuzzy import fuzz

logger = logging.getLogger(__name__)

# Fallback-хосты (актуальные на момент написания)
FALLBACK_HOSTS = [
    "line-lb61-w.bk6bba-resources.com",
    "line-lb54-w.bk6bba-resources.com",
    "line-lb01-w.bk6bba-resources.com",
    "line-lb02-w.bk6bba-resources.com",
    "line-lb03-w.bk6bba-resources.com",
]

# Endpoint со списком событий.
# version=0 заставляет вернуть ПОЛНЫЙ список, а не дельту изменений.
# Без version отдаются только что-то изменившиеся события (обычно только live).
ENDPOINT_PATH = "/ma/events/list?lang=en&scopeMarket=1600&version=0"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

FUZZY_THRESHOLD = 75   # минимальный % совпадения команд
CACHE_TTL = 3600       # обновляем URL раз в час

_url_cache: dict = {"url": None, "ts": 0}
_url_lock = threading.RLock()


# ── Автодетект URL ────────────────────────────────────────────────────────────

def _detect_url_from_homepage() -> Optional[str]:
    """Парсит fonbet.ru и пытается найти актуальный API-хост в скриптах/HTML."""
    try:
        r = requests.get("https://www.fonbet.ru/", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None
        # Ищем все упоминания хостов формата line-lbXX-w.bk6bba-resources.com
        matches = re.findall(r'line-lb\d+-w\.bk6bba-resources\.com', r.text)
        if matches:
            # Берём самый частый (надёжнее всего)
            host = max(set(matches), key=matches.count)
            logger.info(f"Fonbet host автодетект: {host}")
            return host
    except Exception as e:
        logger.warning(f"Автодетект Fonbet URL не сработал: {e}")
    return None


def _try_host(host: str) -> bool:
    """Проверяет что хост отвечает 200 на API."""
    try:
        url = f"https://{host}{ENDPOINT_PATH}"
        r = requests.get(url, headers=HEADERS, timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def get_working_host() -> Optional[str]:
    """Возвращает рабочий хост Фонбета. Кэш на 1 час."""
    with _url_lock:
        now = time.time()
        if _url_cache["url"] and (now - _url_cache["ts"]) < CACHE_TTL:
            return _url_cache["url"]

        # 1) Пробуем автодетект
        host = _detect_url_from_homepage()
        if host and _try_host(host):
            _url_cache.update({"url": host, "ts": now})
            return host

        # 2) Fallback — перебираем известные хосты
        for h in FALLBACK_HOSTS:
            if _try_host(h):
                logger.info(f"Fonbet host fallback: {h}")
                _url_cache.update({"url": h, "ts": now})
                return h

        logger.error("Все Fonbet хосты недоступны!")
        return None


# ── Запрос событий ───────────────────────────────────────────────────────────

def fetch_events() -> list[dict]:
    """
    Запрашивает все футбольные события (prematch + live) с коэффициентами П1/П2.
    Возвращает список словарей: {team1, team2, is_live, odd_p1, odd_p2}
    """
    host = get_working_host()
    if not host:
        return []

    url = f"https://{host}{ENDPOINT_PATH}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"Fonbet API {r.status_code}")
            # Сбрасываем кэш URL — возможно хост умер
            with _url_lock:
                _url_cache["ts"] = 0
            return []
        data = r.json()
    except Exception as e:
        logger.error(f"Fonbet fetch error: {e}")
        with _url_lock:
            _url_cache["ts"] = 0
        return []

    raw_events = data.get("events", [])
    logger.info(f"Fonbet raw events: {len(raw_events)}")

    if not raw_events:
        logger.warning(f"Fonbet: нет events. Ключи ответа: {list(data.keys())[:20]}")
        return []

    # Берём ВСЕ события у которых есть две команды.
    # Фильтр по виду спорта не нужен: бот ищет конкретные команды через fuzzy,
    # и футбольные названия сматчатся только с футбольными событиями.
    # У Фонбета прематч-матчи часто не имеют прямого sportId (он у турнира-родителя),
    # поэтому фильтрация по sportId выбрасывала почти все события.
    events_map = {}
    for ev in raw_events:
        team1 = ev.get("team1") or ev.get("name1") or ""
        team2 = ev.get("team2") or ev.get("name2") or ""
        # Нужны именно матчи команда-против-команды (оба поля заполнены)
        if not team1 or not team2:
            continue
        events_map[ev.get("id")] = {
            "team1": team1,
            "team2": team2,
            "is_live": bool(ev.get("live") or ev.get("inLive") or ev.get("isLive")),
            "odd_p1": None,
            "odd_p2": None,
        }

    logger.info(f"Fonbet events с двумя командами: {len(events_map)}")

    # Парсим customFactors — там лежат коэффициенты
    # Структура: customFactors -> [{e: eventId, factors: [{f: factorType, v: value}, ...]}]
    # Тип фактора 921 (Win1) и 923 (Win2) — победа команды 1 и 2
    for entry in data.get("customFactors", []):
        eid = entry.get("e")
        if eid not in events_map:
            continue
        for f in entry.get("factors", []):
            ftype = f.get("f")
            val = f.get("v")
            if val is None:
                continue
            # Стандартные типы Win1/Win2 в Fonbet
            if ftype == 921:   # П1
                events_map[eid]["odd_p1"] = val
            elif ftype == 923: # П2
                events_map[eid]["odd_p2"] = val

    result = list(events_map.values())
    logger.info(f"Fonbet: получено {len(result)} событий")
    return result


# ── Матчинг прогноза с событием ──────────────────────────────────────────────

def extract_teams_from_prediction(text: str) -> tuple[str, str]:
    """
    Вытаскивает названия команд из текста прогноза.
    Пример: 'Футбол. Бразилия. 22-00 Атлетико Клипер (20) — Фаст Клубе (20) п2 3+'
    → ('Атлетико Клипер (20)', 'Фаст Клубе (20)')
    """
    # 1. Убираем всё до времени включительно
    cleaned = re.sub(r'^.*?\d{1,2}[-:]\d{2}\s*', '', text).strip()

    # 2. Ищем разделитель команд
    team1, team2 = "", ""
    for sep in [' — ', ' – ', ' - ', '—', '–']:
        if sep in cleaned:
            parts = cleaned.split(sep, 1)
            team1 = parts[0].strip()
            team2_raw = parts[1].strip()
            # Убираем ставку (последние короткие слова с цифрами)
            words = team2_raw.split()
            while words:
                last = words[-1].rstrip('.,')
                # Ставка: короткие токены типа "п1", "ф1-4,5", "3+", "ТБ2.5", "см" "стату"
                if (re.match(r'^[\wфФпПтТбБмМxXхХ]{0,4}[\d\+\-\.,]+$', last) or
                    re.match(r'^(см|стату|мб|вынос|получше|вторые)$', last, re.IGNORECASE)):
                    words.pop()
                else:
                    break
            team2 = ' '.join(words).strip() or team2_raw
            break

    return team1, team2


def find_matching_event(pred_text: str, events: list[dict]) -> Optional[dict]:
    """
    Ищет матч из прогноза среди событий Фонбета.
    Возвращает событие если найдено (с коэффициентами), иначе None.
    """
    pred_t1, pred_t2 = extract_teams_from_prediction(pred_text)
    if not pred_t1:
        return None

    best_score = 0
    best_event = None

    p1 = pred_t1.lower()
    p2 = pred_t2.lower() if pred_t2 else ""

    for ev in events:
        e1 = ev["team1"].lower()
        e2 = ev["team2"].lower()

        if p2:
            direct = (fuzz.partial_ratio(p1, e1) + fuzz.partial_ratio(p2, e2)) / 2
            reverse = (fuzz.partial_ratio(p1, e2) + fuzz.partial_ratio(p2, e1)) / 2
            score = max(direct, reverse)
        else:
            score = fuzz.partial_ratio(p1, e1)

        if score > best_score:
            best_score = score
            best_event = ev

    if best_score >= FUZZY_THRESHOLD:
        logger.info(
            f"Fonbet match [{best_score:.0f}%]: "
            f"'{pred_t1} vs {pred_t2}' → "
            f"'{best_event['team1']} vs {best_event['team2']}' "
            f"({'LIVE' if best_event['is_live'] else 'Prematch'})"
        )
        return best_event

    return None
