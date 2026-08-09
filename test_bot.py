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
discord_listener = _load('discord_listener')

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

    # ── Регрессия: склейка прогнозов с составным заголовком "Страна. Лига - Подлига" ──
    # Баг: заголовок с доп. сегментом через дефис ("Australia. State League 2 - West
    # Australian") распознавался только частично (до первой точки), из-за чего конец
    # заголовка одного прогноза приклеивался к началу следующего, а если в доп. сегменте
    # была своя точка ("...- West Australian. Women"), парсер резал заголовок пополам.

    # Два прогноза, заголовок каждого — "Страна. Лига - Подлига[.]" (точный формат из Discord)
    text = (
        "Australia. State League 2 - West Australian 14-30\n"
        "Kalamunda City — Gosnells City\n"
        "п1 4+\n"
        "\n"
        "Australia. State League 1 - West Australian. Women\n"
        "14-30\n"
        "Perth AFC W — Mandurah City W\n"
        "п2 4+"
    )
    preds = parser.parse_predictions(text, 3, "manual")
    check("Составной заголовок 'Страна.Лига-Подлига' → 2 прогноза (не склеены)", len(preds) == 2)
    if len(preds) == 2:
        check(
            "  Блок 1 не содержит хвост заголовка второго прогноза",
            "Australia. State League 1" not in preds[0].text,
        )
        check(
            "  Блок 2 не потерял начало своего заголовка",
            preds[1].text.startswith("Australia. State League 1 - West Australian. Women"),
        )
        check("  Блок 1 содержит свои команды", "Kalamunda City" in preds[0].text)
        check("  Блок 2 содержит свои команды", "Perth AFC W" in preds[1].text)

    # Реальные примеры из жалобы пользователя (заголовок без доп. сегмента через дефис,
    # но с пустой строкой между блоками, которая схлопывается при нормализации пробелов)
    text2 = (
        "South Australian. Women 14-00 Modbury Jets W — Cove W п1 4+\n"
        "\n"
        "South Australian. Women 13-45 Adelaide Jaguars W — Elizabeth Grove W п1 3+"
    )
    preds2 = parser.parse_predictions(text2, 3, "manual")
    check("Два 'South Australian' прогноза подряд → не склеены", len(preds2) == 2)
    if len(preds2) == 2:
        check("  Первый — Modbury/Cove", "Modbury Jets W" in preds2[0].text and "Cove W" in preds2[0].text)
        check("  Второй — Adelaide/Elizabeth Grove", "Adelaide Jaguars W" in preds2[1].text)

    # Три блока подряд со смешанными вариантами заголовка (1 точка / 2 точки / точка+дефис+точка)
    text3 = (
        "Kuwait. Premier League 18-15 Al Arabi — Al Tadamon п1 100+\n"
        "South Australian. Women 14-00 Modbury Jets W — Cove W п1 4+\n"
        "Australia. State League 1 - West Australian. Women 14-30 Perth AFC W — Mandurah City W п2 4+"
    )
    preds3 = parser.parse_predictions(text3, 3, "manual")
    check("Три разных заголовка подряд → 3 прогноза", len(preds3) == 3)

    # ── Регрессия: вторая команда с заглавными буквами ("...SC", "...FC") ──
    # Баг из реального Discord-канала: команда 2 предыдущего прогноза (например
    # "Kazincbarcikai SC") сама по себе похожа на начало заголовка (заглавные
    # буквы), и старый алгоритм (искал позицию по всему схлопнутому тексту)
    # обрезал прогноз ровно после тире, теряя команду 2 и ставку, а хвост
    # приклеивал к следующему прогнозу. Реальный кейс: 5+ прогнозов подряд без
    # пустых строк между блоками, время у нескольких совпадает (18-00).
    text4 = (
        "Hungary. Second Division    18-00\n"
        "Gyirmot — Kazincbarcikai SC\n"
        "Jordan. First Division. Women    18-00\n"
        "Al Raya W — Doqarah W\n"
        "Moldova. Division A    18-00\n"
        "Falesti — Victoria Bardar\n"
        "Moldova. Division A    18-00\n"
        "Stauceni — Floresti\n"
        "Poland. 4 Liga. Podlaskie Voivodeship    18-00\n"
        "KS Wasilkow — LZS Krynki\n"
        "----------------\n"
        "Belarus. Second League    18-30\n"
        "Spartak Minsk — Urozhaynaya\n"
        "Hungary. Second Division    18-30\n"
        "BVSC-Zuglo — Kecskemeti\n"
        "Hungary. Third Division. Northeast    18-30\n"
        "Salgotarjani BTC — Mateszalkai MTK"
    )
    preds4 = parser.parse_predictions(text4, 3, "manual")
    check("5 прогнозов на 18-00 без пустых строк → не склеены", len(preds4) == 8)
    if len(preds4) == 8:
        check("  П1: Gyirmot — Kazincbarcikai SC целиком",
              "Gyirmot" in preds4[0].text and "Kazincbarcikai SC" in preds4[0].text)
        check("  П2 не потерял заголовок 'Jordan.'",
              preds4[1].text.startswith("Jordan. First Division. Women"))
        check("  П2: Al Raya W — Doqarah W целиком",
              "Al Raya W" in preds4[1].text and "Doqarah W" in preds4[1].text)
        check("  П3/П4 (два 'Moldova. Division A' подряд) не склеены",
              "Falesti" in preds4[2].text and "Falesti" not in preds4[3].text)
        check("  П8 (после блочного разделителя) сохранил заголовок",
              preds4[7].text.startswith("Hungary. Third Division. Northeast"))

    # Тот же баг, но прогноз из жалобы пользователя (с реальной ставкой "7+"/"5+"
    # на отдельной строке после команд, и скобки в названиях команд/лиг)
    text5 = (
        "Poland. Women Ekstraklasa    13-00\n"
        "AAPLG Gdansk W — ZS UJ Krakow W\n"
        "7+\n"
        "Finland. Kolmonen. Etela-Suomi    13-00\n"
        "Toukolan Teras — PPJ/Lauttasaari\n"
        "7+\n"
        "India. Durand Cup. Group stage    13-30\n"
        "Shillong Lajong — Mumbay FC\n"
        "см стату\n"
        "Australia. Premier League - Northern Territory (reserves)    13-30\n"
        "University Azzurri (res) — Hellenic AC (res)\n"
        "п1 5+"
    )
    preds5 = parser.parse_predictions(text5, 3, "manual")
    check("4 прогноза со ставкой на отдельной строке → не склеены", len(preds5) == 4)
    if len(preds5) == 4:
        check("  П1 содержит свою ставку '7+', не ставку П2",
              preds5[0].text.rstrip().endswith("7+") and "Finland" not in preds5[0].text)
        check("  П3 (без явной ставки, 'см стату') не проглотил П4",
              "см стату" in preds5[2].text and "Australia" not in preds5[2].text)
        check("  П4 сохранил составной заголовок с скобками",
              preds5[3].text.startswith("Australia. Premier League - Northern Territory (reserves)"))

    # ── Регрессия: скобки ВНУТРИ заголовка (не только в конце строки) ──
    # Баг из реального Discord-канала: "Myanmar (Burma). Youth League U20" не
    # распознавался как заголовок вообще, потому что скобка сразу после первого
    # слова обрывала _WORD_GROUP до появления разделителя сегмента (". "/" - ").
    # Вся строка считалась продолжением предыдущего блока (Belarus), и два
    # разных прогноза на разные матчи склеивались в один. Отличается от
    # предыдущего кейса (preds5, П4) тем, что там скобки стоят В КОНЦЕ, уже
    # после времени — там заголовок успевает "закрыться" до скобок.
    text6 = (
        "Australia. League 1 - New South Wales. Women    12-00\n"
        "Blacktown Spartans W — Sutherland Strikers W\n"
        "7+\n"
        "Belarus. Second League    12-00\n"
        "Nadezhda Gorodishche — Krechet\n"
        "п1 5+\n"
        "Myanmar (Burma). Youth League U20    12-00\n"
        "Falcon Myanmar U20 — Ayeyawady United U20\n"
        "п1 5+\n"
        "Slovenia. Top League. Women    12-00\n"
        "Cerklje W — ZNK Ljubljana W\n"
        "ф2-3,5"
    )
    preds6 = parser.parse_predictions(text6, 3, "manual")
    check("Скобки в середине заголовка ('Myanmar (Burma).') → заголовок не потерян",
          len(preds6) == 4)
    if len(preds6) == 4:
        check("  П2 (Belarus) не проглотил П3 (Myanmar)",
              "Krechet" in preds6[1].text and "Myanmar" not in preds6[1].text)
        check("  П3 сохранил заголовок 'Myanmar (Burma).'",
              preds6[2].text.startswith("Myanmar (Burma). Youth League U20"))
        check("  П4 (Slovenia) не склеен с П3",
              preds6[3].text.startswith("Slovenia. Top League. Women"))


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


