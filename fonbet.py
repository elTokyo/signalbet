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

FUZZY_THRESHOLD = 80   # минимальный % совпадения команд (token_sort_ratio)

# ── Коды факторов Фонбета (определены эмпирически через /factors + сверку с сайтом) ──
# Победители основного времени
CODE_WIN1 = 921   # П1
CODE_WIN2 = 923   # П2

# Форы МАТЧА (основное время). Один код может давать разный pt в разных матчах,
# поэтому при поиске форы фильтруем И по коду из этого набора, И по значению pt.
HANDICAP_MATCH_TEAM1 = {927, 989, 910, 1569, 1672}  # форы Ф1 матча (pt отрицательный/0)
HANDICAP_MATCH_TEAM2 = {928, 991, 912, 1572, 1675}  # форы Ф2 матча (зеркало)

# Форы 1-го ТАЙМА. Ограничены значениями 0/±1/±1.5.
HANDICAP_1STHALF_TEAM1 = {1672, 1678, 1681}  # внимание: 1672 — pt=0 встречается в обоих
HANDICAP_1STHALF_TEAM2 = {1675, 1677, 1680}

# Конкретные коды для значений 1-го тайма (по сверке скриншотов):
# 1678 = Ф1 -1 (1й тайм), 1681 = Ф1 -1.5 (1й тайм)
# 1677 = Ф2 +1 (1й тайм), 1680 = Ф2 +1.5 (1й тайм)
CODE_1STHALF_HANDICAP_T1_MINUS15 = 1681   # Ф1 -1.5 первого тайма (ключевой для СЛУЧАЯ 3)
CODE_1STHALF_HANDICAP_T2_MINUS15 = 1680   # Ф2 -1.5 первого тайма (зеркально +1.5 на Ф1)
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
        # ID лиги/турнира для ссылки.
        # У Фонбета поле sportId на МАТЧЕ фактически содержит ID лиги-сегмента
        # (например 124689 = "Бразилия. До 20 лет"), а НЕ вид спорта.
        # Это же подтверждается структурой URL: fon.bet/.../football/{sportId}/{id}
        league_id = (ev.get("sportId") or ev.get("parentId"))
        if not league_id:
            pids = ev.get("parentIds")
            if isinstance(pids, list) and pids:
                league_id = pids[0]

        events_map[ev.get("id")] = {
            "id": ev.get("id"),
            "league_id": league_id,
            # Время старта матча (Unix timestamp в секундах)
            "start_time": ev.get("startTime") or ev.get("start") or ev.get("time"),
            "team1": team1,
            "team2": team2,
            "is_live": bool(ev.get("live") or ev.get("inLive") or ev.get("isLive")),
            "odd_p1": None,
            "odd_p2": None,
            "factors": [],   # все факторы: [{f: код, v: кэф, pt: значение}, ...]
        }

    logger.info(f"Fonbet events с двумя командами: {len(events_map)}")

    # Парсим customFactors — там лежат коэффициенты
    # Структура: customFactors -> [{e: eventId, factors: [{f: factorType, v: value, pt: param}, ...]}]
    for entry in data.get("customFactors", []):
        eid = entry.get("e")
        if eid not in events_map:
            continue
        for f in entry.get("factors", []):
            ftype = f.get("f")
            val = f.get("v")
            if val is None:
                continue
            # Сохраняем фактор целиком для анализа фор
            events_map[eid]["factors"].append({
                "f": ftype,
                "v": val,
                "pt": f.get("pt"),
            })
            # Дублируем П1/П2 в отдельные поля для быстрого доступа
            if ftype == CODE_WIN1:
                events_map[eid]["odd_p1"] = val
            elif ftype == CODE_WIN2:
                events_map[eid]["odd_p2"] = val

    result = list(events_map.values())
    logger.info(f"Fonbet: получено {len(result)} событий")
    return result


