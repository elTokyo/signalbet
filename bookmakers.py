"""
Другие БК: BetBoom, Winline, Лига Ставок.

Зачем: когда матч признан кривым (или андердогом), бот вдогонку проверяет,
есть ли этот же матч и ТОТ ЖЕ рынок (П1/П2 или фора из прогноза) на трёх
других конторах, и присылает найденные кэфы.

Заменяет прежний каскад Леон → БетСити (leon.py / betcity.py больше
не используются планировщиком).

⚠️ ВАЖНО ПРО ЭНДПОИНТЫ
Структуры ответов у этих трёх контор не документированы публично, поэтому
парсер здесь УНИВЕРСАЛЬНЫЙ (эвристический): он рекурсивно ищет в JSON объекты
с двумя командами и коэффициентами, не зная заранее конкретных имён полей.
Такой подход обычно заводится «как есть», но URL у конторы может отличаться
от зашитого. Поэтому:
  • URL каждой конторы можно переопределить переменной окружения
    (BETBOOM_API_URL / WINLINE_API_URL / LIGASTAVOK_API_URL) — без правки кода;
  • скрипт bk_probe.py перебирает кандидатов и печатает, что отвечает;
  • команда /bk в боте показывает результат по конкретному прогнозу.
"""
import re
import time
import logging
import threading
from datetime import datetime
from typing import Optional

import requests
from fuzzywuzzy import fuzz

import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

FUZZY_THRESHOLD = 80
CACHE_TTL = 30          # кэш линии каждой конторы, сек
REQUEST_TIMEOUT = 12

# ── Конфигурация контор ──────────────────────────────────────────────────────
# url      — основной эндпоинт (перекрывается переменной окружения)
# candidates — запасные адреса, их перебирает bk_probe.py и авто-фоллбэк
BOOKMAKERS = [
    {
        "key": "betboom",
        "name": "BetBoom",
        "url": config.BETBOOM_API_URL or "https://betboom.ru/api/v1/lineFeed/get?sport=1&count=1000",
        "candidates": [
            "https://betboom.ru/api/v1/lineFeed/get?sport=1&count=1000",
            "https://betboom.ru/api/v2/sportsbook/events?sportId=1",
            "https://sportsbook.betboom.ru/api/v1/events?sport=football",
        ],
    },
    {
        "key": "winline",
        "name": "Winline",
        "url": config.WINLINE_API_URL or "https://stavki.winline.ru/api/sportsbook/v1/events?sportId=1",
        "candidates": [
            "https://stavki.winline.ru/api/sportsbook/v1/events?sportId=1",
            "https://stavki.winline.ru/api/v1/line/events?sport=1",
            "https://winline.ru/api/sportsbook/events?sport=football",
        ],
    },
    {
        "key": "ligastavok",
        "name": "Лига Ставок",
        "url": config.LIGASTAVOK_API_URL or "https://www.ligastavok.ru/api/v1/sport/events?sportId=1",
        "candidates": [
            "https://www.ligastavok.ru/api/v1/sport/events?sportId=1",
            "https://www.ligastavok.ru/api/sportsbook/v1/events?sport=football",
            "https://sportsbook.ligastavok.ru/api/v1/events?sport=1",
        ],
    },
]

