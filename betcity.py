"""
БетСити (betcity.ru) — клиент для поиска победителя когда на Фонбете нет чистой П1/П2.

Используется в каскаде после Леона: если Леон не дал победу нужной команды,
бот проверяет БетСити.

API:
- /d/off/events?id_sp=1 — вся линия футбола (прематч)
- Структура: sports.1.chmps.<id>.evts.<id>:
    name_ht / name_at — команды (хозяева/гости)
    date_ev — время старта (unix)
    main.69.data.<id>.blocks.Wm.P1/P2/X.kf — коэффициенты исхода
"""
import time
import logging
import threading
import requests
from typing import Optional
from datetime import datetime
from fuzzywuzzy import fuzz

logger = logging.getLogger(__name__)

BASE_URL = "https://ad.betcity.ru/d/off/events"
PARAMS = {
    "rev": "6",
    "add": "dep_events",
    "id_sp": "1",      # футбол
    "ver": "86",
    "csn": "eepugw",
    "lng": "0",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

FUZZY_THRESHOLD = 80
CACHE_TTL = 20
_cache = {"events": None, "ts": 0}
_lock = threading.RLock()


def fetch_events() -> list[dict]:
    """
    Запрашивает всю линию футбола БетСити.
    Возвращает список: {team1, team2, kickoff_utc, win1, win2, is_live}
    """
    with _lock:
        now = time.time()
        if _cache["events"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return _cache["events"]

    try:
        r = requests.get(BASE_URL, params=PARAMS, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            logger.warning(f"BetCity API {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        logger.error(f"BetCity fetch error: {e}")
        return []

    result = []
    sports = data.get("reply", {}).get("sports", {})
    football = sports.get("1", {})
    chmps = football.get("chmps", {})

    for ch in chmps.values():
        evts = ch.get("evts", {})
        for ev in evts.values():
            team1 = ev.get("name_ht")
            team2 = ev.get("name_at")
            # Пропускаем аутрайты (одна команда, нет соперника)
            if not team1 or not team2:
                continue
            # Пропускаем зависимые статистические события (углы, xG и т.п.)
            if ev.get("is_dep"):
                continue

            # Ищем блок Wm (исход П1/П2/X) в main.69
            win1 = win2 = None
            main = ev.get("main", {})
            block69 = main.get("69", {})
            if block69:
                ev_id = str(ev.get("id_ev"))
                data_block = block69.get("data", {}).get(ev_id, {})
                wm = data_block.get("blocks", {}).get("Wm", {})
                if wm:
                    p1 = wm.get("P1")
                    p2 = wm.get("P2")
                    if isinstance(p1, dict):
                        win1 = p1.get("kf")
                    if isinstance(p2, dict):
                        win2 = p2.get("kf")

            kickoff_utc = None
            date_ev = ev.get("date_ev")
            if date_ev:
                try:
                    kickoff_utc = datetime.utcfromtimestamp(int(date_ev))
                except (ValueError, TypeError, OSError):
                    kickoff_utc = None

            result.append({
                "team1": team1,
                "team2": team2,
                "kickoff_utc": kickoff_utc,
                "win1": win1,
                "win2": win2,
                "is_live": bool(ev.get("is_online")),
            })

    with _lock:
        _cache["events"] = result
        _cache["ts"] = time.time()

    logger.info(f"BetCity: получено {len(result)} футбольных событий")
    return result


def find_win_odds(pred_t1: str, pred_t2: str, team_num: int,
                  expected_utc=None, time_tolerance_min: int = 15) -> Optional[dict]:
    """
    Ищет матч на БетСити по командам и возвращает кэф на победу нужной команды.
    Возвращает {odd, team1, team2, is_live} или None.
    """
    if not pred_t1:
        return None

    events = fetch_events()
    if not events:
        return None

    p1 = pred_t1.lower()
    p2 = (pred_t2 or "").lower()

    best = None
    best_score = 0
    best_time_ok = None

    for ev in events:
        e1 = ev["team1"].lower()
        e2 = ev["team2"].lower()
        if p2:
            direct = (fuzz.token_sort_ratio(p1, e1) + fuzz.token_sort_ratio(p2, e2)) / 2
            reverse = (fuzz.token_sort_ratio(p1, e2) + fuzz.token_sort_ratio(p2, e1)) / 2
            if direct >= reverse:
                score, swapped = direct, False
            else:
                score, swapped = reverse, True
        else:
            score, swapped = fuzz.token_sort_ratio(p1, e1), False

        if score < FUZZY_THRESHOLD or score <= best_score:
            continue

        time_ok = None
        if expected_utc is not None and ev.get("kickoff_utc"):
            diff_min = abs((ev["kickoff_utc"] - expected_utc).total_seconds()) / 60
            time_ok = diff_min <= time_tolerance_min
            if not time_ok and score < 95:
                continue

        best = (ev, swapped)
        best_score = score
        best_time_ok = time_ok

    if not best:
        return None

    ev, swapped = best
    if team_num == 1:
        odd = ev["win2"] if swapped else ev["win1"]
    else:
        odd = ev["win1"] if swapped else ev["win2"]

    if odd is None:
        return None

    logger.info(
        f"BetCity match [{best_score:.0f}%, time_ok={best_time_ok}]: "
        f"'{pred_t1} vs {pred_t2}' → '{ev['team1']} vs {ev['team2']}', "
        f"П{team_num}={odd}"
    )
    return {
        "odd": odd,
        "team1": ev["team1"],
        "team2": ev["team2"],
        "is_live": ev["is_live"],
    }
