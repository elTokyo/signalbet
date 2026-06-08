"""
Юнит-тесты ключевой логики бота.
Запуск:  python3 test_bot.py
Не требует внешних сервисов — мокает Gist и сетевые вызовы.

Покрывает самое хрупкое (что ломали чаще всего):
- парсинг прогнозов (время, триггеры, блоки)
- извлечение команд
- парсинг ставок (победитель/фора)
- логику кривизны (СЛУЧАЙ 1/2/3)
"""
import sys
import os
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta

# Мокаем внешние зависимости
for mod in ['fuzzywuzzy', 'requests', 'aiohttp', 'telegram', 'telegram.ext',
            'telegram.error', 'discord', 'apscheduler',
            'apscheduler.schedulers.asyncio', 'apscheduler.triggers.interval']:
    sys.modules[mod] = MagicMock()
os.environ.setdefault('BOT_TOKEN', 'test')

# fuzzywuzzy мокаем правдоподобно: token_sort_ratio по совпадению слов
def _token_sort(a, b):
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0
    inter = len(wa & wb)
    union = len(wa | wb)
    return int(100 * inter / union)

sys.modules['fuzzywuzzy'].fuzz.token_sort_ratio = _token_sort
sys.modules['fuzzywuzzy'].fuzz.partial_ratio = lambda a, b: 100 if a in b or b in a else 50

import importlib.util


def _load(name):
    spec = importlib.util.spec_from_file_location(name, f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

# Загружаем модули
models = _load('models')
parser = _load('parser')
fonbet = _load('fonbet')

# Счётчики
_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}")


# ── Тесты парсера ────────────────────────────────────────────────────────────

def test_parser():
    print("\n[Парсер прогнозов]")

    # Триггер Soccer
    preds = parser.parse_predictions("Soccer. Brazil. 22-00 Team A — Team B п2 3+", 3, "manual")
    check("Триггер Soccer + время 22-00", len(preds) == 1)

    # Без триггера (Kuwait кейс)
    preds = parser.parse_predictions("Kuwait. Premier League 18-15\nAl Arabi — Al Tadamon п1 100+", 3, "manual")
    check("Без триггера, время 18-15", len(preds) == 1)

    # Несколько через тире-разделитель
    text = ("Футбол. 18-00 A — B п2 4+\n"
            "-------------------------------\n"
            "Футбол. 19-00 C — D п1 3+\n"
            "Футбол. 19-00 E — F ф1-4,5")
    preds = parser.parse_predictions(text, 3, "manual")
    check("Три прогноза через тире-блоки", len(preds) == 3)

    # Нет времени → не парсится
    preds = parser.parse_predictions("Soccer. Brazil. Team A — Team B п2", 3, "manual")
    check("Без времени → пусто", len(preds) == 0)

    # Формат времени HH:MM
    preds = parser.parse_predictions("Soccer. 14:30 A — B п1 4+", 3, "manual")
    check("Время в формате HH:MM", len(preds) == 1)


# ── Тесты извлечения команд ──────────────────────────────────────────────────

def test_extract_teams():
    print("\n[Извлечение команд]")

    t1, t2 = fonbet.extract_teams_from_prediction(
        "Soccer. Brazil. 22-00 Atletico Clipper (20) — Fast Clube (20) п2 3+")
    check("Команды с возрастом (20)", t1 == "Atletico Clipper (20)" and "Fast Clube" in t2)

    t1, t2 = fonbet.extract_teams_from_prediction(
        "Soccer. Kuwait. 18-15 Al Arabi — Al Tadamon п1 100+")
    check("Простые команды", t1 == "Al Arabi" and t2 == "Al Tadamon")


# ── Тесты парсинга ставок ─────────────────────────────────────────────────────

def test_bet_parsing():
    print("\n[Парсинг ставок]")

    b = fonbet.parse_bet_from_prediction("Team A — Team B п2 4+")
    check("Победитель п2 4+", b == {"type": "win", "team": 2, "threshold": 4.0})

    b = fonbet.parse_bet_from_prediction("Team A — Team B п1 100+")
    check("Победитель п1 100+", b == {"type": "win", "team": 1, "threshold": 100.0})

    b = fonbet.parse_bet_from_prediction("Team A — Team B ф1-4,5")
    check("Фора ф1-4,5 (запятая)", b == {"type": "handicap", "team": 1, "value": -4.5})

    b = fonbet.parse_bet_from_prediction("Team A — Team B ф2 -2.5")
    check("Фора ф2 -2.5 (точка)", b == {"type": "handicap", "team": 2, "value": -2.5})

    b = fonbet.parse_bet_from_prediction("Team A — Team B без ставки")
    check("Нет ставки → None", b is None)


# ── Тесты логики кривизны ─────────────────────────────────────────────────────

