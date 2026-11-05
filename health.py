"""
Состояние «здоровья» бота: для команды /status и алертов админам.

Источники данных (всё в памяти процесса, тред-безопасно):
  • Фонбет — каждый запрос событий из планировщика (успех/неудача, число событий);
  • Discord — подключение (on_ready/on_disconnect/on_resume) и успешные перечиты.
Алерты — конечный автомат на каждое условие: сообщение уходит админам ОДИН раз при
срабатывании и ещё одно — когда всё восстановилось (без повторов каждую минуту).

evaluate() ничего не отправляет — возвращает список событий; отправляет планировщик
(scheduler.health_tick). Время передаётся параметром, поэтому логика тестируется.
"""
import threading
import time
from typing import Optional

# Сколько подряд неудачных запросов к Фонбету считаем аварией (тик 15–60 сек → ~1–5 мин)
FONBET_FAIL_STREAK = 5
# Discord-клиент без соединения дольше этого — авария (короткие реконнекты — норма)
DISCORD_DOWN_SEC = 180
# Перечит канала идёт раз в 15 минут; тишина дольше этого — авария
DISCORD_SYNC_STALE_SEC = 30 * 60
# После старта процесса не тревожим: Discord/Фонбет ещё подключаются
STARTUP_GRACE_SEC = 180
# Conflict getUpdates: один-два при редеплое — норма (старый контейнер доживает секунды);
# три и больше за окно — тот же BOT_TOKEN читает ещё кто-то
TG_CONFLICT_COUNT = 3
TG_CONFLICT_WINDOW_SEC = 120

_lock = threading.RLock()
_started_at = time.time()

_fonbet = {"last_ok": None, "last_fail": None, "streak": 0, "events": None,
           "error": None, "fail_since": None}
_discord = {"enabled": False, "connected": None, "since": None, "last_ready": None,
            "last_disconnect": None, "last_sync_ok": None, "last_sync_error": None,
            "last_sync_stats": None}
_tg: dict = {"conflicts": []}   # времена ошибок Telegram Conflict (getUpdates)
_alerts: dict = {}   # имя → {"since": ts}  (есть = активен)


def reset():
    """Для тестов."""
    global _started_at
    with _lock:
        _started_at = time.time()
        for d in (_fonbet, _discord):
            for k in d:
                d[k] = 0 if k == "streak" else (False if k == "enabled" else None)
        _alerts.clear()
        _tg["conflicts"].clear()


# ── Telegram ─────────────────────────────────────────────────────────────────

def telegram_conflict(now: Optional[float] = None):
    """Telegram вернул Conflict на getUpdates: тот же токен читает другой экземпляр бота."""
    now = now or time.time()
    with _lock:
        _tg["conflicts"] = [t for t in _tg["conflicts"] if now - t < 600] + [now]


def telegram_conflicts_recent(window: float = 600, now: Optional[float] = None) -> int:
    now = now or time.time()
    with _lock:
        return sum(1 for t in _tg["conflicts"] if now - t <= window)


# ── Фонбет ───────────────────────────────────────────────────────────────────

def fonbet_result(ok: bool, events: int = 0, error: Optional[str] = None, now: Optional[float] = None):
    now = now or time.time()
    with _lock:
        _fonbet["events"] = events
        if ok:
            _fonbet.update(last_ok=now, streak=0, error=None, fail_since=None)
        else:
            _fonbet["last_fail"] = now
            _fonbet["streak"] += 1
            _fonbet["error"] = error or "нет событий"
            if _fonbet["fail_since"] is None:
                _fonbet["fail_since"] = now


# ── Discord ──────────────────────────────────────────────────────────────────

def discord_enabled(flag: bool = True):
    with _lock:
        _discord["enabled"] = flag


def discord_connected(flag: bool, now: Optional[float] = None):
    now = now or time.time()
    with _lock:
        if _discord["connected"] != flag:
            _discord["since"] = now
        _discord["connected"] = flag
        if flag:
            _discord["last_ready"] = now
        else:
            _discord["last_disconnect"] = now