def dump_event_factors(pred_text: str) -> Optional[dict]:
    """
    Диагностика: находит матч из прогноза на Фонбете и возвращает
    ВСЕ его факторы (коды рынков + коэффициенты) для определения кодов фор.

    Возвращает: {team1, team2, is_live, factors: [{f: код, v: кэф, pt: параметр}, ...]}
    или None если матч не найден.
    """
    host = get_working_host()
    if not host:
        return None

    url = f"https://{host}{ENDPOINT_PATH}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception as e:
        logger.error(f"dump_event_factors fetch error: {e}")
        return None

    # Находим нужный матч среди событий
    pred_t1, pred_t2 = extract_teams_from_prediction(pred_text)
    if not pred_t1:
        return None

    target_id = None
    target_info = None
    target_raw = None
    best_score = 0

    p1 = pred_t1.lower()
    p2 = pred_t2.lower() if pred_t2 else ""

    for ev in data.get("events", []):
        team1 = ev.get("team1") or ev.get("name1") or ""
        team2 = ev.get("team2") or ev.get("name2") or ""
        if not team1 or not team2:
            continue
        e1, e2 = team1.lower(), team2.lower()
        if p2:
            direct = (fuzz.token_sort_ratio(p1, e1) + fuzz.token_sort_ratio(p2, e2)) / 2
            reverse = (fuzz.token_sort_ratio(p1, e2) + fuzz.token_sort_ratio(p2, e1)) / 2
            score = max(direct, reverse)
        else:
            score = fuzz.token_sort_ratio(p1, e1)
        if score > best_score:
            best_score = score
            target_id = ev.get("id")
            target_raw = ev   # сохраняем сырое событие для диагностики
            target_info = {
                "team1": team1,
                "team2": team2,
                "is_live": bool(ev.get("live") or ev.get("inLive") or ev.get("isLive")),
            }

    if not target_id or best_score < FUZZY_THRESHOLD:
        return None

    # Сохраняем все поля события содержащие "id"/"time"/"parent" — для диагностики ссылки
    if target_raw:
        target_info["raw_fields"] = {
            k: v for k, v in target_raw.items()
            if any(s in k.lower() for s in ("id", "time", "parent", "tournament", "league", "sport"))
        }

    # Собираем ВСЕ факторы этого матча
    all_factors = []
    for entry in data.get("customFactors", []):
        if entry.get("e") != target_id:
            continue
        for f in entry.get("factors", []):
            all_factors.append({
                "f": f.get("f"),    # код рынка
                "v": f.get("v"),    # коэффициент
                "pt": f.get("pt"),  # параметр (значение форы/тотала)
                "p": f.get("p"),    # альтернативный параметр
            })

    target_info["factors"] = all_factors
    target_info["match_score"] = round(best_score)
    return target_info


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