_cache: dict[str, dict] = {}
_lock = threading.RLock()


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _fetch_json(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"BK {url} → HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.warning(f"BK {url} → ошибка: {e}")
        return None


# ── Универсальный разбор JSON ────────────────────────────────────────────────

TEAM_PAIRS = [
    ("team1", "team2"), ("name1", "name2"), ("homeName", "awayName"),
    ("home", "away"), ("team_1", "team_2"), ("opp1", "opp2"),
    ("participant1", "participant2"), ("homeTeam", "awayTeam"),
    ("name_ht", "name_at"), ("first", "second"),
]

TIME_KEYS = ("kickoff", "startTime", "start_time", "start", "dateStart",
             "date_ev", "eventDate", "date", "time", "ts")

LIVE_KEYS = ("live", "isLive", "inLive", "inplay", "isInplay", "is_online")

ODD_KEYS = ("price", "kf", "odd", "odds", "coef", "coefficient", "v", "value", "factor")
LABEL_KEYS = ("name", "caption", "title", "type", "n", "code", "key", "marketName")
PARAM_KEYS = ("pt", "param", "parameter", "handicap", "hcp", "value2", "p")


def _as_name(v) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("name", "title", "caption"):
            if isinstance(v.get(k), str) and v[k].strip():
                return v[k].strip()
    return None


def _teams_from(node: dict) -> Optional[tuple[str, str]]:
    """Пытается достать пару команд из узла JSON."""
    for k1, k2 in TEAM_PAIRS:
        n1, n2 = _as_name(node.get(k1)), _as_name(node.get(k2))
        if n1 and n2:
            return n1, n2

    for key in ("competitors", "participants", "teams", "opponents"):
        items = node.get(key)
        if isinstance(items, list) and len(items) >= 2:
            home = away = None
            for c in items:
                if not isinstance(c, dict):
                    continue
                side = str(c.get("homeAway") or c.get("side") or c.get("type") or "").upper()
                name = _as_name(c)
                if not name:
                    continue
                if side.startswith("HOME") or side in ("1", "H"):
                    home = home or name
                elif side.startswith("AWAY") or side in ("2", "A"):
                    away = away or name
            if home and away:
                return home, away
            names = [_as_name(c) for c in items[:2] if _as_name(c)]
            if len(names) == 2:
                return names[0], names[1]
    return None


def _kickoff_from(node: dict) -> Optional[datetime]:
    for k in TIME_KEYS:
        v = node.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                ts = float(v)
                if ts > 1e11:       # миллисекунды
                    ts /= 1000.0
                if ts < 1e9:        # явно не unix-время
                    continue
                return datetime.utcfromtimestamp(ts)
            if isinstance(v, str) and len(v) >= 10:
                return datetime.fromisoformat(v.replace("Z", "").split("+")[0])
        except (ValueError, TypeError, OSError):
            continue
    return None


def _is_live_from(node: dict) -> bool:
    for k in LIVE_KEYS:
        if node.get(k):
            return True
    state = str(node.get("betline") or node.get("state") or node.get("place") or "").lower()
    return "live" in state or "inplay" in state


def _collect_odds(node, out: list, depth: int = 0):
    """
    Рекурсивно собирает все пары (метка, параметр, кэф) внутри события.
    Метка — то, что похоже на название исхода/рынка, кэф — число 1.01…1000.
    """
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _collect_odds(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return

    odd = None
    for k in ODD_KEYS:
        v = node.get(k)
        if isinstance(v, (int, float)) and 1.01 <= float(v) <= 1000:
            odd = float(v)
            break
    if odd is not None:
        label = ""
        for k in LABEL_KEYS:
            if isinstance(node.get(k), str) and node[k].strip():
                label = node[k].strip()
                break
        param = None
        for k in PARAM_KEYS:
            v = node.get(k)
            if isinstance(v, (int, float)):
                param = float(v)
                break
            if isinstance(v, str):
                try:
                    param = float(v.replace(",", "."))
                    break
                except ValueError:
                    pass
        out.append({"label": label, "param": param, "odd": odd})

    # Ключи-исходы вида {"P1": {...}} / {"W1": 2.5} тоже важны
    for k, v in node.items():
        if isinstance(v, (int, float)) and 1.01 <= float(v) <= 1000 and _outcome_side(k):
            out.append({"label": k, "param": None, "odd": float(v)})
        elif isinstance(v, (dict, list)):
            if isinstance(v, dict) and _outcome_side(k):
                inner = None
                for ok in ODD_KEYS:
                    if isinstance(v.get(ok), (int, float)):
                        inner = float(v[ok])
                        break
                if inner and 1.01 <= inner <= 1000:
                    out.append({"label": k, "param": v.get("pt"), "odd": inner})
            _collect_odds(v, out, depth + 1)


_WIN1_RE = re.compile(r'^(п1|p1|w1|1|home|хозяева|первый)$', re.IGNORECASE)
_WIN2_RE = re.compile(r'^(п2|p2|w2|2|away|гости|второй)$', re.IGNORECASE)
_HCP_RE = re.compile(r'(фора|handicap|hcp|\bф[12]\b)', re.IGNORECASE)


def _outcome_side(label: str) -> Optional[int]:
    """1 / 2 / None — на какую команду исход по его метке."""
    if not isinstance(label, str):
        return None
    l = label.strip()
    if _WIN1_RE.match(l):
        return 1
    if _WIN2_RE.match(l):
        return 2
    return None


def _walk_events(node, out: list, depth: int = 0):
    """Рекурсивно ищет в ответе узлы, похожие на матч (две команды)."""
    if depth > 8 or len(out) > 8000:
        return
    if isinstance(node, list):
        for item in node:
            _walk_events(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return

    teams = _teams_from(node)
    if teams:
        odds: list = []
        _collect_odds(node, odds)
        out.append({
            "team1": teams[0],
            "team2": teams[1],
            "kickoff_utc": _kickoff_from(node),
            "is_live": _is_live_from(node),
            "odds": odds,
        })
        return  # внутрь найденного события глубже не лезем

    for v in node.values():
        if isinstance(v, (dict, list)):
            _walk_events(v, out, depth + 1)


def fetch_events(bk: dict) -> list[dict]:
    """Линия одной конторы (с кэшем). Пустой список = не достучались/не распарсили."""
    key = bk["key"]
    with _lock:
        c = _cache.get(key)
        if c and (time.time() - c["ts"]) < CACHE_TTL:
            return c["events"]

    urls = [bk["url"]] + [u for u in bk.get("candidates", []) if u != bk["url"]]
    events: list = []
    for url in urls:
        data = _fetch_json(url)
        if not data:
            continue
        _walk_events(data, events)
        if events:
            logger.info(f"{bk['name']}: {len(events)} событий ({url})")
            break
        logger.warning(f"{bk['name']}: ответ получен, но события не распознаны ({url})")

    with _lock:
        _cache[key] = {"events": events, "ts": time.time()}
    return events


# ── Поиск матча и нужного рынка ──────────────────────────────────────────────

def _match_event(events: list[dict], t1: str, t2: str,
                 expected_utc=None, tolerance_min: int = 15) -> Optional[tuple[dict, bool]]:
    """Фаззи-поиск матча. Возвращает (событие, swapped) или None."""
    if not t1:
        return None
    p1 = t1.lower()
    p2 = (t2 or "").lower()

    best, best_score = None, 0
    for ev in events:
        e1, e2 = ev["team1"].lower(), ev["team2"].lower()
        if p2:
            direct = (fuzz.token_sort_ratio(p1, e1) + fuzz.token_sort_ratio(p2, e2)) / 2
            reverse = (fuzz.token_sort_ratio(p1, e2) + fuzz.token_sort_ratio(p2, e1)) / 2
            score, swapped = (direct, False) if direct >= reverse else (reverse, True)
        else:
            score, swapped = fuzz.token_sort_ratio(p1, e1), False

        if score < FUZZY_THRESHOLD or score <= best_score:
            continue

        if expected_utc is not None and ev.get("kickoff_utc"):
            diff_min = abs((ev["kickoff_utc"] - expected_utc).total_seconds()) / 60
            if diff_min > tolerance_min and score < 95:
                continue

        best, best_score = (ev, swapped), score

    return best


def _find_win(ev: dict, side: int) -> Optional[float]:
    for o in ev["odds"]:
        if _outcome_side(o["label"]) == side and not _HCP_RE.search(o["label"] or ""):
            return o["odd"]
    return None


def _find_handicap(ev: dict, side: int, value: float, tol: float = 0.05) -> Optional[float]:
    for o in ev["odds"]:
        label = o.get("label") or ""
        if not _HCP_RE.search(label):
            continue
        # сторона форы: ф1/ф2 в метке либо цифра рядом со словом «фора»
        m = re.search(r'(?:ф|f|handicap|hcp)\s*([12])', label, re.IGNORECASE)
        if m and int(m.group(1)) != side:
            continue
        param = o.get("param")
        if param is None:
            m2 = re.search(r'(-?\d+[.,]?\d*)', label.replace(",", "."))
            if not m2:
                continue
            try:
                param = float(m2.group(1))
            except ValueError:
                continue
        if abs(abs(float(param)) - abs(value)) < tol:
            return o["odd"]
    return None


def check_all(team1: str, team2: str, bet: dict, expected_utc=None) -> list[dict]:
    """
    Проверяет все три конторы на наличие матча и рынка из прогноза.

    bet — результат fonbet.parse_bet_from_prediction:
          {"type": "win", "team": 1|2, ...} или {"type": "handicap", "team": 1|2, "value": -2.5}

    Возвращает список по каждой конторе:
      {"name", "status": "ok"|"no_market"|"no_match"|"error", "odd", "label", "is_live"}
    """
    results = []
    for bk in BOOKMAKERS:
        entry = {"name": bk["name"], "status": "no_match", "odd": None,
                 "label": "", "is_live": False}
        try:
            events = fetch_events(bk)
            if not events:
                entry["status"] = "error"
                results.append(entry)
                continue

            found = _match_event(events, team1, team2, expected_utc)
            if not found:
                results.append(entry)
                continue

            ev, swapped = found
            entry["is_live"] = ev.get("is_live", False)
            side = bet["team"]
            if swapped:
                side = 2 if side == 1 else 1

            if bet["type"] == "win":
                odd = _find_win(ev, side)
                entry["label"] = f"П{bet['team']}"
            else:
                odd = _find_handicap(ev, side, bet["value"])
                entry["label"] = f"Ф{bet['team']} {bet['value']:g}"

            entry["status"] = "ok" if odd else "no_market"
            entry["odd"] = odd
        except Exception as e:
            logger.error(f"{bk['name']} check error: {e}")
            entry["status"] = "error"
        results.append(entry)
    return results


def format_results(results: list[dict]) -> Optional[str]:
    """
    Текст сообщения-довеска. None если ни одна контора ничего не дала
    (тогда сообщение просто не отправляется).
    """
    lines = []
    any_found = False
    for r in results:
        if r["status"] == "ok":
            any_found = True
            live = " 🔴" if r["is_live"] else ""
            lines.append(f"✅ {r['name']} — {r['label']} = {r['odd']:.2f}{live}")
        elif r["status"] == "no_market":
            any_found = True
            lines.append(f"➖ {r['name']} — матч есть, рынка {r['label']} нет")
        elif r["status"] == "no_match":
            lines.append(f"❌ {r['name']} — матча нет")
        else:
            lines.append(f"⚠️ {r['name']} — не отвечает")

    if not any_found:
        return None
    return "🔎 Этот матч на других конторах:\n" + "\n".join(lines)
