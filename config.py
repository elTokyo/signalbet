import os

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
DISCORD_CHANNEL_ID        = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DISCORD_TARGET_TG_CHAT_ID = int(os.getenv("DISCORD_TARGET_TG_CHAT_ID", "0"))

# ── Параметры напоминаний ────────────────────────────────────────────────────
NOTIFY_BEFORE_MINUTES = [30, 5]
DELETE_AFTER_MINUTES  = 5

# ── Часовой пояс по умолчанию ────────────────────────────────────────────────
DEFAULT_TZ_OFFSET = int(os.getenv("DEFAULT_TZ_OFFSET", "3"))
