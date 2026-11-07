"""
Юнит-тесты ключевой логики бота.
Запуск:  python3 test_bot.py
Не требует внешних сервисов — мокает Gist и сетевые вызовы.

Покрывает самое хрупкое (что ломали чаще всего):
- парсинг прогнозов (время, триггеры, блоки)
- извлечение команд
- парсинг ставок (победитель/фора)
- логику кривизны (СЛУЧАЙ 1/2/3)
- здоровье бота/алерты, личные фильтры, журнал и алиасы, вопросы админу по неопределённым датам, тик планировщика
- матчинг с Фонбетом: пороги 60/50, «тот же слот» (время + страна/лига), окно мониторинга
- даты матчей из Discord-сообщений (публикация/правка/полночь/дубли/гонки) — с контролируемым временем
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
match_dates = _load('match_dates')
parser = _load('parser')
fonbet = _load('fonbet')
discord_listener = _load('discord_listener')
storage = sys.modules['storage']   # импортирован discord_listener-ом
bookmakers = _load('bookmakers')
sys.modules.setdefault('auth', MagicMock())   # auth.py для тестов не нужен
scheduler = _load('scheduler')

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


    # ── Регрессия: заголовок со СТРОЧНЫМИ словами ──
    # Баг: строгий заголовок требовал заглавную в каждом слове сегмента, поэтому
    # "South American games. Women. Argentina 21-00" (строчное «games») не считался
    # началом прогноза — строка приклеивалась к Norway, два матча становились одним
    # (второй терялся, у первого «команда 2» превращалась в мусор).
    text7 = (
        "Norway. Youth League U19 20-30\n"
        "Flint II U19 — Runar/Goif/Helgerod II U19\n"
        "South American games. Women. Argentina 21-00\n"
        "Uruguay U20 W — Paraguay U20 W"
    )
    preds7 = parser.parse_predictions(text7, 3, "manual")
    check("Заголовок со строчным словом ('South American games.') → 2 прогноза", len(preds7) == 2)
    if len(preds7) == 2:
        check("  П1 (Norway) не проглотил второй матч",
              "Runar" in preds7[0].text and "Argentina" not in preds7[0].text)
        check("  П2 сохранил заголовок целиком",
              preds7[1].text.startswith("South American games. Women. Argentina 21-00"))
        check("  Команды П1 извлекаются чисто (без хвоста второго матча)",
              "Paraguay" not in fonbet.extract_teams_from_prediction(preds7[0].text)[1])

    # Те же матчи, но каждый прогноз одной строкой
    text7b = (
        "Norway. Youth League U19 20-30 Flint II U19 — Runar/Goif/Helgerod II U19\n"
        "South American games. Women. Argentina 21-00 Uruguay U20 W — Paraguay U20 W"
    )
    check("То же, прогноз одной строкой → 2 прогноза",
          len(parser.parse_predictions(text7b, 3, "manual")) == 2)

    # Строчные соединители в названии страны — и в середине, и первой строкой
    text8 = (
        "Bosnia and Herzegovina. Premier League 21-00\n"
        "Zrinjski — Borac\n"
        "п1 5+\n"
        "Norway. Youth League U19 20-30\n"
        "Flint II U19 — Runar II U19"
    )
    check("'Bosnia and Herzegovina.' первой строкой не теряется",
          len(parser.parse_predictions(text8, 3, "manual")) == 2)

    # Заголовок со строчным словом, время отдельной строкой следом
    text9 = (
        "Norway. Youth League U19 20-30\n"
        "Flint II U19 — Runar II U19\n"
        "South American games. Women\n"
        "21-00\n"
        "Uruguay U20 W — Paraguay U20 W"
    )
    check("Строчный заголовок + время отдельной строкой → 2 прогноза",
          len(parser.parse_predictions(text9, 3, "manual")) == 2)

    # Защита от ложных срабатываний: строка-комментарий с точкой — не заголовок
    text10 = (
        "Norway. Youth League U19 20-30\n"
        "Flint II U19 — Runar II U19\n"
        "Берём на победу хозяев. Кэф высокий\n"
        "Ставка после 20-30. Ждём кэф\n"
        "7+"
    )
    check("Комментарии с точкой не становятся новым прогнозом",
          len(parser.parse_predictions(text10, 3, "manual")) == 1)


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


def test_live_detection():
    """Строгое определение ЛАЙВ / ЛИНИЯ (п.1 апдейта)."""
    print("\n[Определение лайв/линия]")
    import time as _t
    now = _t.time()

    is_live, reason = fonbet.detect_live({"id": 1, "startTime": now + 3600}, set(), now)
    check("Матч через час → линия", is_live is False and reason == "line")

    is_live, reason = fonbet.detect_live({"id": 2, "startTime": now - 30}, set(), now)
    check("Время старта прошло → лайв (по времени)", is_live is True and reason == "time")

    is_live, reason = fonbet.detect_live({"id": 3, "startTime": now + 600, "live": True}, set(), now)
    check("Флаг live → лайв", is_live is True and reason.startswith("flag"))

    is_live, reason = fonbet.detect_live({"id": 4, "startTime": now + 600, "place": "live"}, set(), now)
    check("Поле place=live → лайв", is_live is True and reason == "place")

    is_live, reason = fonbet.detect_live({"id": 5, "startTime": now + 600}, {5}, now)
    check("Есть блок со счётом → лайв", is_live is True and reason == "scoreboard")

    live_ids = fonbet._collect_live_ids({"eventMiscs": [
        {"id": 7, "score1": 1, "score2": 0}, {"id": 8, "someOtherField": 1},
    ]})
    check("_collect_live_ids берёт только события со счётом/таймером", live_ids == {7})


def test_underdog():
    """Разделение кривого матча и андердога (п.2 апдейта)."""
    print("\n[Андердог vs кривой]")
    base = {"team1": "A", "team2": "B", "is_live": False, "id": 1, "league_id": 1,
            "factors": [{"f": 921, "v": 1.2, "pt": None}]}
    text = "Футбол. Чехия. 19-00 A — B п1 4+"

    r = fonbet.check_crookedness(text, dict(base, odd_p1=1.2, odd_p2=9.5))
    check("Кэф на соперника 9.5 → андердог", r and r["kind"] == "underdog")

    r = fonbet.check_crookedness(text, dict(base, odd_p1=1.2, odd_p2=8.0))
    check("Ровно 8.0 → андердог (порог включительно)", r and r["kind"] == "underdog")

    r = fonbet.check_crookedness(text, dict(base, odd_p1=1.2, odd_p2=7.99))
    check("7.99 → ничего", r is None)

    r = fonbet.check_crookedness(text, dict(base, odd_p1=4.5, odd_p2=9.5))
    check("Оба условия → кривой матч (не андердог)", r and r["kind"] == "crooked")
    check("В причине кривого видно и собаку", r and "собака" in r["reason"])
    check("Видны кэфы обеих команд", r and "П1: 4.50" in r["odds_info"] and "П2: 9.50" in r["odds_info"])

    r = fonbet.check_crookedness("Футбол. 19-00 A — B ф1-2,5",
                                 dict(base, factors=[{"f": 927, "v": 9.0, "pt": -2.5}]))
    check("Для форы андердог не проверяется", r is None or r["kind"] == "crooked")


def test_bookmakers_parser():
    """Универсальный разбор ответов других контор (п.3 апдейта)."""
    print("\n[Парсер других контор]")

    shapes = {
        "формат с competitors": {"data": [{
            "competitors": [{"name": "Уния", "homeAway": "HOME"},
                            {"name": "Спуйня", "homeAway": "AWAY"}],
            "kickoff": 1758200000000, "betline": "prematch",
            "markets": [{"name": "1X2", "runners": [{"name": "П1", "price": 1.45},
                                                    {"name": "П2", "price": 8.90}]},
                        {"name": "Фора", "runners": [{"name": "Ф2", "price": 2.55,
                                                      "handicap": -2.5}]}]}]},
        "формат с name1/name2": {"events": [{
            "name1": "Уния", "name2": "Спуйня", "startTime": 1758200000, "live": False,
            "outcomes": [{"caption": "P1", "kf": 1.5}, {"caption": "P2", "kf": 9.1},
                         {"caption": "Фора 2 (-2.5)", "kf": 2.4, "param": -2.5}]}]},
        "формат с homeName/awayName": {"result": {"items": [{
            "homeName": "Уния", "awayName": "Спуйня", "dateStart": "2026-09-18T19:00:00",
            "markets": {"1": {"P1": {"price": 1.48}, "P2": {"price": 8.2}}}}]}},
    }

    for label, data in shapes.items():
        events = []
        bookmakers._walk_events(data, events)
        ok = len(events) == 1 and events[0]["team1"] == "Уния" and events[0]["team2"] == "Спуйня"
        check(f"Команды распознаны: {label}", ok)
        if ok:
            check(f"П2 распознан: {label}", bookmakers._find_win(events[0], 2) is not None)

    events = []
    bookmakers._walk_events(shapes["формат с competitors"], events)
    check("Фора -2.5 найдена по значению",
          bookmakers._find_handicap(events[0], 2, -2.5) == 2.55)
    check("Фора другого значения не подставляется",
          bookmakers._find_handicap(events[0], 2, -4.5) is None)
    check("Фаззи-поиск матча работает",
          bookmakers._match_event(events, "Уния", "Спуйня") is not None)
    check("Чужой матч не матчится",
          bookmakers._match_event(events, "Реал", "Барселона") is None)

    check("Пустой результат → сообщение не шлётся",
          bookmakers.format_results([{"name": "X", "status": "no_match", "odd": None,
                                      "label": "", "is_live": False}]) is None)
    txt = bookmakers.format_results([
        {"name": "BetBoom", "status": "ok", "odd": 9.2, "label": "П2", "is_live": False},
        {"name": "Winline", "status": "no_match", "odd": None, "label": "П2", "is_live": False},
    ])
    check("Найденная контора попадает в текст", txt and "BetBoom" in txt and "9.20" in txt)


def test_settings_migration():
    """Новые тумблеры не ломают старые настройки в Gist."""
    print("\n[Миграция настроек]")
    old = {"chat_id": 1, "timezone_offset": 3, "fonbet_notifications": False}
    s = models.UserSettings.from_dict(old)
    check("Андердог наследует старый тумблер кривых", s.notify_underdog is False)
    check("Войс по умолчанию включён", s.notify_voice is True)
    s2 = models.UserSettings.from_dict(models.UserSettings(chat_id=2).to_dict())
    check("Круговая сериализация настроек не теряет поля",
          s2.notify_underdog is True and s2.notify_voice is True)


def test_live_message():
    """Лайв-уведомление ВСЕГДА дублирует прогноз (раньше — только если не было «линии»)."""
    print("\n[Лайв-уведомление]")
    pred = "Romania. Liga 3. Group 6 20-00 CSM Targu Jiu — CS Drobeta-Turnu Severin см стату равные +-"
    msg = scheduler._format_live_message("CSM Targu Jiu", "CS Drobeta-Turnu Severin",
                                         "П1: 1.75  |  П2: 3.65", pred)
    check("Заголовок лайва на месте", msg.startswith("🔴 Матч вышел в лайв!"))
    check("Кэфы на месте", "П1: 1.75" in msg)
    check("Прогноз продублирован", "📝 Прогноз:\n" + pred in msg)
    msg2 = scheduler._format_live_message("A", "B", "(коэф. так и не появились)", "Soccer. 18-00 A — B п1")
    check("Прогноз есть и когда кэфы не появились", "📝 Прогноз:" in msg2)


def test_voice():
    """Слежение за войсом: кулдаун, откат при недоставке, диагностика."""
    print("\n[Слежение за войсом Discord]")
    import asyncio
    dl = discord_listener
    dl.config.DISCORD_VOICE_USER_ID = 111
    dl.config.DISCORD_VOICE_CHANNEL_ID = 222
    dl.config.VOICE_COOLDOWN_SEC = 600

    sent = []
    state = {"recipients": [1, 2], "delivered": 2}

    def fake_recipients(field):
        return list(state["recipients"])

    async def fake_send(recipients, text):
        sent.append(text)
        return state["delivered"]

    dl._recipients_for = fake_recipients
    dl._send_tg = fake_send

    def run(kind):
        asyncio.run(dl._voice_notify(kind, "Admin", "Общий"))

    dl._voice_last_sent.update({"join": 0.0, "leave": 0.0})
    run("join")
    check("Заход → отправлено", len(sent) == 1 and "зашёл" in sent[0])
    run("join")
    check("Повторный заход внутри кулдауна → тишина", len(sent) == 1)

    dl._voice_last_sent.update({"join": 0.0, "leave": 0.0})
    sent.clear()
    state["delivered"] = 0
    run("join")
    state["delivered"] = 2
    run("join")
    check("Telegram не принял → кулдаун откатывается, следующая попытка идёт", len(sent) == 2)

    dl._voice_last_sent.update({"join": 0.0, "leave": 0.0})
    sent.clear()
    state["recipients"] = []
    run("leave")
    check("Нет получателей → ничего не шлём", not sent)
    state["recipients"] = [1]
    run("leave")
    check("Получатели появились → выход уходит (кулдаун не сжёгся)",
          len(sent) == 1 and "вышел" in sent[0])

    # Диагностика /voice
    class _Client:
        def is_closed(self): return False
        def get_channel(self, _id): return None
        guilds = []

    dl._client_ref = _Client()
    st = dl.get_voice_status()
    check("/voice: канал не виден боту — сказано прямо", "НЕ видит этот канал" in st)
    dl.config.DISCORD_VOICE_CHANNEL_ID = 0
    check("/voice: без переменных — сказано, что выключено", "Выключено" in dl.get_voice_status())
    dl._client_ref = None



# ── Даты матчей: дата определяется по самому матчу, а не по возрасту сообщения ─

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

TZ = 3  # МСК: в тестах все «локальные» времена — UTC+3


def L(day, hour, minute=0):
    """Локальное (МСК) время 'day октября 2026' → naive UTC, как хранится в проекте."""
    return datetime(2026, 10, day, hour, minute) - timedelta(hours=TZ)


def lt(dt):
    """naive UTC → строка локального времени 'дд HH:MM' для сравнений в тестах."""
    d = dt + timedelta(hours=TZ)
    return f"{d.day:02d} {d:%H:%M}"


def M(hhmm, a, b):
    """Строка прогноза в старом формате: 'Футбол. Лига. HH-MM А — Б п1 2+'."""
    return f"Футбол. Лига. {hhmm.replace(':', '-')} {a} — {b} п1 2+"


def snap(mid, created, content, edited=None):
    return {"id": str(mid), "created_at": created, "edited_at": edited, "content": content}


class _FakeGist:
    """In-memory замена gist_storage: каждое чтение — свежая копия, как JSON из сети."""
    FILE_PREDICTIONS = "predictions.json"
    FILE_SETTINGS = "settings.json"

    def __init__(self):
        self.db = {}
        self.writes = 0

    def read(self, name):
        return copy.deepcopy(self.db.get(name, {}))

    def write(self, name, data):
        self.db[name] = copy.deepcopy(data)
        self.writes += 1

    def invalidate_cache(self):
        pass


class _Env:
    """Подменяет Gist в storage на время теста."""
    def __enter__(self):
        self.gist = _FakeGist()
        self._old = storage.gist_storage
        storage.gist_storage = self.gist
        return self

    def __exit__(self, *a):
        storage.gist_storage = self._old

    def apply(self, snaps, now, **kw):
        return storage.apply_discord_messages(snaps, now=now, tz_offset=TZ, **kw)

    def sched(self):
        return sorted(storage.load_predictions(), key=lambda p: p.match_time)

    def times(self):
        return [lt(p.match_time) for p in self.sched()]

    def ledger(self):
        return self.gist.db.get("predictions.json", {}).get(storage.LEDGER_KEY, {})


def test_match_dates_rules():
    print("\n[Даты матчей: правила и границы суток]")
    R = match_dates.resolve_match_datetime

    def res(h, m, anchor, hint=None, tz=TZ):
        r = R(h, m, hint, anchor, tz)
        return (lt(r.dt) if r.dt else None), r.basis

    # день в день
    check("Пост 11:00, матч 18:00 → тот же день", res(18, 0, L(20, 11)) == ("20 18:00", "anchor:same-day"))
    check("Пост 11:00, ночной матч 01:00 → следующий день", res(1, 0, L(20, 11)) == ("21 01:00", "anchor:next-day-night-match"))
    check("Пост 11:00, время 09:00 раньше публикации и не ночь → не определено",
          res(9, 0, L(20, 11)) == (None, "undetermined"))
    # вечер на завтра
    check("Пост 23:00, матч 12:00 → завтра", res(12, 0, L(20, 23)) == ("21 12:00", "anchor:next-day-evening-post"))
    check("Пост 18:00 (граница «вечера»), матч 10:00 → завтра", res(10, 0, L(20, 18)) == ("21 10:00", "anchor:next-day-evening-post"))
    check("Пост 17:59, матч 10:00 → не определено", res(10, 0, L(20, 17, 59)) == (None, "undetermined"))
    # границы полуночи
    check("Пост 23:59, матч 00:30 → 21-е 00:30", res(0, 30, L(20, 23, 59)) == ("21 00:30", "anchor:next-day-evening-post"))
    check("Пост 23:59, матч 23:59 → тот же день", res(23, 59, L(20, 23, 59)) == ("20 23:59", "anchor:same-day"))
    check("Пост 00:10, матч 01:00 → тот же день (20-е)", res(1, 0, L(20, 0, 10)) == ("20 01:00", "anchor:same-day"))
    check("Матч начался 10 мин до публикации → всё ещё тот же день (grace)", res(10, 50, L(20, 11))[0] == "20 10:50")
    # сутки по UTC и по местному времени — разные
    anchor = datetime(2026, 10, 20, 21, 30)   # 20.10 21:30 UTC = 21.10 00:30 МСК
    check("Граница суток считается по местному поясу, а не по UTC", res(12, 0, anchor)[0] == "21 12:00")
    r10 = R(9, 0, None, anchor, 10)
    check("UTC+10: пост 07:30 местн., матч 09:00 → 21.10 09:00 местн. (= 20.10 23:00 UTC)",
          r10.dt == datetime(2026, 10, 20, 23, 0) and r10.basis == "anchor:same-day")
    # явные даты
    check("Явная дата 22.10 приоритетнее правила",
          res(12, 0, L(20, 11), match_dates.DateHint(day=22, month=10))[0] == "22 12:00")
    check("«Завтра» считается от момента публикации, а не от «сейчас»",
          res(12, 0, L(20, 11), match_dates.DateHint(rel_days=1))[0] == "21 12:00")
    check("Явная дата без года: ближайший к публикации год (31.12 при публикации 30.12.2026)",
          lt(R(12, 0, match_dates.DateHint(day=1, month=1), datetime(2026, 12, 30, 9, 0), TZ).dt) .startswith("01 "))
    check("Невозможная дата (31.02) → не определено",
          R(12, 0, match_dates.DateHint(day=31, month=2), L(20, 11), TZ).dt is None)
    # отсечка заведомо мёртвой истории
    now = L(21, 9)
    check("История: сообщение 5-дневной давности без правок не читаем", not match_dates.can_still_matter(L(16, 11), None, now))
    check("История: старое сообщение, правленное недавно, читаем", match_dates.can_still_matter(L(16, 11), L(21, 1), now))
    check("История: вчерашнее сообщение читаем", match_dates.can_still_matter(L(20, 11), None, now))


def test_date_headers_parser():
    print("\n[Парсер: даты в тексте]")
    ph = parser._parse_date_header
    check("Заголовок «21.10»", ph("21.10") == match_dates.DateHint(day=21, month=10))
    check("Заголовок «Вторник, 21 октября»", ph("Вторник, 21 октября") == match_dates.DateHint(day=21, month=10))
    check("Заголовок «Прогнозы на завтра:»", ph("Прогнозы на завтра:") == match_dates.DateHint(rel_days=1))
    check("Заголовок «🔥 Сегодня 🔥»", ph("🔥 Сегодня 🔥") == match_dates.DateHint(rel_days=0))
    check("«ТБ 2.5» — не дата", ph("ТБ 2.5") is None)
    check("«п2 3+» — не дата", ph("п2 3+") is None)
    check("«4 лига» — не дата", ph("4 лига") is None)
    check("Время «18-00» — не дата", ph("18-00") is None)
    check("Строка с командами — не заголовок", ph("Футбол. Польша. 19-00 Уния — Спуйня 21.10") is None)

    blocks = parser.split_blocks("Сегодня\n" + M("18:00", "А", "Б") + "\n---------\n22.10\n" + M("12:00", "В", "Г")
                                 + "\n" + "Футбол. Чехия. 23.10 19-00 Д — Е п1")
    hints = [h for _b, h in blocks]
    check("Секции: сегодня / 22.10 / инлайн 23.10 приоритетнее заголовка 22.10",
          hints == [match_dates.DateHint(rel_days=0), match_dates.DateHint(day=22, month=10),
                    match_dates.DateHint(day=23, month=10)])
    check("Заголовки дат не попадают в текст прогнозов",
          all("22.10\n" not in b and not b.startswith("Сегодня") for b, _h in blocks))
    plain = parser.split_blocks(M("18:00", "А", "Б") + "\n" + M("19:00", "В", "Г"))
    check("Без дат разбор прежний: 2 блока, без подсказок", len(plain) == 2 and all(h is None for _b, h in plain))
    mt = parser.parse_predictions("Футбол. Лига. 25.12 18-00 А — Б п1", TZ, "manual")[0].match_time + timedelta(hours=TZ)
    check("Ручной /add с явной датой 25.12 использует её", (mt.month, mt.day, mt.hour) == (12, 25, 18))


def test_scenario_a_yesterday_message():
    print("\n[Сценарий A: вчерашнее сообщение с прошедшими матчами]")
    content = "\n".join([M("14:00", "Альфа", "Бета"), M("18:00", "Гамма", "Дельта"),
                         M("20:30", "Эпсилон", "Зета"), M("01:00", "Ипсилон", "Омега")])
    created = L(20, 11)
    with _Env() as env:
        st = env.apply([snap(1, created, content)], now=L(21, 9))
        check("Вчерашнее сообщение, бот видит его 21.10 в 09:00 → в расписании пусто", env.times() == [])
        check("Все 4 матча учтены как прошедшие", st["skipped_stale"] == 4 and st["added"] == 0)
        check("Ночной матч 01:00 → это 21.10 01:00, к 09:00 тоже прошёл",
              all(r["status"] == "stale" for r in env.ledger()["1"]["matches"].values()))
    with _Env() as env:
        st = env.apply([snap(1, created, content)], now=L(20, 11, 5))
        check("То же сообщение в день публикации → 4 матча (включая ночной на 21-е)",
              env.times() == ["20 14:00", "20 18:00", "20 20:30", "21 01:00"])
        env.apply([snap(1, created, content)], now=L(21, 9))
        check("На следующее утро записи не дублируются и не «переезжают» на сегодня",
              env.times() == ["20 14:00", "20 18:00", "20 20:30", "21 01:00"])


def test_scenario_b_evening_for_tomorrow():
    print("\n[Сценарий B: вечернее сообщение с матчами на завтра]")
    content = "\n".join([M("09:00", "А", "Б"), M("15:00", "В", "Г"), M("20:00", "Д", "Е")])
    with _Env() as env:
        st = env.apply([snap(2, L(20, 23), content)], now=L(20, 23, 5))
        check("Пост 20.10 23:00 → все 3 матча на 21.10",
              env.times() == ["21 09:00", "21 15:00", "21 20:00"] and st["added"] == 3)
        check("Основание даты записано (вечерняя публикация)",
              all(p.date_basis == "anchor:next-day-evening-post" for p in env.sched()))
        check("msg_id и first_seen_at сохранены", all(p.msg_id == "2" and p.first_seen_at for p in env.sched()))
        env.apply([snap(2, L(20, 23), content)], now=L(21, 8))
        check("Утром 21.10 те же 3 матча, без дублей", len(env.sched()) == 3)


def test_scenario_c_edit_adds_matches():
    print("\n[Сценарий C: правка старого сообщения добавляет матчи]")
    v1 = "\n".join([M("14:00", "А", "Б"), M("18:00", "В", "Г")])
    v2 = v1 + "\n" + M("12:00", "Новая1", "Х") + "\n" + M("09:30", "Новая2", "У")
    with _Env() as env:
        env.apply([snap(3, L(20, 11), v1)], now=L(20, 11, 1))
        check("11:00: два матча на сегодня", env.times() == ["20 14:00", "20 18:00"])
        # планировщик вычистил сыгранные
        storage.save_predictions(predictions=[])
        st = env.apply([snap(3, L(20, 11), v2, edited=L(20, 23))], now=L(20, 23, 1))
        check("23:00 правка: добавлено ровно 2 новых матча", st["added"] == 2)
        check("Новые матчи — на 21.10 (anchor = время правки, а не публикации)",
              env.times() == ["21 09:30", "21 12:00"])
        check("Сыгранные 14:00/18:00 в расписание не вернулись", all("20 " not in t for t in env.times()))
        check("Уведомление только о новых (added_preds = 2)", len(st["added_preds"]) == 2)
    # 5 матчей → 8
    five = [M(f"{h}:00", f"К{h}", f"Л{h}") for h in (13, 14, 15, 16, 17)]
    eight = five + [M(f"{h}:00", f"К{h}", f"Л{h}") for h in (18, 19, 20)]
    with _Env() as env:
        env.apply([snap(4, L(20, 11), "\n".join(five))], now=L(20, 11, 1))
        st = env.apply([snap(4, L(20, 11), "\n".join(eight), edited=L(20, 12))], now=L(20, 12, 1))
        check("Было 5, стало 8 → добавлено только 3 (всего 8)", st["added"] == 3 and len(env.sched()) == 8)


def test_repeat_edit_no_changes():
    print("\n[Повторная правка без изменений]")
    v1 = M("14:00", "А", "Б") + "\n" + M("18:00", "В", "Г")
    with _Env() as env:
        env.apply([snap(5, L(20, 11), v1)], now=L(20, 11, 1))
        w = env.gist.writes
        st = env.apply([snap(5, L(20, 11), v1)], now=L(20, 11, 2))
        check("Тот же снимок повторно → ничего не добавлено/обновлено/удалено",
              (st["added"], st["updated"], st["removed"]) == (0, 0, 0) and st["unchanged"] == 1)
        check("…и в Gist ничего не пишется", env.gist.writes == w)
        st = env.apply([snap(5, L(20, 11), v1, edited=L(20, 12))], now=L(20, 12, 1))
        check("Правка без смены содержимого (напр. embed) → 0 изменений, 2 записи",
              (st["added"], st["updated"], st["removed"]) == (0, 0, 0) and len(env.sched()) == 2)


def test_time_change_of_known_match():
    print("\n[Изменение времени известного матча]")
    v1 = M("18:00", "А", "Б") + "\n" + M("20:00", "В", "Г")
    v2 = M("19:30", "А", "Б") + "\n" + M("20:00", "В", "Г")
    with _Env() as env:
        env.apply([snap(6, L(20, 11), v1)], now=L(20, 11, 1))
        p = [x for x in env.sched() if "А —" in x.text][0]
        storage.update_prediction(pred_id=p.id, notified_30=True, notified_5=True)
        pid = p.id
        st = env.apply([snap(6, L(20, 11), v2, edited=L(20, 15))], now=L(20, 15, 1))
        after = env.sched()
        check("18:00 → 19:30: запись обновлена, шестого матча нет", st["updated"] == 1 and st["added"] == 0 and len(after) == 2)
        moved = [x for x in after if x.id == pid][0]
        check("Время и дата верны (20.10 19:30), id прежний", lt(moved.match_time) == "20 19:30")
        check("Флаги напоминаний 30/5 мин сброшены для нового времени", not moved.notified_30 and not moved.notified_5)
        v3 = M("01:00", "А", "Б") + "\n" + M("20:00", "В", "Г")
        env.apply([snap(6, L(20, 11), v3, edited=L(20, 16))], now=L(20, 16, 1))
        check("Перенос на 01:00 → следующая ночь (21.10 01:00)",
              [lt(x.match_time) for x in env.sched() if x.id == pid] == ["21 01:00"])
        # завершившийся матч не возвращается из-за правки текста
        storage.save_predictions(predictions=[x for x in env.sched() if x.id != pid])   # его «вычистили»
        env.apply([snap(6, L(20, 11), v3 + "\nстатистика", edited=L(21, 2))], now=L(21, 9))
    with _Env() as env:
        env.apply([snap(7, L(20, 11), M("14:00", "А", "Б"))], now=L(20, 11, 1))
        storage.save_predictions(predictions=[])      # матч сыгран и вычищен планировщиком
        st = env.apply([snap(7, L(20, 11), M("14:00", "А", "Б") + " ✅", edited=L(20, 18))], now=L(20, 18))
        check("Сыгранный матч после правки текста в расписание не возвращается", env.sched() == [] and st["restored"] == 0)
        env.apply([snap(7, L(20, 11), M("23:00", "А", "Б"), edited=L(20, 19))], now=L(20, 19))
        check("…но если автор перенёс его на будущее время — возвращается",
              env.times() == ["20 23:00"])


def test_match_identity_stable():
    print("\n[Идентичность матча при правках текста]")
    with _Env() as env:
        env.apply([snap(50, L(20, 11), M("18:00", "Альфа", "Бета"))], now=L(20, 11, 1))
        pid = env.sched()[0].id
        st = env.apply([snap(50, L(20, 11), M("18:00", "Альфа", "Бета") + " ✅", edited=L(20, 19))], now=L(20, 19))
        check("Дописан значок «✅» → тот же матч, не новый", len(env.sched()) == 1 and env.sched()[0].id == pid and st["added"] == 0)
        st = env.apply([snap(50, L(20, 11), M("18:00", "Альфа", "Бета ФК"), edited=L(20, 19, 5))], now=L(20, 19, 5))
        check("Поправили название второй команды при том же времени → запись обновлена, а не добавлена вторая",
              len(env.sched()) == 1 and env.sched()[0].id == pid)
    with _Env() as env:
        two = M("18:00", "Реал", "Бетис") + "\n" + M("18:00", "Реал", "Барса")
        env.apply([snap(51, L(20, 11), two)], now=L(20, 11, 1))
        check("Разные матчи одной команды в одно время не склеиваются", len(env.sched()) == 2)


def test_multiple_dates_in_one_message():
    print("\n[Несколько дат в одном сообщении]")
    content = "\n".join([
        "20.10", M("18:00", "А", "Б"), M("21:00", "В", "Г"),
        "---------------",
        "21.10", M("12:00", "Д", "Е"),
        "---------------",
        "Завтра", M("15:00", "Ж", "З"),   # относительно публикации → 21.10
    ])
    with _Env() as env:
        env.apply([snap(8, L(20, 11), content)], now=L(20, 11, 1))
        check("Каждый матч — на свою дату (20.10 ×2, 21.10 ×2)",
              env.times() == ["20 18:00", "20 21:00", "21 12:00", "21 15:00"])
        check("Основание — явная дата", all(p.date_basis == "explicit" for p in env.sched()))
        check("Текст прогноза остаётся без строки-заголовка даты",
              all(not p.text.startswith(("20.10", "21.10", "Завтра")) for p in env.sched()))
    # заголовок со вчерашней датой в сегодняшнем контексте — прошедшие
    with _Env() as env:
        st = env.apply([snap(9, L(20, 11), content)], now=L(21, 13))
        check("21.10 13:00: прошедшие 20.10 и 21.10 12:00 пропущены, остался 21.10 15:00",
              env.times() == ["21 15:00"] and st["skipped_stale"] == 3)


def test_undetermined_date():
    print("\n[Дату нельзя определить достоверно]")
    with _Env() as env:
        content = M("09:00", "А", "Б") + "\n" + M("18:00", "В", "Г")
        st = env.apply([snap(10, L(20, 11), content)], now=L(20, 11, 1))
        check("Дневной пост, время 09:00 раньше публикации → не добавлен, 18:00 добавлен",
              env.times() == ["20 18:00"] and st["skipped_undetermined"] == 1)
        rec = [r for r in env.ledger()["10"]["matches"].values() if r["status"] == "undetermined"]
        check("Причина неопределённости сохранена для повторного разбора", len(rec) == 1 and "достоверно" in rec[0]["reason"])
    with _Env() as env:
        # бот впервые видит сообщение, которое уже правили: неизвестно, что было в оригинале
        content = M("14:00", "А", "Б")
        st = env.apply([snap(11, L(20, 11), content, edited=L(20, 23))], now=L(20, 23, 5))
        check("Холодный старт на правленом сообщении: матч 14:00 неоднозначен → не добавлен",
              env.times() == [] and st["skipped_undetermined"] == 1)
        st = env.apply([snap(11, L(20, 11), content, edited=L(20, 23))], now=L(20, 23, 20))
        check("…и повторный перечит его не «додумывает»", env.times() == [])
    with _Env() as env:
        # то же вечернее сообщение, правка в тот же вечер: версии согласны → дата достоверна
        content = M("12:00", "А", "Б")
        env.apply([snap(12, L(20, 23), content, edited=L(20, 23, 40))], now=L(20, 23, 45))
        check("Холодный старт, но публикация и правка дают одну дату → добавлен (21.10 12:00)",
              env.times() == ["21 12:00"])


def test_restart_reprocessing():
    print("\n[Повторная обработка после перезапуска]")
    content = M("14:00", "А", "Б") + "\n" + M("18:00", "В", "Г")
    snaps = [snap(13, L(20, 11), content)]
    with _Env() as env:
        env.apply(snaps, now=L(20, 11, 1))
        ids = sorted(p.id for p in env.sched())
        for i in range(3):   # «рестарты»: тот же перечит истории канала
            st = env.apply(snaps, now=L(20, 12 + i), full_scan=True, scan_floor=datetime.min)
        check("3 перечита истории → те же записи с теми же id", sorted(p.id for p in env.sched()) == ids)
        check("Уведомлений о новых нет", st["added_preds"] == [] and st["added"] == 0)
        # потеряна запись расписания (гонка записи), реестр цел → будущий матч восстановлен тихо
        storage.save_predictions(predictions=[p for p in env.sched() if "А —" not in p.text])
        st = env.apply(snaps, now=L(20, 12, 30), full_scan=True, scan_floor=datetime.min)
        check("Потерянная будущая запись восстановлена без повторного уведомления",
              len(env.sched()) == 2 and st["restored"] == 1 and st["added_preds"] == [])
        # старый снапшот без реестра в памяти — реестр в Gist, так что состояние переживает рестарт
        check("Реестр сообщений хранится в Gist (переживает редеплой)", "13" in env.ledger())


def test_legacy_and_removals():
    print("\n[Старые записи, удаление матчей и сообщений]")
    content = M("14:00", "Старая", "Запись") + "\n" + M("18:00", "Живая", "Запись")
    # старая запись (без msg_id), посчитанная прежним парсером «на сегодня» из вчерашнего сообщения
    with _Env() as env:
        bug = models.Prediction(text=M("18:00", "Вчерашняя", "Игра"), match_time=L(21, 18), source="discord")
        manual = models.Prediction(text=M("20:00", "Ручная", "Игра"), match_time=L(21, 20), source="manual")
        storage.save_predictions(predictions=[bug, manual])
        st = env.apply([snap(14, L(20, 11), M("18:00", "Вчерашняя", "Игра"))], now=L(21, 9),
                       full_scan=True, scan_floor=datetime.min)
        check("Баг-запись «вчерашний матч на сегодня» убрана при перечите", st["legacy_removed"] == 1)
        check("Ручной прогноз не тронут", [p.source for p in env.sched()] == ["manual"])
    # корректная старая запись усыновляется, а не дублируется; флаги сохраняются
    with _Env() as env:
        old = models.Prediction(text=M("18:00", "Живая", "Запись"), match_time=L(20, 18), source="discord", notified_30=True)
        storage.save_predictions(predictions=[old])
        st = env.apply([snap(15, L(20, 11), M("18:00", "Живая", "Запись"))], now=L(20, 12))
        p = env.sched()
        check("Запись прежней версии с верной датой привязана к сообщению (без дубля, id и флаги целы)",
              len(p) == 1 and p[0].id == old.id and p[0].msg_id == "15" and p[0].notified_30 and st["added"] == 0)
    # ручной прогноз с теми же командами/временем → дубль не создаётся
    with _Env() as env:
        manual = models.Prediction(text=M("18:00", "Живая", "Запись"), match_time=L(20, 18), source="manual")
        storage.save_predictions(predictions=[manual])
        st = env.apply([snap(16, L(20, 11), M("18:00", "Живая", "Запись"))], now=L(20, 12))
        check("Совпадает с ручным прогнозом → дубль не добавлен", len(env.sched()) == 1 and st["skipped_duplicate"] == 1)
    # одинаковый матч в двух сообщениях
    with _Env() as env:
        env.apply([snap(17, L(20, 11), M("18:00", "Х", "У")), snap(18, L(20, 11, 30), M("18:00", "Х", "У"))], now=L(20, 12))
        check("Тот же матч в двух сообщениях → одна запись", len(env.sched()) == 1)
    # автор убрал матч правкой
    with _Env() as env:
        both = M("18:00", "А", "Б") + "\n" + M("22:00", "В", "Г")
        env.apply([snap(19, L(20, 11), both)], now=L(20, 12))
        st = env.apply([snap(19, L(20, 11), M("22:00", "В", "Г"), edited=L(20, 13))], now=L(20, 13))
        check("Матч убран из сообщения до старта → снят с расписания", env.times() == ["20 22:00"] and st["removed"] == 1)
    with _Env() as env:
        both = M("18:00", "А", "Б") + "\n" + M("22:00", "В", "Г")
        env.apply([snap(23, L(20, 11), both)], now=L(20, 12))
        # исчезновение прогноза ≠ отмена матча: уже начавшийся матч остаётся в расписании
        st = env.apply([snap(23, L(20, 11), M("22:00", "В", "Г"), edited=L(20, 18, 30))], now=L(20, 18, 30))
        check("Убранный из сообщения матч, который уже начался, не трогаем",
              st["removed"] == 0 and env.times() == ["20 18:00", "20 22:00"])
        env.apply([snap(23, L(20, 11), M("22:00", "В", "Г") + "\n" + M("18:00", "А", "Б"), edited=L(20, 18, 40))],
                  now=L(20, 18, 40))
        check("Автор вернул убранный матч → дубликата нет", env.times() == ["20 18:00", "20 22:00"])
    with _Env() as env:
        env.apply([snap(20, L(20, 11), M("18:00", "А", "Б") + "\n" + M("22:00", "В", "Г"))], now=L(20, 12))
        n = storage.retract_discord_message(20, now=L(20, 12, 5))
        check("Сообщение удалено из канала → его будущие матчи сняты", n == 2 and env.sched() == [])
    with _Env() as env:
        env.apply([snap(21, L(20, 11), M("18:00", "А", "Б"))], now=L(20, 12))
        st = env.apply([], now=L(20, 12, 15), full_scan=True, scan_floor=datetime.min)
        check("Перечит: сообщения больше нет в канале → матч снят", env.sched() == [] and st["removed"] == 1)
        env.apply([snap(22, L(20, 11), M("18:00", "К", "Л"))], now=L(20, 12))
        env.apply([], now=L(20, 12, 15), full_scan=True, scan_floor=L(20, 12))
        check("История прочитана не до конца (scan_floor новее сообщения) → не считаем его удалённым",
              len(env.sched()) == 1)


def test_concurrent_events():
    print("\n[Повторные и одновременные события]")
    v1 = "\n".join([M("14:00", "А", "Б"), M("18:00", "В", "Г")])
    v2 = v1 + "\n" + M("21:00", "Д", "Е")
    with _Env() as env:
        s1 = snap(30, L(20, 11), v1)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda _i: env.apply([s1], now=L(20, 11, 1)), range(16)))
        check("16 одновременных одинаковых событий → ровно 2 записи, без дублей", len(env.sched()) == 2)
        ids_once = sorted(p.id for p in env.sched())
        s2 = snap(30, L(20, 11), v2, edited=L(20, 12))
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(lambda sn: env.apply([sn], now=L(20, 12, 1)), [s1, s2] * 8))
        check("Правка и устаревший снимок вперемешку → итог = версия после правки (3 матча)",
              len(env.sched()) == 3 and all(i in [p.id for p in env.sched()] for i in ids_once))
        st = env.apply([s1], now=L(20, 12, 2))
        check("Устаревший снимок (до правки) игнорируется и не затирает свежие данные",
              st["outdated_event"] == 1 and len(env.sched()) == 3)
    with _Env() as env:
        # перечит и событие on_message одновременно на одной и той же версии
        s1 = snap(31, L(20, 11), v1)
        errs = []

        def run(kind):
            try:
                env.apply([s1], now=L(20, 11, 1), full_scan=(kind == "recheck"), scan_floor=datetime.min)
            except Exception as e:
                errs.append(e)
        ts = [threading.Thread(target=run, args=(k,)) for k in ["recheck", "event"] * 6]
        [t.start() for t in ts]
        [t.join() for t in ts]
        check("Перечит и on_message параллельно: без ошибок и дублей", not errs and len(env.sched()) == 2)


def test_midnight_flow():
    print("\n[Переход через полночь на уровне расписания]")
    with _Env() as env:
        content = "\n".join([M("23:30", "А", "Б"), M("00:30", "В", "Г"), M("03:00", "Д", "Е"), M("12:00", "Ж", "З")])
        env.apply([snap(40, L(20, 23, 10), content)], now=L(20, 23, 11))
        check("Пост 20.10 23:10: 23:30 сегодня; 00:30, 03:00, 12:00 — уже 21.10",
              env.times() == ["20 23:30", "21 00:30", "21 03:00", "21 12:00"])
        env.apply([snap(40, L(20, 23, 10), content)], now=L(21, 0, 5))
        check("После полуночи те же записи на тех же датах (ничего не «сдвинулось на сегодня»)",
              env.times() == ["20 23:30", "21 00:30", "21 03:00", "21 12:00"])
    with _Env() as env:
        # первое чтение после полуночи: пост вчерашний, дата считается от публикации
        content = M("23:30", "А", "Б") + "\n" + M("12:00", "Ж", "З")
        st = env.apply([snap(41, L(20, 23, 10), content)], now=L(21, 0, 30))
        check("Бот поднялся в 00:30 и читает пост 23:10: 23:30 прошёл, 12:00 — 21.10",
              env.times() == ["21 12:00"] and st["skipped_stale"] == 1)


# ── Матчинг прогноза с событием Фонбета: пороги и «тот же слот» ───────────────

def _ts(dt):
    return int((dt - datetime(1970, 1, 1)).total_seconds())


def _ev(t1, t2, start, league="Poland. 4 Liga. Warmia", live=False, eid=1):
    return {"id": eid, "team1": t1, "team2": t2, "start_time": _ts(start), "is_live": live,
            "live_reason": "line", "league_name": league, "odd_p1": 2.0, "odd_p2": 3.0, "factors": []}


def test_fonbet_matching_tiers():
    print("\n[Фонбет: пороги совпадения и «тот же слот»]")
    # В тестовом окружении token_sort_ratio = доля общих слов (Jaccard) → проценты считаются точно
    start = L(20, 16)                                     # матч в 16:00 МСК
    pred = "Poland. 4 Liga. Warmia 16-00 alpha beta gamma — delta epsilon zeta п1 2+"
    find = fonbet.find_matching_event

    check("Базовый порог понижен 80 → 60", fonbet.FUZZY_THRESHOLD == 60)
    check("Порог «того же слота» = 50", fonbet.FUZZY_THRESHOLD_SAME_SLOT == 50)

    # 66% — раньше (порог 80) не находился, теперь base
    ev66 = _ev("alpha beta", "delta epsilon", start, league=None)
    r = find(pred, [ev66], expected_utc=start)
    check("Совпадение 66% (раньше не находилось) → найдено как base, лига не нужна",
          r is not None and r["match_tier"] == "base" and r["match_score"] == 66)

    # 50%: два слова из трёх у каждой команды (2/4)
    ev50 = _ev("alpha beta kappa", "delta epsilon omega", start)
    r = find(pred, [ev50], expected_utc=start)
    check("50% + то же время + та же страна/лига → найдено (same_slot)",
          r is not None and r["match_tier"] == "same_slot" and r["match_score"] == 50)
    check("Оригинальное событие не мутируется (возвращается копия)", "match_tier" not in ev50)

    check("50%, но другая лига → не найдено",
          find(pred, [_ev("alpha beta kappa", "delta epsilon omega", start, league="Poland. Ekstraklasa")],
               expected_utc=start) is None)
    check("50%, но другая страна → не найдено",
          find(pred, [_ev("alpha beta kappa", "delta epsilon omega", start, league="Germany. 4 Liga. Warmia")],
               expected_utc=start) is None)
    check("50%, лига неизвестна (нет справочника sports) → не найдено, как раньше",
          find(pred, [_ev("alpha beta kappa", "delta epsilon omega", start, league=None)], expected_utc=start) is None)
    check("50%, время старта отличается на 30 мин → не найдено",
          find(pred, [_ev("alpha beta kappa", "delta epsilon omega", start + timedelta(minutes=30))],
               expected_utc=start) is None)
    check("50%, но времени прогноза нет (expected_utc=None) → пониженный порог не работает",
          find(pred, [ev50], expected_utc=None) is None)
    check("Ниже 50% → не найдено даже в той же лиге и в то же время",
          find(pred, [_ev("alpha kappa lambda", "delta sigma omega", start)], expected_utc=start) is None)
    check("Возрастной маркер всё ещё обязателен: прогноз (20) ≠ событие основы",
          find("Poland. 4 Liga. Warmia 16-00 alpha beta gamma (20) — delta epsilon zeta (20) п1 2+",
               [ev50], expected_utc=start) is None)

    # Много матчей в одном слоте
    other_far = _ev("kappa lambda", "sigma omega", start, eid=2)
    r = find(pred, [other_far, ev50], expected_utc=start)
    check("В слоте несколько матчей, наш явно ближе → найден правильный", r is not None and r["id"] == 1)
    twin = _ev("alpha beta lambda", "delta epsilon sigma", start, eid=3)
    check("Два одинаково похожих кандидата в том же слоте → неоднозначно, не найдено",
          find(pred, [ev50, twin], expected_utc=start) is None)
    check("Неоднозначность не мешает обычному (base) совпадению",
          (find(pred, [ev66, twin], expected_utc=start) or {}).get("match_tier") == "base")

    # Прежнее поведение при высоком совпадении
    ev100 = _ev("alpha beta gamma", "delta epsilon zeta", start, league=None)
    check("100% совпадение → base", (find(pred, [ev100], expected_utc=start) or {}).get("match_tier") == "base")
    check("Команды в обратном порядке тоже находятся",
          find(pred, [_ev("delta epsilon zeta", "alpha beta gamma", start, league=None)], expected_utc=start) is not None)


def test_league_detection():
    print("\n[Фонбет: страна и лига события]")
    lm = fonbet.league_matches
    check("Страна+лига+подлига совпадают", lm("Poland. 4 Liga. Warmia-Masuria Voivodeship 16-00 A — B",
                                              "Poland. 4 Liga. Warmia-Masuria Voivodeship"))
    check("На Фонбете короче («Poland. 4 Liga»), в прогнозе — с подлигой", lm(
        "Poland. 4 Liga. Warmia-Masuria Voivodeship 16-00 A — B", "Poland. 4 Liga"))
    check("Префикс «Футбол.» в прогнозе не мешает", lm("Футбол. Poland. 4 Liga. 16-00 A — B", "Poland. 4 Liga"))
    check("Только страна в прогнозе — достаточно страны", lm("Belarus. 18-00 A — B", "Belarus. Second League"))
    check("Другая лига той же страны — нет", not lm("Poland. 4 Liga. 16-00 A — B", "Poland. Ekstraklasa"))
    check("Другая страна — нет", not lm("Poland. 4 Liga. 16-00 A — B", "Germany. 4 Liga"))
    check("Название лиги неизвестно — нет", not lm("Poland. 4 Liga. 16-00 A — B", None))
    check("Разные алфавиты (прогноз «Нигерия», лига «Nigeria») не сопоставляются",
          not lm("Футбол. Нигерия. Кубок. 18-00 А — Б", "Nigeria. Cup"))
    check("Скобки в названии страны", lm("Myanmar (Burma). Youth League U20 15-00 A — B", "Myanmar (Burma). Youth League U20"))

    data = {"sports": [
        {"id": 1, "name": "Football", "kind": "sport"},
        {"id": 10, "parentId": 1, "name": "Poland"},
        {"id": 11, "parentId": 10, "name": "4 Liga"},
        {"id": 20, "parentId": 1, "name": "England"},
        {"id": 21, "parentId": 20, "name": "England. Premier League"},
    ]}
    names = fonbet._build_league_names(data)
    check("Справочник sports: «Страна. Лига» собирается по цепочке parentId, корень «Football» отброшен",
          names[11] == "Poland. 4 Liga")
    check("Дублирующийся префикс не повторяется", names[21] == "England. Premier League")
    check("Нет справочника sports → пусто (работает только базовый порог)", fonbet._build_league_names({"events": []}) == {})


def test_monitor_window_retention():
    print("\n[Окно мониторинга: прогноз живёт ≥ 15 минут после старта]")
    rm = match_dates.retention_minutes
    check("DELETE_AFTER_MINUTES=5 → прогноз живёт 16 мин (окно 15 + запас)", rm(5) == 16)
    check("Не задано/мусор → тот же минимум", rm(None) == 16 and rm("abc") == 16)
    check("Большее значение из окружения сохраняется", rm(30) == 30)
    check("Окно мониторинга планировщика = 15 мин", scheduler.FONBET_WINDOW_AFTER_MIN == 15)
    check("Дедлайн ожидания кэфов в лайве укладывается в срок жизни прогноза",
          scheduler.FONBET_WINDOW_AFTER_MIN - 5 < rm(5) and 10 < rm(5))

    content = M("14:00", "А", "Б")
    with _Env() as env:
        env.apply([snap(60, L(20, 11), content)], now=L(20, 14, 10))
        check("Матч начался 10 мин назад — ещё в расписании (раньше срезался на 5-й минуте)", env.times() == ["20 14:00"])
    with _Env() as env:
        st = env.apply([snap(61, L(20, 11), content)], now=L(20, 14, 17))
        check("Матч начался 17 мин назад — уже за окном, не добавляется", env.times() == [] and st["skipped_stale"] == 1)


# ── Апгрейд: здоровье, фильтры, журнал, алиасы, неопределённые даты, тик ─────

import asyncio
import time as _time
import health
import userprefs


def test_models_new_fields():
    print("\n[Модели: новые поля обратно совместимы]")
    old = models.Prediction.from_dict({"id": "a", "text": "t", "match_time": "2026-10-21T10:00:00"})
    check("Старая запись без новых полей читается (fonbet_matched=False, state=None)",
          old.fonbet_matched is False and old.fonbet_state is None)
    p = models.Prediction(text="t", match_time=datetime(2026, 10, 21, 10), fonbet_matched=True, fonbet_state="live")
    check("fonbet_matched/state переживают to_dict → from_dict",
          models.Prediction.from_dict(p.to_dict()).fonbet_state == "live")
    s = models.UserSettings.from_dict({"chat_id": 1})
    check("Старые настройки: тихие часы выключены", s.quiet_from is None and s.quiet_to is None)
    r = models.UserSettings.from_dict(models.UserSettings(chat_id=1, quiet_from=23, quiet_to=8).to_dict())
    check("Тихие часы переживают сохранение", (r.quiet_from, r.quiet_to) == (23, 8))
    legacy = models.UserSettings.from_dict({"chat_id": 1, "muted_ids": ["x"], "blocked_countries": ["poland"]})
    check("Заглушка матчей и блокировка стран удалены: лишние ключи в старых данных не мешают",
          legacy.chat_id == 1 and not hasattr(legacy, "muted_ids") and not hasattr(legacy, "blocked_countries")
          and "muted_ids" not in legacy.to_dict())


def test_health_alerts():
    print("\n[Здоровье: алерты админам]")
    health.reset()
    t0 = _time.time() + 10_000          # далеко за периодом «после старта не тревожим»
    check("В первые минуты после старта тревог нет", health.evaluate(_time.time()) == [])

    for i in range(health.FONBET_FAIL_STREAK - 1):
        health.fonbet_result(False, 0, "HTTP 503", now=t0 + i)
    check("4 неудачи подряд — ещё не авария", health.evaluate(t0 + 10) == [])
    health.fonbet_result(False, 0, "HTTP 503", now=t0 + 11)
    ev = health.evaluate(t0 + 12)
    check("5-я неудача подряд → ровно одна тревога про Фонбет с причиной",
          len(ev) == 1 and ev[0][:2] == ("fonbet_down", "raise") and "HTTP 503" in ev[0][2])
    health.fonbet_result(False, 0, "HTTP 503", now=t0 + 30)
    check("Тревога не повторяется, пока авария продолжается", health.evaluate(t0 + 60) == [])
    health.fonbet_result(True, 3500, now=t0 + 300)
    ev = health.evaluate(t0 + 301)
    check("Фонбет ожил → одно сообщение «снова отвечает» с длительностью",
          len(ev) == 1 and ev[0][1] == "clear" and "снова отвечает" in ev[0][2] and "мин" in ev[0][2])
    check("И больше событий нет", health.evaluate(t0 + 400) == [])

    health.reset()
    check("Discord отключён в конфиге → его тревог нет",
          all(not e[0].startswith("discord") for e in health.evaluate(t0 + 5000)))
    health.discord_enabled(True)
    health.discord_connected(True, now=t0)
    health.discord_sync(True, {"added": 1}, now=t0)
    check("Всё в порядке → тишина", health.evaluate(t0 + 600) == [])
    health.discord_connected(False, now=t0 + 700)
    check("Короткий реконнект (<3 мин) — не авария", health.evaluate(t0 + 800) == [])
    ev = health.evaluate(t0 + 700 + health.DISCORD_DOWN_SEC + 5)
    check("Нет соединения дольше 3 мин → тревога", any(e[0] == "discord_down" and e[1] == "raise" for e in ev))
    health.discord_connected(True, now=t0 + 1200)
    ev = health.evaluate(t0 + 1210)
    check("Соединение вернулось → «снова подключён»", any(e[0] == "discord_down" and e[1] == "clear" for e in ev))

    health.reset()
    health.discord_enabled(True)
    health.discord_connected(True, now=t0)
    ev = health.evaluate(t0 + health.DISCORD_SYNC_STALE_SEC + 60)
    check("Перечит не проходил >30 мин → тревога discord_stale", any(e[0] == "discord_stale" for e in ev))
    health.discord_sync(True, {"added": 0}, now=t0 + health.DISCORD_SYNC_STALE_SEC + 100)
    ev = health.evaluate(t0 + health.DISCORD_SYNC_STALE_SEC + 110)
    check("Перечит прошёл → тревога снята", any(e[0] == "discord_stale" and e[1] == "clear" for e in ev))
    health.reset()


def test_userprefs():
    print("\n[Личный фильтр: тихие часы, статус матча]")
    q = userprefs.in_quiet_hours
    at = lambda h, m=0: L(20, h, m)         # локальное (МСК) время → UTC
    check("Тихие часы 23→8: 23:30 внутри", q(23, 8, at(23, 30), 3))
    check("…03:00 внутри (через полночь)", q(23, 8, at(3), 3))
    check("…08:00 уже снаружи (конец не включён)", not q(23, 8, at(8), 3))
    check("…12:00 снаружи", not q(23, 8, at(12), 3))
    check("Обычный интервал 13→15", q(13, 15, at(14), 3) and not q(13, 15, at(15), 3))
    check("Часы считаются по поясу пользователя (UTC+10: 23:00 UTC = 09:00 местн.)", not q(23, 8, datetime(2026, 10, 20, 23, 0), 10))
    check("Не заданы → не тихо", not q(None, None, at(3), 3))

    check("/quiet 23 8", userprefs.parse_quiet_args(["23", "8"]) == ("set", 23, 8))
    check("/quiet off", userprefs.parse_quiet_args(["off"])[0] == "off")
    check("/quiet без аргументов — показать", userprefs.parse_quiet_args([])[0] == "show")
    check("/quiet 25 8 и 5 5 — ошибка", userprefs.parse_quiet_args(["25", "8"])[0] == "error"
          and userprefs.parse_quiet_args(["5", "5"])[0] == "error" and userprefs.parse_quiet_args(["a", "b"])[0] == "error")

    base = models.UserSettings(chat_id=1)
    quiet = models.UserSettings(chat_id=1, quiet_from=23, quiet_to=8)
    check("Без тихих часов можно слать", userprefs.user_allows(base, L(20, 12)))
    check("Вне тихих часов можно слать", userprefs.user_allows(quiet, L(20, 12)))
    check("В тихие часы — нельзя", not userprefs.user_allows(quiet, L(20, 2)))
    check("Тихие часы считаются по поясу пользователя",
          userprefs.user_allows(models.UserSettings(chat_id=1, quiet_from=23, quiet_to=8, timezone_offset=10),
                                datetime(2026, 10, 20, 23, 0)))
    check("Заглушки матчей и стран удалены из userprefs",
          not hasattr(userprefs, "country_key") and not hasattr(userprefs, "normalize_country"))

    mk = lambda **kw: models.Prediction(text="t", match_time=L(20, 18), **kw)
    now = L(20, 17)
    check("Статус: далеко до старта", userprefs.match_status(mk(), L(20, 10))[0] == "⏳")
    check("Статус: ищу", userprefs.match_status(mk(), now)[0] == "🔎")
    check("Статус: в линии", userprefs.match_status(mk(fonbet_matched=True, fonbet_state="line"), now)[1] == "в линии")
    check("Статус: в лайве", userprefs.match_status(mk(fonbet_matched=True, fonbet_state="live"), L(20, 18, 5))[1] == "в лайве")
    check("Статус: не найден после старта", userprefs.match_status(mk(), L(20, 18, 12))[0] == "❓")


def test_fonbet_aliases_diag():
    print("\n[Фонбет: время-первым, алиасы, диагностика промахов]")
    find = fonbet.find_matching_event
    start = L(20, 16)
    pred = "Poland. 4 Liga. Warmia 16-00 alpha beta gamma — delta epsilon zeta п1 2+"

    # время — первым: событие с идеальными названиями, но в другое время, не принимается
    far = _ev("alpha beta gamma", "delta epsilon zeta", start + timedelta(hours=2), league=None)
    check("Идеальные названия, но старт на 2 часа позже → не найдено", find(pred, [far], expected_utc=start) is None)
    check("Без ожидаемого времени фильтр по времени не применяется (как раньше)", find(pred, [far]) is not None)
    many = [_ev(f"zz{i} yy", f"xx{i} ww", start + timedelta(minutes=i * 7), league=None, eid=100 + i) for i in range(40)]
    r = find(pred, many + [_ev("alpha beta gamma", "delta epsilon zeta", start, league=None, eid=1)], expected_utc=start)
    check("Среди множества чужих событий находит нужное", r is not None and r["id"] == 1)

    check("prediction_script: английский", fonbet.prediction_script(pred) == "en")
    check("prediction_script: русский", fonbet.prediction_script("Футбол. Нигерия. Кубок. 18-00 Альфа Бета — Гамма Дельта п2 3+") == "ru")
    check("prediction_script: «п2» в английском прогнозе не делает его русским",
          fonbet.prediction_script("Poland. 4 Liga. 16-00 Sokol Ostroda — DKS Dobre Miasto п2 5+") == "en")

    # алиасы
    ev0 = _ev("kappa lambda", "sigma omega", start, league=None)
    al = {fonbet.alias_norm("alpha beta gamma"): [fonbet.alias_norm("kappa lambda")],
          fonbet.alias_norm("delta epsilon zeta"): [fonbet.alias_norm("sigma omega")]}
    check("Без алиасов совсем разные названия не находятся", find(pred, [ev0], expected_utc=start) is None)
    r = find(pred, [ev0], expected_utc=start, aliases=al)
    check("Обе команды известны как алиасы → найдено как base (100%)", r is not None and r["match_tier"] == "base" and r["match_score"] == 100)
    half = {k: v for k, v in list(al.items())[:1]}
    check("Известна одна команда (50%) и лига неизвестна → не найдено, одного алиаса мало без лиги",
          find(pred, [ev0], expected_utc=start, aliases=half) is None)
    check("Известна одна команда (50%) + то же время + та же лига → найдено",
          find(pred, [_ev("kappa lambda", "sigma omega", start)], expected_utc=start, aliases=half) is not None)

    # какие пары запоминать
    ev50 = _ev("alpha beta kappa", "delta epsilon omega", start)
    r = find(pred, [ev50], expected_utc=start)
    check("same_slot (50%/50%): запоминаются обе пары названий",
          sorted(r["alias_pairs"]) == sorted([("alpha beta gamma", "alpha beta kappa"), ("delta epsilon zeta", "delta epsilon omega")]))
    pred2 = "Poland. 4 Liga. Warmia 16-00 p q r s — m n o п1 2+"
    r = find(pred2, [_ev("p q r s t", "m n z", start, league=None)], expected_utc=start)
    check("base 80%/50%: запоминается только слабая сторона",
          r["match_tier"] == "base" and r["alias_pairs"] == [("m n o", "m n z")])
    r = find(pred, [_ev("alpha beta gamma", "delta epsilon zeta", start, league=None)], expected_utc=start)
    check("100%/100%: запоминать нечего", r["alias_pairs"] == [])

    # диагностика промахов
    def miss(events, **kw):
        d = {}
        res = find(pred, events, expected_utc=start, diag=d, **kw)
        return res, d
    res, d = miss([])
    check("Диагностика: пустой список событий", res is None and d["reason"] == "no_events")
    res, d = miss([_ev("qqq www", "eee rrr", start)])
    check("Диагностика: похожих команд нет", d["reason"] == "no_similar")
    res, d = miss([_ev("alpha beta gamma", "delta epsilon zeta", start + timedelta(minutes=40))])
    check("Диагностика: названия идеальны, но время не то → «время … 40 мин»",
          d["reason"] == "time" and "40 мин" in d["best"][0]["why"] and d["best"][0]["score"] == 100)
    res, d = miss([_ev("alpha beta kappa", "delta epsilon omega", start, league="Germany. 4 Liga")])
    check("Диагностика: 50%, лига чужая → «страна/лига не совпали»", d["reason"] == "league" and "Germany" in d["best"][0]["why"])
    res, d = miss([_ev("alpha beta kappa", "delta epsilon omega", start, league=None)])
    check("Диагностика: 50%, лига неизвестна → так и сказано", d["reason"] == "league" and "неизвестна" in d["best"][0]["why"])
    res, d = miss([_ev("alpha beta x y", "delta epsilon x y", start)])   # по 40% на команду
    check("Диагностика: ниже 50% → «названия N% — ниже 50%»", d["reason"] == "name" and "50%" in d["best"][0]["why"])
    res, d = miss([_ev("alpha beta kappa", "delta epsilon omega", start, eid=1), _ev("alpha beta lambda", "delta epsilon sigma", start, eid=2)])
    check("Диагностика: неоднозначно", res is None and d["reason"] == "ambiguous")
    res, d = miss([_ev("alpha beta gamma", "delta epsilon zeta", start)])
    check("Найдено → diag не заполняется", res is not None and d == {})
    ru_pred = "Футбол. Польша. 16-00 Альфа Бета — Дельта Эпсилон п1 2+"
    d = {}
    find(ru_pred, [_ev("Alpha Beta", "Delta Epsilon", start)], expected_utc=start, diag=d)
    check("Русский прогноз против английского списка не находится (поэтому нужен lang=ru)", d.get("reason") in ("no_similar", "name"))
    check("URL запроса получает нужный язык", "lang=ru" in fonbet._endpoint_url("h.example", "ru") and "lang=en" in fonbet._endpoint_url("h.example"))


def test_storage_journal_aliases_settings():
    print("\n[Хранилище: журнал, алиасы, личные настройки]")
    with _Env() as env:
        for i in range(205):
            storage.journal_add({"kind": "miss" if i % 2 else "found", "tier": "base", "reason": "time", "n": i})
        j = storage.journal_recent(300)
        check("Журнал — кольцо на 200 записей", len(j) == storage.JOURNAL_MAX)
        check("Новые первыми, старые вытеснены", j[0]["n"] == 204 and j[-1]["n"] == 5)
        st = storage.journal_stats()
        check("Статистика журнала по видам, уровням и причинам",
              st["found"] + st["miss"] == 200 and st["tiers"] == {"base": st["found"]} and st["reasons"] == {"time": st["miss"]})
        check("Фильтр по виду", all(e["kind"] == "miss" for e in storage.journal_recent(10, "miss")))
        # журнал не затирает расписание и наоборот
        storage.save_predictions(predictions=[models.Prediction(text="x", match_time=L(20, 12))])
        storage.journal_add({"kind": "found"})
        check("Запись журнала не стирает расписание", len(env.sched()) == 1)
        storage.save_predictions(predictions=[])
        check("Сохранение расписания не стирает журнал", len(storage.journal_recent(300)) == 200)

    with _Env() as env:
        check("Алиасов пока нет", storage.load_aliases() == {} and storage.list_aliases() == [])
        n = storage.add_aliases([("Dinamo Kyiv", "Dynamo Kyiv"), ("Dinamo Kyiv", "Dynamo Kyiv"), ("same", "SAME"), ("", "x")])
        check("Запоминаются пары (одинаковые после нормализации и пустые — нет)", n == 2 and len(storage.list_aliases()) == 1)
        al = storage.load_aliases()
        check("load_aliases отдаёт нормализованные ключи", al == {"dinamokyiv": ["dynamokyiv"]})
        storage.add_aliases([("dinamo kyiv", "Dynamo Kiev")])
        check("Кэш сброшен — новая пара видна сразу, счётчик растёт",
              len(storage.load_aliases()["dinamokyiv"]) == 2 and storage.list_aliases()[0]["n"] == 3)
        check("Удаление алиаса по названию (любой регистр)", storage.remove_alias("DINAMO  kyiv") and storage.load_aliases() == {})
        check("Удаление несуществующего", storage.remove_alias("нет такого") is False)

    with _Env() as env:
        storage.update_settings(7, lambda s: setattr(s, "timezone_offset", 0))

        def bump(_i):
            storage.update_settings(7, lambda s: setattr(s, "timezone_offset", s.timezone_offset + 1))
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(bump, range(20)))
        check("update_settings атомарен: 20 параллельных увеличений — ни одно не потеряно",
              storage.load_settings(7).timezone_offset == 20)
        storage.update_settings(7, lambda s: setattr(s, "quiet_from", 23))
        check("Прочие поля при этом сохраняются", storage.load_settings(7).quiet_from == 23
              and storage.load_settings(7).timezone_offset == 20)


def test_undetermined_buttons_flow():
    print("\n[Неопределённая дата → вопрос админу кнопками]")
    content = M("14:00", "А", "Б")
    snp = snap(70, L(20, 11), content, edited=L(20, 23))      # бот увидел уже правленое сообщение
    now = L(20, 23, 5)
    with _Env() as env:
        st = env.apply([snp], now=now)
        asks = st["undetermined_new"]
        check("Неопределённый матч попал в вопросы админу (один)", env.times() == [] and len(asks) == 1 and asks[0]["mid"] == "70")
        st = env.apply([snp], now=now + timedelta(minutes=15), full_scan=True, scan_floor=datetime.min)
        check("Повторный перечит не спрашивает второй раз", st["undetermined_new"] == [])
        snp2 = snap(70, L(20, 11), content + "\n" + M("20:00", "В", "Г"), edited=L(20, 23, 30))
        st = env.apply([snp2], now=now + timedelta(minutes=40))
        asks2 = st["undetermined_new"]
        check("Правка сообщения: про прежний матч не спрашивает, про новый — спрашивает",
              all("В —" in a["text"] or "В" in a["text"] for a in asks2) and not any("А —" in a["text"] for a in asks2))

    with _Env() as env:
        env.apply([snp], now=now)
        item = storage.list_undetermined()[0]
        btns = storage.ud_buttons(item, now=now, tz=TZ)
        check("Три кнопки: сегодня, завтра, пропустить", [b[0] for b in btns] == ["📅 Сегодня 20.10", "📅 Завтра 21.10", "🚫 Пропустить"])
        check("callback_data укладывается в 64 байта Telegram", all(len(b[1].encode()) <= 64 for b in btns))
        res = storage.resolve_undetermined(item["mid"], item["h8"], btns[1][1].split("|")[3], now=now, tz=TZ)
        check("«Завтра» → матч добавлен на 21.10 14:00 с основанием admin",
              res["status"] == "added" and env.times() == ["21 14:00"] and env.sched()[0].date_basis == "admin")
        env.apply([snap(70, L(20, 11), content + " ✅", edited=L(20, 23, 50))], now=now + timedelta(minutes=45))
        check("Правка сообщения не снимает выбранную админом дату", env.times() == ["21 14:00"])
        check("Решённый матч больше не в списке неопределённых", storage.list_undetermined() == [])
        again = storage.resolve_undetermined(item["mid"], item["h8"], "261021", now=now, tz=TZ)
        check("Повторное нажатие кнопки безопасно", again["status"] == "notfound" and len(env.sched()) == 1)

    with _Env() as env:
        env.apply([snp], now=now)
        item = storage.list_undetermined()[0]
        res = storage.resolve_undetermined(item["mid"], item["h8"], "261020", now=now, tz=TZ)
        check("Дата, на которую матч уже прошёл → не добавляется (past)", res["status"] == "past" and env.times() == [])
        check("…и вопрос остаётся открытым", len(storage.list_undetermined()) == 1)
        res = storage.resolve_undetermined(item["mid"], item["h8"], "s", now=now, tz=TZ)
        check("«Пропустить» → снят с очереди, в расписание не попал",
              res["status"] == "skipped" and storage.list_undetermined() == [] and env.times() == [])
        st = env.apply([snap(70, L(20, 11), content, edited=L(20, 23, 20))], now=now + timedelta(minutes=30))
        check("После «Пропустить» бот про этот матч больше не спрашивает", st["undetermined_new"] == [])
    with _Env() as env:
        check("Неизвестная кнопка/сообщение → notfound", storage.resolve_undetermined("999", "deadbeef", "s")["status"] == "notfound")


class _Recorder:
    """Подмена scheduler._broadcast: запоминает, кому и что ушло."""
    def __init__(self):
        self.sent = []

    async def __call__(self, app, chat_ids, text, url=None, pred=None):
        self.sent.append({"to": list(chat_ids), "text": text, "pred": pred.id if pred else None, "url": url})


def _tick_env(events_by_lang, recipients=(111, 222)):
    """Готовит scheduler к прогону тика: фейковые Фонбет, рассылка, получатели."""
    rec = _Recorder()
    calls = []

    def fake_fetch(lang="en"):
        calls.append(lang)
        return events_by_lang.get(lang, [])
    saved = (scheduler.fetch_events, scheduler._broadcast, storage.get_all_recipient_chat_ids)
    scheduler.fetch_events, scheduler._broadcast = fake_fetch, rec
    storage.get_all_recipient_chat_ids = lambda: list(recipients)
    scheduler._sent_prematch.clear(); scheduler._sent_live.clear()
    scheduler._sent_crooked.clear(); scheduler._sent_close_match.clear()
    scheduler._last_diag.clear(); scheduler._diag_ts.clear()
    health.reset()

    def restore():
        scheduler.fetch_events, scheduler._broadcast, storage.get_all_recipient_chat_ids = saved
    return rec, calls, restore


def _utc_event(t1, t2, start_utc, league="Poland. 4 Liga. Warmia", eid=1):
    ev = _ev(t1, t2, start_utc, league=league, eid=eid)
    ev["league_id"] = 777
    return ev


def test_scheduler_fonbet_tick_integration():
    print("\n[Планировщик: тик Фонбета — поиск, журнал, алиасы, фильтры]")
    now = datetime.utcnow()
    start = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    pred_text = "Poland. 4 Liga. Warmia 16-00 alpha beta gamma — delta epsilon zeta п1 2+"

    with _Env() as env:
        pred = models.Prediction(text=pred_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        events = {"en": [_utc_event("alpha beta kappa", "delta epsilon omega", start)]}
        rec, calls, restore = _tick_env(events)
        try:
            asyncio.run(scheduler._fonbet_tick_inner(None))
            line_msgs = [m for m in rec.sent if "ЛИНИЮ" in m["text"]]
            check("Найден по пониженному порогу (same_slot) → уведомление «вышел в линию»", len(line_msgs) == 1)
            check("Уведомление получили оба получателя", line_msgs[0]["to"] == [111, 222])
            saved_pred = env.sched()[0]
            check("Статус сохранён: найден, «в линии»", saved_pred.fonbet_matched and saved_pred.fonbet_state == "line")
            j = storage.journal_recent(5, "found")
            check("Журнал: запись «найден» с уровнем same_slot, 50% и лигой",
                  len(j) == 1 and j[0]["tier"] == "same_slot" and j[0]["score"] == 50 and "Warmia" in (j[0]["league"] or ""))
            check("Алиасы запомнены (обе пары)", len(storage.list_aliases()) == 2)
            check("Здоровье: Фонбет отвечает", health.snapshot()["fonbet"]["streak"] == 0 and health.snapshot()["fonbet"]["events"] == 1)
            check("Русский список не запрашивался для английского прогноза", calls == ["en"])

            n = len(rec.sent)
            asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Следующий тик: повторных уведомлений и записей журнала нет",
                  len([m for m in rec.sent if "ЛИНИЮ" in m["text"]]) == 1 and len(storage.journal_recent(10, "found")) == 1)
        finally:
            restore()

    # Алиас помогает следующему матчу тех же команд, даже без лиги
    with _Env() as env:
        storage.add_aliases([("alpha beta gamma", "kappa lambda"), ("delta epsilon zeta", "sigma omega")])
        pred = models.Prediction(text=pred_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        rec, calls, restore = _tick_env({"en": [_utc_event("kappa lambda", "sigma omega", start, league=None)]}, recipients=(111,))
        try:
            asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Запомненные названия работают: совсем другие имена найдены по алиасам",
                  len([m for m in rec.sent if "ЛИНИЮ" in m["text"]]) == 1)
        finally:
            restore()

    # Русский прогноз → второй запрос lang=ru
    with _Env() as env:
        ru_text = "Футбол. Польша. 16-00 Альфа Бета — Дельта Эпсилон п1 2+"
        pred = models.Prediction(text=ru_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        rec, calls, restore = _tick_env({
            "en": [_utc_event("Alfa Beta", "Delta Epsilon", start, league="Poland. 4 Liga", eid=1)],
            "ru": [_utc_event("Альфа Бета", "Дельта Эпсилон", start, league="Польша. 4 Лига", eid=1)],
        }, recipients=(111,))
        try:
            asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Прогноз на русском: запрошен lang=ru и найден по русским названиям",
                  "ru" in calls and len([m for m in rec.sent if "ЛИНИЮ" in m["text"]]) == 1)
        finally:
            restore()

    # Тихие часы
    with _Env() as env:
        pred = models.Prediction(text=pred_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        hour = (now + timedelta(hours=TZ)).hour
        storage.update_settings(111, lambda s: (setattr(s, "quiet_from", hour), setattr(s, "quiet_to", (hour + 2) % 24)))
        rec, calls, restore = _tick_env({"en": [_utc_event("alpha beta gamma", "delta epsilon zeta", start)]})
        try:
            asyncio.run(scheduler._fonbet_tick_inner(None))
            line = [m for m in rec.sent if "ЛИНИЮ" in m["text"]]
            check("Тихие часы у одного получателя → уведомление идёт только второму, матч найден",
                  len(line) == 1 and line[0]["to"] == [222] and env.sched()[0].fonbet_matched)
        finally:
            restore()

    # Фонбет молчит → здоровье
    with _Env() as env:
        pred = models.Prediction(text=pred_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        rec, calls, restore = _tick_env({"en": []})
        try:
            for _ in range(health.FONBET_FAIL_STREAK):
                asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Фонбет вернул пусто 5 тиков подряд → счётчик неудач в health",
                  health.snapshot()["fonbet"]["streak"] == health.FONBET_FAIL_STREAK)
        finally:
            restore()


def test_scheduler_miss_journal():
    print("\n[Планировщик: журнал «почему не нашёл»]")
    now = datetime.utcnow()
    start = (now + timedelta(minutes=5)).replace(second=0, microsecond=0)
    pred_text = "Poland. 4 Liga. Warmia 16-00 alpha beta gamma — delta epsilon zeta п1 2+"
    with _Env() as env:
        pred = models.Prediction(text=pred_text, match_time=start, source="manual")
        storage.save_predictions(predictions=[pred])
        rec, calls, restore = _tick_env({"en": [_utc_event("alpha beta gamma", "delta epsilon zeta", start + timedelta(minutes=45))]})
        try:
            asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Промах в тике: причина записана в память (время не то)", scheduler._last_diag[pred.id]["reason"] == "time")
            asyncio.run(scheduler._fonbet_tick_inner(None))
            check("Подробная диагностика не чаще раза в пару минут (второй тик её не пересчитывает)",
                  len(scheduler._diag_ts) == 1)
            storage.update_prediction(pred_id=pred.id, match_time=now - timedelta(minutes=30))
            asyncio.run(scheduler.notification_tick(None))
            miss = storage.journal_recent(5, "miss")
            check("Матч ушёл из расписания ненайденным → промах в журнале с причиной и кандидатами",
                  len(miss) == 1 and miss[0]["reason"] == "time" and "45 мин" in miss[0]["why"] and miss[0]["best"])
            check("Прогноз из расписания удалён", env.sched() == [])
        finally:
            restore()
    with _Env() as env:
        found = models.Prediction(text=pred_text, match_time=now - timedelta(minutes=30), source="manual", fonbet_matched=True)
        storage.save_predictions(predictions=[found])
        rec, calls, restore = _tick_env({"en": []})
        try:
            asyncio.run(scheduler.notification_tick(None))
            check("Найденный ранее матч при чистке в журнал промахов не попадает", storage.journal_recent(5, "miss") == [])
        finally:
            restore()
    with _Env() as env:
        rec, calls, restore = _tick_env({"en": []})
        try:
            tz_pred = models.Prediction(text=pred_text, match_time=(now + timedelta(minutes=30)).replace(second=0, microsecond=0), source="manual")
            storage.save_predictions(predictions=[tz_pred])
            storage.update_settings(222, lambda s: setattr(s, "notify_reminders", True))
            asyncio.run(scheduler.notification_tick(None))
            r = [m for m in rec.sent if "30" in m["text"] or "мин" in m["text"]]
            check("Напоминание за 30 минут приходит обоим получателям", r and r[0]["to"] == [111, 222])
            hour = (datetime.utcnow() + timedelta(hours=TZ)).hour
            storage.update_settings(222, lambda s: (setattr(s, "quiet_from", hour), setattr(s, "quiet_to", (hour + 2) % 24)))
            storage.update_prediction(pred_id=tz_pred.id, notified_30=False)
            rec.sent.clear()
            asyncio.run(scheduler.notification_tick(None))
            check("Напоминание не уходит тому, у кого сейчас тихие часы", rec.sent and all(222 not in m["to"] for m in rec.sent))
        finally:
            restore()


def test_handlers_pure_bits():
    print("\n[Кнопки: callback_data в лимите Telegram]")
    mid = "1234567890123456789"
    item = {"mid": mid, "h8": storage._h8("ключ"), "text": "t"}
    check("ud|<19-значный id>|<h8>|<ГГММДД> укладывается в 64 байта",
          all(len(b[1].encode()) <= 64 for b in storage.ud_buttons(item, now=L(20, 12), tz=TZ)))


def test_handlers_smoke():
    print("\n[Команды и кнопки: прогон на заглушках Telegram]")
    from unittest.mock import AsyncMock, MagicMock
    import handlers

    def upd(uid=5, args=None, text=None):
        u = MagicMock()
        u.effective_user.id = uid
        u.message.reply_text = AsyncMock()
        c = MagicMock()
        c.args = args or []
        return u, c

    def said(u):
        return " ".join(str(c.args[0]) for c in u.message.reply_text.call_args_list)

    class _FixedDT(datetime):          # «сейчас» = 20.10 12:00 МСК, не зависит от часов машины
        @classmethod
        def utcnow(cls):
            return L(20, 12)
    real_dt = handlers.datetime
    handlers.datetime = _FixedDT
    now = L(20, 12)
    with _Env() as env:
        p1 = models.Prediction(text=M("18:00", "Альфа", "Бета"), match_time=now + timedelta(minutes=20), fonbet_matched=True, fonbet_state="line")
        p2 = models.Prediction(text="Poland. 4 Liga. 20-00 Gamma — Delta п1 2+", match_time=now + timedelta(minutes=300))
        storage.save_predictions(predictions=[p1, p2])

        u, c = upd()
        asyncio.run(handlers.cmd_today(u, c))
        out = said(u)
        check("/today: показывает матчи и статус «в линии»", "в линии" in out and "ещё рано" in out)

        u, c = upd(args=["23", "8"]); asyncio.run(handlers.cmd_quiet(u, c))
        s = storage.load_settings(5)
        check("/quiet 23 8 сохраняет тихие часы", (s.quiet_from, s.quiet_to) == (23, 8) and "23:00" in said(u))
        u, c = upd(args=[]); asyncio.run(handlers.cmd_quiet(u, c))
        check("/quiet без аргументов показывает текущие", "23:00" in said(u))
        u, c = upd(args=["off"]); asyncio.run(handlers.cmd_quiet(u, c))
        check("/quiet off выключает", storage.load_settings(5).quiet_from is None)
        u, c = upd(args=["99"]); asyncio.run(handlers.cmd_quiet(u, c))
        check("/quiet с мусором — подсказка формата", "Формат" in said(u))

        # заглушка матчей и блокировка стран удалены: команд нет, /today без пометок
        u, c = upd(); asyncio.run(handlers.cmd_today(u, c))
        check("/today без пометок 🔕/🚫", "🔕" not in said(u) and "🚫" not in said(u))
        check("Команд /block, /unblock, /unmute больше нет",
              not any(hasattr(handlers, n) for n in ("cmd_block", "cmd_unblock", "cmd_unmute")))

        # кнопки
        def cb(data, uid=5):
            q = MagicMock(); q.data = data; q.from_user.id = uid
            q.answer = AsyncMock(); q.edit_message_text = AsyncMock(); q.message.text = ""
            u = MagicMock(); u.callback_query = q
            return u, q
        u, q = cb(f"mute|{p1.id}")
        before = storage.load_settings(5).to_dict()
        try:
            asyncio.run(handlers.on_callback(u, MagicMock()))
            crashed = False
        except Exception:
            crashed = True
        check("Кнопка «mute|…» из старого сообщения: бот не падает, настройки не меняются",
              not crashed and storage.load_settings(5).to_dict() == before)
        check("…и индикатор загрузки на кнопке гаснет (callback подтверждён)", q.answer.await_count == 1)

        # диагностика
        health.reset()
        u, c = upd(); asyncio.run(handlers.cmd_status(u, c))
        out = said(u)
        check("/status: есть Фонбет, Discord, расписание, журнал", all(k in out for k in ("Фонбет", "Discord", "Расписание", "Журнал")))
        storage.journal_add({"kind": "miss", "text": "тест", "match_time": now.isoformat(), "reason": "time",
                             "why": "время старта отличается на 40 мин", "best": [{"score": 90, "match": "A — B", "league": "L", "why": "время"}]})
        u, c = upd(); asyncio.run(handlers.cmd_misses(u, c))
        check("/misses: причина и кандидаты", "40 мин" in said(u) and "90%" in said(u))
        u, c = upd(args=["found"]); asyncio.run(handlers.cmd_misses(u, c))
        check("/misses found: без записей — понятный текст", "Найденных пока нет" in said(u))
        storage.add_aliases([("Dinamo", "Dynamo")])
        u, c = upd(); asyncio.run(handlers.cmd_aliases(u, c))
        check("/aliases", "Dinamo" in said(u))
        u, c = upd(args=["dinamo"]); asyncio.run(handlers.cmd_unalias(u, c))
        check("/unalias", "удалён" in said(u) and storage.list_aliases() == [])

        # неопределённая дата: команда и кнопка
        snp = snap(80, L(20, 11), M("14:00", "Х", "У"), edited=L(20, 23))
        env.apply([snp], now=L(20, 23, 5))
        u, c = upd(); asyncio.run(handlers.cmd_undetermined(u, c))
        check("/undetermined: вопрос с кнопками", u.message.reply_text.await_count == 1 and "Когда он играет" in said(u))
        item = storage.list_undetermined()[0]
        u, q = cb(storage.ud_buttons(item, now=now, tz=TZ)[2][1])
        asyncio.run(handlers.on_callback(u, MagicMock()))
        check("Кнопка «Пропустить» снимает вопрос и редактирует сообщение",
              storage.list_undetermined() == [] and q.edit_message_text.await_count == 1)
    handlers.datetime = real_dt


def test_commands_registry():
    print("\n[/commands: реестр команд совпадает с bot.py]")
    import re as _re
    import handlers
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"), encoding="utf-8").read()
    registered = set()
    for m in _re.finditer(r'CommandHandler\(\s*(\[[^\]]*\]|"[^"]+")', src):
        registered |= set(_re.findall(r'"([a-z_]+)"', m.group(1)))
    names = handlers.all_command_names()
    # «comands» — синоним-опечатка, в справке отдельной строкой не нужен
    check("Все команды из bot.py есть в реестре /commands: " + (", ".join(sorted(registered - names - handlers.COMMAND_ALIASES)) or "ок"),
          (registered - handlers.COMMAND_ALIASES) <= names)
    check("В реестре нет команд, которых бот не регистрирует: " + (", ".join(sorted(names - registered)) or "ок"),
          names <= registered)
    check("/commands, /comands и /help зарегистрированы вместе", {"commands", "comands", "help"} <= registered)
    missing = [n for n in names if not hasattr(handlers, f"cmd_{n}")]
    check("У каждой команды из реестра есть обработчик cmd_*: " + (", ".join(missing) or "ок"), not missing)
    allc = list(handlers.COMMANDS_USER) + [c for _t, items in handlers.COMMANDS_ADMIN for c in items]
    check("У каждой команды есть описание", all(c[2].strip() for c in allc))
    check("Имена команд в реестре не повторяются", len([c[0] for c in allc]) == len(set(c[0] for c in allc)))

    user_txt = handlers.build_commands_text(False)
    admin_txt = handlers.build_commands_text(True)
    check("Обычному пользователю: его команды есть, админских нет",
          "/today" in user_txt and "/quiet" in user_txt and "/status" not in user_txt and "/ban" not in user_txt)
    check("Админу: показаны и пользовательские, и все админские",
          all(f"/{n}" in admin_txt for n in names))
    check("Текст влезает в одно сообщение Telegram (<4096)", len(admin_txt) < 4096)

    from unittest.mock import AsyncMock, MagicMock
    u = MagicMock(); u.effective_user.id = 5; u.message.reply_text = AsyncMock()
    asyncio.run(handlers.cmd_commands(u, MagicMock()))
    check("/commands отвечает одним сообщением", u.message.reply_text.await_count == 1
          and "Все команды" in u.message.reply_text.call_args.args[0])


import handlers
import version


def test_version_and_startup():
    print("\n[Сборка: самопроверка комплекта файлов]")
    check("Текущий комплект целый — проблем нет", version.check_consistency() == [])

    saved = scheduler.health_tick
    del scheduler.health_tick
    try:
        check("Устаревший scheduler.py (нет health_tick) → назван в проблемах", version.check_consistency() == ["scheduler.py"])
    finally:
        scheduler.health_tick = saved
    saved_h = handlers.cmd_commands
    del handlers.cmd_commands
    try:
        check("Старый handlers.py (нет cmd_commands) → назван в проблемах", "handlers.py" in version.check_consistency())
    finally:
        handlers.cmd_commands = saved_h
    check("После восстановления снова всё хорошо", version.check_consistency() == [])

    ok = version.startup_text(40, [])
    bad = version.startup_text(40, ["scheduler.py", "fonbet.py"])
    check("Стартовое сообщение: сборка и число обработчиков", version.BUILD in ok and "40" in ok and "/commands" in ok)
    check("При проблемах в сообщении перечислены файлы и что делать",
          "scheduler.py, fonbet.py" in bad and "целиком" in bad and "⚠️" in bad)


def test_health_telegram_conflict():
    print("\n[Здоровье: Conflict Telegram]")
    health.reset()
    t0 = _time.time() + 10_000
    health.telegram_conflict(t0)
    health.telegram_conflict(t0 + 5)
    check("Один-два Conflict (как при редеплое) — не авария", health.evaluate(t0 + 10) == [])
    health.telegram_conflict(t0 + 20)
    ev = health.evaluate(t0 + 21)
    check("Три за 2 минуты → одна тревога про второй экземпляр",
          len(ev) == 1 and ev[0][0] == "telegram_conflict" and ev[0][1] == "raise" and "BOT_TOKEN" in ev[0][2])
    health.telegram_conflict(t0 + 30)
    check("Тревога не повторяется", health.evaluate(t0 + 31) == [])
    ev = health.evaluate(t0 + 30 + health.TG_CONFLICT_WINDOW_SEC + 5)
    check("Conflict прекратились → «конфликт ушёл»", len(ev) == 1 and ev[0][1] == "clear")
    check("Счётчик за 10 минут для /status", health.telegram_conflicts_recent(600, t0 + 100) == 4)
    health.reset()


class _Err(Exception):
    pass


class Conflict(Exception):
    pass


class TimedOut(Exception):
    pass


def _err_ctx(err):
    from unittest.mock import AsyncMock, MagicMock
    ctx = MagicMock()
    ctx.error = err
    ctx.bot.send_message = AsyncMock()
    return ctx


def test_error_handler():
    print("\n[Обработчик ошибок Telegram]")
    from unittest.mock import MagicMock
    handlers._ERR_SEEN.clear()
    health.reset()

    ctx = _err_ctx(Conflict("terminated by other getUpdates request"))
    asyncio.run(handlers.on_error(None, ctx))
    check("Conflict: админам не пишем, но учитываем для алерта",
          ctx.bot.send_message.await_count == 0 and health.telegram_conflicts_recent() == 1)
    ctx = _err_ctx(TimedOut("Timed out"))
    asyncio.run(handlers.on_error(None, ctx))
    check("Сетевой сбой: только лог, админам не пишем", ctx.bot.send_message.await_count == 0)

    upd = MagicMock(); upd.callback_query = None; upd.message.text = "/list"
    ctx = _err_ctx(_Err("что-то сломалось"))
    asyncio.run(handlers.on_error(upd, ctx))
    sent = [c.args[1] for c in ctx.bot.send_message.await_args_list]
    check("Неожиданное исключение: админам уходит короткое сообщение с типом, текстом и командой",
          len(sent) == len(storage.config.ADMIN_CHAT_IDS) and "_Err" in sent[0] and "/list" in sent[0])
    ctx2 = _err_ctx(_Err("что-то сломалось"))
    asyncio.run(handlers.on_error(upd, ctx2))
    check("Та же ошибка повторно — не спамим (кулдаун)", ctx2.bot.send_message.await_count == 0)
    ctx3 = _err_ctx(ValueError("другая"))
    asyncio.run(handlers.on_error(None, ctx3))
    check("Другая ошибка проходит", ctx3.bot.send_message.await_count == len(storage.config.ADMIN_CHAT_IDS))
    cq = MagicMock(); cq.callback_query.data = "ud|1|abc|s"; cq.message = None
    ctx4 = _err_ctx(KeyError("x"))
    asyncio.run(handlers.on_error(cq, ctx4))
    check("Для кнопки в сообщении указана callback_data", "ud|1|abc|s" in ctx4.bot.send_message.await_args_list[0].args[1])
    health.reset()


def test_unknown_command():
    print("\n[Незнакомая команда: подсказка вместо молчания]")
    from unittest.mock import AsyncMock, MagicMock

    def mk(text, chat_type="private", bot_username="mybot"):
        u = MagicMock()
        u.effective_user.id = 5
        u.effective_chat.type = chat_type
        u.message.text = text
        u.message.reply_text = AsyncMock()
        c = MagicMock(); c.bot.username = bot_username
        return u, c

    def reply(u):
        return u.message.reply_text.call_args.args[0] if u.message.reply_text.await_count else None

    u, c = mk("/comand"); asyncio.run(handlers.cmd_unknown(u, c))
    check("/comand → подсказка про /commands и /comands", "/commands" in reply(u) and "Возможно" in reply(u))
    u, c = mk("/lsit"); asyncio.run(handlers.cmd_unknown(u, c))
    check("Опечатка /lsit → предлагает /list", "/list" in reply(u).split("Возможно")[1])
    u, c = mk("/qwertyuiop"); asyncio.run(handlers.cmd_unknown(u, c))
    check("Совсем незнакомая — без «Возможно», но со ссылкой на список", "Возможно" not in reply(u) and "/commands" in reply(u))
    u, c = mk("/foo@otherbot"); asyncio.run(handlers.cmd_unknown(u, c))
    check("Команда адресована другому боту — молчим", reply(u) is None)
    u, c = mk("/foo", chat_type="supergroup"); asyncio.run(handlers.cmd_unknown(u, c))
    check("В группе чужую слэш-команду без @ — молчим", reply(u) is None)
    u, c = mk("/foo@MyBot", chat_type="supergroup"); asyncio.run(handlers.cmd_unknown(u, c))
    check("В группе, но адресовано нашему боту (регистр не важен) — отвечаем", reply(u) is not None)


def test_command_menu_and_bot_wiring():
    print("\n[Меню «/» и сборка приложения bot.py]")
    import re as _re
    for admin in (False, True):
        menu = handlers.command_menu(admin)
        names = [n for n, _d in menu]
        check(f"Меню ({'админ' if admin else 'пользователь'}): имена допустимы для Telegram (a-z0-9_, ≤32), описания 1–256",
              all(_re.fullmatch(r"[a-z0-9_]{1,32}", n) for n in names) and all(1 <= len(d) <= 256 for _n, d in menu))
    user_names = {n for n, _d in handlers.command_menu(False)}
    admin_names = {n for n, _d in handlers.command_menu(True)}
    check("Пользовательское меню не содержит админских команд", not ({"ban", "status", "debug", "clear"} & user_names))
    check("Админское меню — все команды из реестра", admin_names == handlers.all_command_names())
    check("В меню нет дублей", len(handlers.command_menu(True)) == len(admin_names))

    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py"), encoding="utf-8").read()
    last_cmd = max(m.start() for m in _re.finditer(r"CommandHandler\(", src))
    unknown = src.index("filters.COMMAND, handlers.cmd_unknown")
    check("Обработчик незнакомых команд зарегистрирован ПОСЛЕ всех CommandHandler (иначе перехватит свои)", unknown > last_cmd)
    check("Зарегистрирован обработчик ошибок", "add_error_handler(handlers.on_error)" in src)
    check("Стартовая самопроверка подключена (post_init)", "post_init(_on_startup)" in src and "check_consistency" in src)
    check("Логи идут в stdout (иначе Railway помечает всё как error)", "stream=sys.stdout" in src)
    check("/help — синоним /commands", '["commands", "comands", "help"]' in src)


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
    test_live_detection()
    test_underdog()
    test_bookmakers_parser()
    test_settings_migration()
    test_live_message()
    test_voice()
    test_match_dates_rules()
    test_date_headers_parser()
    test_scenario_a_yesterday_message()
    test_scenario_b_evening_for_tomorrow()
    test_scenario_c_edit_adds_matches()
    test_repeat_edit_no_changes()
    test_time_change_of_known_match()
    test_match_identity_stable()
    test_multiple_dates_in_one_message()
    test_undetermined_date()
    test_restart_reprocessing()
    test_legacy_and_removals()
    test_concurrent_events()
    test_midnight_flow()
    test_fonbet_matching_tiers()
    test_league_detection()
    test_monitor_window_retention()
    test_models_new_fields()
    test_health_alerts()
    test_userprefs()
    test_fonbet_aliases_diag()
    test_storage_journal_aliases_settings()
    test_undetermined_buttons_flow()
    test_scheduler_fonbet_tick_integration()
    test_scheduler_miss_journal()
    test_handlers_pure_bits()
    test_handlers_smoke()
    test_commands_registry()
    test_version_and_startup()
    test_health_telegram_conflict()
    test_error_handler()
    test_unknown_command()
    test_command_menu_and_bot_wiring()

    print("\n" + "=" * 50)
    print(f"Пройдено: {_passed}  |  Провалено: {_failed}")
    print("=" * 50)
    sys.exit(1 if _failed else 0)