def test_notify_grouping():
    print("\n[Группировка/сортировка уведомлений Discord]")

    from models import Prediction
    # match_time в БД хранится в UTC (naive datetime, как datetime.utcnow()
    # в storage.py); format_time_local сама прибавляет tz_offset при выводе.
    # Здесь offset=3, поэтому base-час UTC выбираем так, чтобы после +3
    # получались "круглые" значения, удобные для проверки.
    base = datetime(2026, 8, 9, 0, 0)

    def mk(text, hh_local, mm):
        # hh_local — то время, которое должно получиться ПОСЛЕ +3 (offset=3)
        return Prediction(text=text, match_time=base.replace(hour=(hh_local - 3) % 24, minute=mm), source="discord")

    # Регрессия: реальный кейс пользователя — added_preds приходит в порядке
    # чтения истории Discord (новые сообщения первыми), НЕ в порядке времени
    # матчей. Раньше _group_by_time сохраняла этот порядок как есть, и
    # уведомление шло 11:30 → 12:00 → 12:40 → 13:00 → 14:00 → 10:30 (последний
    # блок из более старого/раннего сообщения оказывался в конце).
    preds = [
        mk("A", 11, 30),
        mk("B", 12, 0),
        mk("C", 12, 40),
        mk("D", 13, 0),
        mk("E", 14, 0),
        mk("F", 10, 30),   # физически раньше всех, но последний в списке
        mk("G", 10, 30),
    ]
    groups = discord_listener._group_by_time(preds, tz_offset=3)
    labels = [t for t, _ in groups]
    check("Группы отсортированы по времени, а не по порядку появления",
          labels == ["10:30", "11:30", "12:00", "12:40", "13:00", "14:00"])
    check("Прогнозы на одно и то же время (10:30) остались в одной группе",
          len(dict(groups)["10:30"]) == 2)

    # Склонение — используется прямо в заголовке уведомления, ошибка тут
    # означает неправильный текст на проде при каждом синке.
    cases = {1: "прогноз", 2: "прогноза", 4: "прогноза", 5: "прогнозов",
             11: "прогнозов", 12: "прогнозов", 14: "прогнозов",
             21: "прогноз", 22: "прогноза", 25: "прогнозов"}
    all_ok = all(discord_listener._pluralize_predictions(n) == word
                 for n, word in cases.items())
    check("Склонение 'прогноз/прогноза/прогнозов' верно для всех проверенных чисел", all_ok)

    # Сводка вместо полного списка при большом числе прогнозов (порог = 15)
    many = [mk(f"League {i}. Div    12-0{i%10}\nTeam A{i} — Team B{i}\n5+", 12, i % 60) for i in range(20)]
    msg = discord_listener._build_notify_messages(many, 3, "автопроверка")
    check("При >15 прогнозов уходит сводка, упоминающая /list",
          any("/list" in m for m in msg))


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
    test_notify_grouping()

    print("\n" + "=" * 50)
    print(f"Пройдено: {_passed}  |  Провалено: {_failed}")
    print("=" * 50)
    sys.exit(1 if _failed else 0)