def discord_sync(ok: bool, stats: Optional[dict] = None, error: Optional[str] = None, now: Optional[float] = None):
    now = now or time.time()
    with _lock:
        if ok:
            _discord["last_sync_ok"] = now
            _discord["last_sync_error"] = None
            if stats:
                _discord["last_sync_stats"] = {k: v for k, v in stats.items() if isinstance(v, (int, float))}
        else:
            _discord["last_sync_error"] = error


# ── Снимок и алерты ──────────────────────────────────────────────────────────

def snapshot() -> dict:
    with _lock:
        return {"started_at": _started_at, "fonbet": dict(_fonbet), "discord": dict(_discord),
                "alerts": {k: dict(v) for k, v in _alerts.items()}}


def _fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    if sec < 90:
        return f"{sec} сек"
    if sec < 5400:
        return f"{sec // 60} мин"
    return f"{sec // 3600} ч {sec % 3600 // 60} мин"


def ago(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return "—"
    return _fmt_dur((now or time.time()) - ts) + " назад"


def _conditions(now: float) -> dict:
    """имя → (активно, текст аварии, текст восстановления)."""
    f, d = _fonbet, _discord
    out = {}

    conflicts = sum(1 for t in _tg["conflicts"] if now - t <= TG_CONFLICT_WINDOW_SEC)
    out["telegram_conflict"] = (
        conflicts >= TG_CONFLICT_COUNT,
        f"🚨 Telegram: Conflict ×{conflicts} за {TG_CONFLICT_WINDOW_SEC // 60} мин — этот же BOT_TOKEN одновременно "
        f"читает другой экземпляр бота (старый деплой не остановился или запущен второй проект). "
        f"Команды и кнопки будут отвечать вразнобой, уведомления могут дублироваться. "
        f"Проверь Railway → Deployments.",
        "✅ Конфликт Telegram ушёл: токен снова читает один экземпляр.",
    )

    out["fonbet_down"] = (
        f["streak"] >= FONBET_FAIL_STREAK,
        f"🚨 Фонбет не отвечает: {f['streak']} неудачных запросов подряд"
        f"{' (' + str(f['error']) + ')' if f['error'] else ''}. "
        f"Уведомления о линии/лайве/кривых не приходят, пока это не починится.",
        "✅ Фонбет снова отвечает.",
    )

    if d["enabled"]:
        down_for = (now - d["since"]) if (d["connected"] is False and d["since"]) else 0
        out["discord_down"] = (
            d["connected"] is False and down_for >= DISCORD_DOWN_SEC,
            f"🚨 Discord-клиент без соединения уже {_fmt_dur(down_for)}. Новые прогнозы не читаются.",
            "✅ Discord снова подключён.",
        )
        base = d["last_sync_ok"] or d["last_ready"] or _started_at
        stale = d["connected"] is not False and (now - base) >= DISCORD_SYNC_STALE_SEC
        out["discord_stale"] = (
            stale,
            f"🚨 Перечит Discord-канала не проходил {_fmt_dur(now - base)} (обычно раз в 15 мин)"
            f"{'; последняя ошибка: ' + str(d['last_sync_error']) if d['last_sync_error'] else ''}.",
            "✅ Перечит Discord-канала снова идёт.",
        )
    return out


def evaluate(now: Optional[float] = None) -> list[tuple[str, str, str]]:
    """
    Проверяет условия и возвращает события [(имя, 'raise'|'clear', текст)] — только
    ПЕРЕХОДЫ состояния (тревога повторно не шлётся, пока условие не пропало).
    """
    now = now or time.time()
    events = []
    with _lock:
        if now - _started_at < STARTUP_GRACE_SEC:
            return events
        for name, (active, raise_txt, clear_txt) in _conditions(now).items():
            if active and name not in _alerts:
                _alerts[name] = {"since": now}
                events.append((name, "raise", raise_txt))
            elif not active and name in _alerts:
                since = _alerts.pop(name)["since"]
                events.append((name, "clear", f"{clear_txt} (был недоступен {_fmt_dur(now - since)})"))
    return events