def find_matching_event(pred_text: str, events: list[dict],
                        expected_utc=None, time_tolerance_min: int = 15) -> Optional[dict]:
    """
    Ищет матч из прогноза среди событий Фонбета.
    Возвращает событие если найдено (с коэффициентами), иначе None.

    Использует token_sort_ratio + проверку времени старта:
    - Если время старта Фонбета совпадает с expected_utc (±tolerance) — матч подтверждён,
      даже при чуть меньшем fuzzy-score.
    - Если время НЕ совпадает — требуется очень высокий fuzzy (исключает путаницу
      U20 с основным составом, которые играют в разное время).

    expected_utc — datetime (UTC, naive) ожидаемого времени матча из прогноза.
    """
    import datetime as _dt

    pred_t1, pred_t2 = extract_teams_from_prediction(pred_text)
    if not pred_t1:
        return None

    p1 = pred_t1.lower()
    p2 = pred_t2.lower() if pred_t2 else ""

    candidates = []  # (score, time_ok, event)

    for ev in events:
        e1 = ev["team1"].lower()
        e2 = ev["team2"].lower()

        if p2:
            direct = (fuzz.token_sort_ratio(p1, e1) + fuzz.token_sort_ratio(p2, e2)) / 2
            reverse = (fuzz.token_sort_ratio(p1, e2) + fuzz.token_sort_ratio(p2, e1)) / 2
            score = max(direct, reverse)
        else:
            score = fuzz.token_sort_ratio(p1, e1)

        if score < FUZZY_THRESHOLD:
            continue

        # Проверка времени старта
        time_ok = None  # None = не смогли проверить
        if expected_utc is not None and ev.get("start_time"):
            try:
                ev_start = _dt.datetime.utcfromtimestamp(int(ev["start_time"]))
                diff_min = abs((ev_start - expected_utc).total_seconds()) / 60
                time_ok = diff_min <= time_tolerance_min
            except (ValueError, TypeError, OSError):
                time_ok = None

        candidates.append((score, time_ok, ev))

    if not candidates:
        return None

    # Приоритет:
    # 1. Совпало время (time_ok=True) — берём с лучшим score среди таких
    # 2. Время проверить не смогли (time_ok=None) — берём если score высокий
    # 3. Время НЕ совпало (time_ok=False) — берём только если score почти идеальный (>=95)

    time_matched = [c for c in candidates if c[1] is True]
    time_unknown = [c for c in candidates if c[1] is None]
    time_mismatch = [c for c in candidates if c[1] is False]

    chosen = None
    if time_matched:
        chosen = max(time_matched, key=lambda c: c[0])
        reason = "время+команды"
    elif time_unknown:
        best = max(time_unknown, key=lambda c: c[0])
        if best[0] >= FUZZY_THRESHOLD:
            chosen = best
            reason = "команды (время неизвестно)"
    elif time_mismatch:
        # Время не совпало — высокий риск ложного матча (U20 vs основа).
        # Берём ТОЛЬКО при почти идеальном совпадении названий.
        best = max(time_mismatch, key=lambda c: c[0])
        if best[0] >= 95:
            chosen = best
            reason = "только команды (ВРЕМЯ НЕ СОВПАЛО!)"

    if not chosen:
        return None

    score, time_ok, event = chosen
    logger.info(
        f"Fonbet match [{score:.0f}%, {reason}]: "
        f"'{pred_t1} vs {pred_t2}' → "
        f"'{event['team1']} vs {event['team2']}' "
        f"({'LIVE' if event['is_live'] else 'Prematch'})"
    )
    return event


# ── Определение «кривого» (value) матча ──────────────────────────────────────
import re as _re


def build_match_url(event: dict) -> str:
    """
    Собирает ссылку на матч на сайте Фонбета.
    Лайв:    fon.bet/live/football/{league_id}/{id}
    Прематч: fon.bet/sports/football/{league_id}/{id}
    """
    eid = event.get("id")
    league = event.get("league_id")
    if not eid:
        return "https://fon.bet/"
    section = "live" if event.get("is_live") else "sports"
    if league:
        return f"https://fon.bet/{section}/football/{league}/{eid}"
    return f"https://fon.bet/{section}/football/{eid}"


def parse_bet_from_prediction(text: str) -> Optional[dict]:
    """
    Извлекает тип ставки и порог из текста прогноза.

    Победитель:
      'п1 4+' / 'п2 3+' / 'P1 5+' → {type: 'win', team: 1|2, threshold: 4.0}
    Фора:
      'ф1-2,5' / 'ф2 -3.5' / 'Ф1 -4,5' → {type: 'handicap', team: 1|2, value: -2.5}

    Возвращает dict или None если ставку не распознать.
    """
    t = text.lower()

    # ── Фора: ф1 / ф2 со значением (минус, запятая или точка) ──
    # примеры: ф1-2,5  ф2 -3.5  ф1 -4,5  ф1-1.5
    m = _re.search(r'ф\s*([12])\s*(-?\d+[.,]?\d*)', t)
    if m:
        team = int(m.group(1))
        val_raw = m.group(2).replace(',', '.')
        try:
            value = float(val_raw)
        except ValueError:
            return None
        # Фора в прогнозах отрицательная (минусовая фора фаворита).
        # Если знак не указан — считаем отрицательной.
        if value > 0:
            value = -value
        return {"type": "handicap", "team": team, "value": value}

    # ── Победитель: п1 / п2 с порогом N+ ──
    # примеры: п1 4+  п2 3+  п1 100+
    m = _re.search(r'п\s*([12])\s*(\d+)\s*\+', t)
    if m:
        team = int(m.group(1))
        threshold = float(m.group(2))
        return {"type": "win", "team": team, "threshold": threshold}

    return None


