import os


def _env_int(name: str, default: int = 0) -> int:
    """
    int из переменной окружения. Терпит кавычки и пробелы вокруг значения:
    из .env-привычки легко вставить в Railway NAME="123" вместе с кавычками, и
    раньше это роняло бота на старте с ValueError. Мусор -> default + строка в лог.
    """
    raw = os.getenv(name, "").strip().strip("\"'").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] {name}={raw!r} - не число, использую {default}")
        return default


# ── Telegram ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ── Авторизация ──────────────────────────────────────────────────────────────
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "")

# Список админов через запятую: "123456789,987654321"
_admin_ids_raw = os.getenv("ADMIN_CHAT_IDS", "") or os.getenv("ADMIN_CHAT_ID", "")
ADMIN_CHAT_IDS = [
    int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().lstrip("-").isdigit()
]
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0] if ADMIN_CHAT_IDS else 0

# ── GitHub Gist (хранилище данных) ───────────────────────────────────────────
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GIST_ID      = os.getenv("GIST_ID", "")

# ── Discord (опционально) ────────────────────────────────────────────────────
DISCORD_TOKEN             = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID        = _env_int("DISCORD_CHANNEL_ID", 0)
DISCORD_TARGET_TG_CHAT_ID = _env_int("DISCORD_TARGET_TG_CHAT_ID", 0)

# ── Discord: слежение за голосовым каналом ───────────────────────────────────
# За кем следим и в каком войсе. 0 = функция выключена.
DISCORD_VOICE_USER_ID    = _env_int("DISCORD_VOICE_USER_ID", 0)
DISCORD_VOICE_CHANNEL_ID = _env_int("DISCORD_VOICE_CHANNEL_ID", 0)
# Кулдаун между уведомлениями одного типа (сек). Защита от спама при реконнектах.
VOICE_COOLDOWN_SEC = _env_int("VOICE_COOLDOWN_SEC", 600)
# Пауза перед уведомлением о выходе: если человек вернулся за это время —
# считаем это реконнектом и не шлём ни «вышел», ни «зашёл».
VOICE_LEAVE_GRACE_SEC = _env_int("VOICE_LEAVE_GRACE_SEC", 60)

# ── Параметры напоминаний ────────────────────────────────────────────────────
NOTIFY_BEFORE_MINUTES = [30, 5]
# Через сколько минут после старта прогноз удаляется из общего листа.
# Влияет и на окно лайв-мониторинга: после удаления следить уже не за чем.
DELETE_AFTER_MINUTES  = _env_int("DELETE_AFTER_MINUTES", 5)

# ── Другие БК (проверка кривых матчей) ───────────────────────────────────────
# URL можно переопределить переменными окружения, не трогая код —
# пригодится когда контора сменит эндпоинт (см. bk_probe.py).
BETBOOM_API_URL    = os.getenv("BETBOOM_API_URL", "")
WINLINE_API_URL    = os.getenv("WINLINE_API_URL", "")
LIGASTAVOK_API_URL = os.getenv("LIGASTAVOK_API_URL", "")

# ── Часовой пояс по умолчанию ────────────────────────────────────────────────
DEFAULT_TZ_OFFSET = _env_int("DEFAULT_TZ_OFFSET", 3)
