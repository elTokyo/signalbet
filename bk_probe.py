"""
Диагностика эндпоинтов BetBoom / Winline / Лига Ставок.

Запуск (локально или на Railway — см. README):
    python bk_probe.py
    python bk_probe.py https://какой-то.url/из/devtools   # проверить свой URL

Что делает:
  1. дёргает все URL-кандидаты каждой конторы;
  2. печатает код ответа, тип контента и верхние ключи JSON;
  3. пробует универсальным парсером вытащить события и печатает первые три
     с командами и парой коэффициентов.

Вывод целиком пришли мне — по нему я допилю парсеры под реальную структуру.
Скрипт ничего не пишет и не меняет, его можно гонять сколько угодно.
"""
import sys
import json
import logging

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

import bookmakers


def probe_url(url: str):
    print(f"\n── {url}")
    try:
        r = requests.get(url, headers=bookmakers.HEADERS, timeout=15)
    except Exception as e:
        print(f"   ✖ запрос не прошёл: {e}")
        return

    ctype = r.headers.get("Content-Type", "?")
    print(f"   HTTP {r.status_code}, Content-Type: {ctype}, размер: {len(r.content)} байт")
    if r.status_code != 200:
        print(f"   тело (первые 300 симв.): {r.text[:300]}")
        return

    try:
        data = r.json()
    except Exception:
        print(f"   ✖ это не JSON. Первые 300 символов: {r.text[:300]}")
        return

    if isinstance(data, dict):
        print(f"   ключи верхнего уровня: {list(data.keys())[:25]}")
    elif isinstance(data, list):
        print(f"   это список, элементов: {len(data)}")
        if data and isinstance(data[0], dict):
            print(f"   ключи первого элемента: {list(data[0].keys())[:25]}")

    events = []
    bookmakers._walk_events(data, events)
    print(f"   распознано событий: {len(events)}")
    for ev in events[:3]:
        odds_preview = []
        for o in ev["odds"][:6]:
            label = o["label"] or "?"
            param = "" if o["param"] is None else f"({o['param']})"
            odds_preview.append(f"{label}{param}={o['odd']}")
        print(f"   • {ev['team1']} — {ev['team2']} | live={ev['is_live']} | "
              f"старт={ev['kickoff_utc']} | кэфы: {odds_preview}")

    if not events:
        sample = json.dumps(data, ensure_ascii=False)[:1200]
        print(f"   сырой кусок ответа для разбора вручную:\n   {sample}")


def main():
    if len(sys.argv) > 1:
        for url in sys.argv[1:]:
            probe_url(url)
        return

    for bk in bookmakers.BOOKMAKERS:
        print(f"\n════════ {bk['name']} ════════")
        urls = [bk["url"]] + [u for u in bk.get("candidates", []) if u != bk["url"]]
        for url in urls:
            probe_url(url)


if __name__ == "__main__":
    main()