def test_crookedness():
    print("\n[Логика кривизны]")

    # Реальные факторы Wolfsburg
    factors = [
        {"f": 921, "v": 1.72, "pt": None}, {"f": 923, "v": 4.7, "pt": None},
        {"f": 927, "v": 2.25, "pt": -1}, {"f": 1569, "v": 2.8, "pt": -1.5},
        {"f": 989, "v": 4.6, "pt": -2}, {"f": 910, "v": 5.3, "pt": -2.5},
        {"f": 1681, "v": 8.5, "pt": -1.5},
    ]
    event = {"team1": "Wolfsburg", "team2": "Paderborn", "is_live": False,
             "odd_p1": 1.72, "odd_p2": 4.7, "factors": factors}

    # СЛУЧАЙ 1: П2 4+, кэф П2=4.7 ≥ 4 → кривой
    r = fonbet.check_crookedness("W — P п2 4+", event)
    check("СЛУЧАЙ 1: п2 4+ при П2=4.7 → кривой", r is not None)

    # П1 4+, П1=1.72<4, П2=4.7<8 → не кривой
    r = fonbet.check_crookedness("W — P п1 4+", event)
    check("СЛУЧАЙ 1: п1 4+ при П1=1.72 → не кривой", r is None)

    # СЛУЧАЙ 2: ф1 -2.5, фора матча Ф1-2.5=5.3 ≥ 3 → кривой
    r = fonbet.check_crookedness("W — P ф1-2,5", event)
    check("СЛУЧАЙ 2: ф1 -2.5 при 5.3 → кривой", r is not None)

    # СЛУЧАЙ 3: ф1 -3.5, фора 1тайма -1.5=8.5 ≥ 2.9 → кривой
    r = fonbet.check_crookedness("W — P ф1-3,5", event)
    check("СЛУЧАЙ 3: ф1 -3.5 через 1тайм 8.5 → кривой", r is not None)

    # Низкие кэфы → не кривой
    low_factors = [
        {"f": 921, "v": 1.3, "pt": None}, {"f": 923, "v": 3.0, "pt": None},
    ]
    low_event = {"team1": "A", "team2": "B", "is_live": False,
                 "odd_p1": 1.3, "odd_p2": 3.0, "factors": low_factors}
    r = fonbet.check_crookedness("A — B п2 4+", low_event)
    check("Низкий кэф П2=3.0 при пороге 4 → не кривой", r is None)


# ── Тесты URL ─────────────────────────────────────────────────────────────────

def test_url():
    print("\n[Построение URL]")

    url = fonbet.build_match_url({"id": 65279576, "league_id": 124689, "is_live": False})
    check("Прематч URL", url == "https://fon.bet/sports/football/124689/65279576")

    url = fonbet.build_match_url({"id": 64439911, "league_id": 16372, "is_live": True})
    check("Лайв URL", url == "https://fon.bet/live/football/16372/64439911")


# ── Тесты has_relevant_odds ───────────────────────────────────────────────────

def test_has_odds():
    print("\n[Проверка наличия коэффициентов]")

    # Победитель без П1/П2 → нет коэф (был баг с цифрами в "П1"/"П2")
    ev = {"odd_p1": None, "odd_p2": None, "factors": []}
    check("Победитель без коэф → False", fonbet.has_relevant_odds("A — B п1 4+", ev) is False)

    ev = {"odd_p1": 1.5, "odd_p2": 6.0, "factors": []}
    check("Победитель с коэф → True", fonbet.has_relevant_odds("A — B п1 4+", ev) is True)


def test_age_markers():
    print("\n[Возрастные маркеры]")

    check("(20) → u20", fonbet.extract_age_marker("Atletico (20) — Fast (20)") == "u20")
    check("U23 → u23", fonbet.extract_age_marker("Brazil U23. Sao Paulo — Santos") == "u23")
    check("основа → None", fonbet.extract_age_marker("Atletico — Fast") is None)
    check("W → women", fonbet.extract_age_marker("Lyon W — PSG W") == "women")

    # Главное: прогноз с возрастом НЕ совместим с событием-основой
    check("U20 prog ≠ основа event",
          fonbet._age_markers_compatible("Atletico (20) — Fast (20)", "Atletico", "Fast") is False)
    check("U20 prog = U20 event",
          fonbet._age_markers_compatible("Atletico (20) — Fast (20)", "Atletico U20", "Fast U20") is True)
    check("основа prog ≠ U20 event",
          fonbet._age_markers_compatible("Atletico — Fast", "Atletico U20", "Fast U20") is False)


if __name__ == "__main__":
    print("=" * 50)
    print("ТЕСТЫ BET BOT")
    print("=" * 50)

    test_parser()
    test_extract_teams()
    test_bet_parsing()
    test_crookedness()
    test_url()
    test_has_odds()
    test_age_markers()

    print("\n" + "=" * 50)
    print(f"Пройдено: {_passed}  |  Провалено: {_failed}")
    print("=" * 50)
    sys.exit(1 if _failed else 0)