def _find_factor(factors: list[dict], codes: set, pt_value: float, tol: float = 0.05) -> Optional[float]:
    """
    Ищет коэффициент фактора по набору кодов и значению pt.
    Возвращает кэф или None.
    """
    for f in factors:
        if f["f"] not in codes:
            continue
        pt = f.get("pt")
        if pt is None:
            continue
        try:
            if abs(float(pt) - pt_value) < tol:
                return f["v"]
        except (ValueError, TypeError):
            continue
    return None


def check_crookedness(pred_text: str, event: dict) -> Optional[dict]:
    """
    Проверяет матч на «кривизну» (value) по спецификации.
    event — словарь из fetch_events (с полем 'factors').

    Возвращает dict с описанием если матч кривой, иначе None:
      {team1, team2, is_live, reason, odds_info}
    """
    bet = parse_bet_from_prediction(pred_text)
    if not bet:
        return None

    factors = event.get("factors", [])
    if not factors:
        return None

    crooked = False
    reason = ""
    odds_info = ""

    if bet["type"] == "win":
        # СЛУЧАЙ 1 — Победитель
        threshold = bet["threshold"]
        odd_p1 = event.get("odd_p1")
        odd_p2 = event.get("odd_p2")

        # кэф на свою команду и на противоположную
        if bet["team"] == 1:
            own, opp = odd_p1, odd_p2
        else:
            own, opp = odd_p2, odd_p1

        # Кривой если: кэф на свою ≥ порог, ИЛИ кэф на чужую ≥ 8.0
        if own is not None and own >= threshold:
            crooked = True
            reason = f"П{bet['team']} ≥ {threshold:g}"
        elif opp is not None and opp >= 8.0:
            crooked = True
            other = 2 if bet["team"] == 1 else 1
            reason = f"П{other} ≥ 8.0 (можно тащить андердога)"

        p1s = f"{odd_p1:.2f}" if odd_p1 else "—"
        p2s = f"{odd_p2:.2f}" if odd_p2 else "—"
        odds_info = f"П1: {p1s}  |  П2: {p2s}"

    elif bet["type"] == "handicap":
        value = bet["value"]   # отрицательное, напр -2.5, -3.5
        team = bet["team"]

        if team == 1:
            match_codes = HANDICAP_MATCH_TEAM1
            half_minus15_code = {CODE_1STHALF_HANDICAP_T1_MINUS15}
        else:
            match_codes = HANDICAP_MATCH_TEAM2
            half_minus15_code = {CODE_1STHALF_HANDICAP_T2_MINUS15}

        # кэф на фору из прогноза в основное время
        match_handicap_odd = _find_factor(factors, match_codes, value)

        if abs(value) <= 2.5:
            # СЛУЧАЙ 2 — малая фора (-2.5): тайм не смотрим
            if match_handicap_odd is not None and match_handicap_odd >= 3.0:
                crooked = True
                reason = f"Ф{team} {value:g} ≥ 3.0"
            mo = f"{match_handicap_odd:.2f}" if match_handicap_odd else "—"
            odds_info = f"Ф{team} {value:g}: {mo}"
        else:
            # СЛУЧАЙ 3 — большая фора (-3.5 и больше)
            # кривой если: фора матча ≥ 2.5 ИЛИ фора 1 тайма -1.5 ≥ 2.9
            half_handicap_odd = _find_factor(factors, half_minus15_code, -1.5)

            if match_handicap_odd is not None and match_handicap_odd >= 2.5:
                crooked = True
                reason = f"Ф{team} {value:g} (матч) ≥ 2.5"
            elif half_handicap_odd is not None and half_handicap_odd >= 2.9:
                crooked = True
                reason = f"Ф{team} -1.5 (1й тайм) ≥ 2.9"

            mo = f"{match_handicap_odd:.2f}" if match_handicap_odd else "—"
            ho = f"{half_handicap_odd:.2f}" if half_handicap_odd else "—"
            odds_info = f"Ф{team} {value:g} матч: {mo}  |  Ф{team} -1.5 тайм: {ho}"

    if not crooked:
        return None

    return {
        "team1": event["team1"],
        "team2": event["team2"],
        "is_live": event["is_live"],
        "reason": reason,
        "odds_info": odds_info,
        "url": build_match_url(event),
    }


def has_relevant_odds(pred_text: str, event: dict) -> bool:
    """
    True если у события есть коэффициенты релевантные ставке прогноза.
    Для победителя — есть П1 или П2. Для форы — найдена фора нужного значения.
    """
    bet = parse_bet_from_prediction(pred_text)
    factors = event.get("factors", [])

    if not bet or bet["type"] == "win":
        return event.get("odd_p1") is not None or event.get("odd_p2") is not None

    # Фора
    value = bet["value"]
    team = bet["team"]
    if team == 1:
        match_codes = HANDICAP_MATCH_TEAM1
        half_code = {CODE_1STHALF_HANDICAP_T1_MINUS15}
    else:
        match_codes = HANDICAP_MATCH_TEAM2
        half_code = {CODE_1STHALF_HANDICAP_T2_MINUS15}

    if _find_factor(factors, match_codes, value) is not None:
        return True
    if abs(value) > 2.5 and _find_factor(factors, half_code, -1.5) is not None:
        return True
    return False


def format_bet_odds(pred_text: str, event: dict) -> str:
    """
    Формирует строку коэффициентов под тип ставки из прогноза.
    - Победитель → 'П1: x | П2: y'
    - Фора → 'Ф1 -2.5: x' (+ фора 1 тайма -1.5 если большая фора)
    Если ставку не распознать — показываем П1/П2 по умолчанию.
    """
    bet = parse_bet_from_prediction(pred_text)
    factors = event.get("factors", [])

    # По умолчанию (или для победителя) — П1/П2
    def win_line():
        p1 = event.get("odd_p1")
        p2 = event.get("odd_p2")
        p1s = f"{p1:.2f}" if p1 else "—"
        p2s = f"{p2:.2f}" if p2 else "—"
        return f"П1: {p1s}  |  П2: {p2s}"

    if not bet or bet["type"] == "win":
        return win_line()

    # Фора
    value = bet["value"]
    team = bet["team"]
    if team == 1:
        match_codes = HANDICAP_MATCH_TEAM1
        half_minus15_code = {CODE_1STHALF_HANDICAP_T1_MINUS15}
    else:
        match_codes = HANDICAP_MATCH_TEAM2
        half_minus15_code = {CODE_1STHALF_HANDICAP_T2_MINUS15}

    match_odd = _find_factor(factors, match_codes, value)
    parts = []
    mo = f"{match_odd:.2f}" if match_odd is not None else "—"
    parts.append(f"Ф{team} {value:g} (матч): {mo}")

    # Для большой форы (-3.5+) также показываем фору 1 тайма -1.5
    if abs(value) > 2.5:
        half_odd = _find_factor(factors, half_minus15_code, -1.5)
        ho = f"{half_odd:.2f}" if half_odd is not None else "—"
        parts.append(f"Ф{team} -1.5 (1й тайм): {ho}")

    return "\n".join(parts)
