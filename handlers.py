import asyncio
import difflib
import logging
import time
from datetime import datetime, timedelta
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import storage
import auth
import config
import health
import userprefs
import version
from parser import parse_predictions, format_time_local, display_text_without_time

logger = logging.getLogger(__name__)

# Состояния ожидания: ключ = (chat_id, user_id)
PENDING_INPUT: dict[tuple[int, int], str] = {}


# ── Декораторы ───────────────────────────────────────────────────────────────

def require_auth(func):
    """Любая авторизация: личка или участник группы. Проверяется user_id."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not auth.is_authorized(user_id):
            await update.message.reply_text(
                "🔒 Доступ закрыт. Напиши боту /start в личку и введи пароль."
            )
            return
        return await func(update, ctx)
    return wrapper


def require_admin(func):
    """Только админ (по user_id)."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not auth.is_admin(user_id):
            await update.message.reply_text("⛔ Только админ может выполнять это действие.")
            return
        return await func(update, ctx)
    return wrapper


# ── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id

    # В группе /start работает только как информация
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text(
            "👋 Бот добавлен в группу.\n\n"
            "Прогнозы добавляют админы. Все участники видят уведомления автоматически.\n\n"
            "Чтобы получить личный доступ — напиши мне /start *в личку*.",
            parse_mode="Markdown",
        )
        return

    if auth.is_authorized(user_id):
        await _send_help(update, user_id)
        return

    if not config.ACCESS_PASSWORD:
        # Режим разработки — авторизуем без пароля
        auth.authorize(user_id, user.username or "", user.first_name or "")
        await _send_help(update, user_id)
        return

    PENDING_INPUT[(chat_id, user_id)] = "password"
    await update.message.reply_text(
        "🔒 *Доступ к боту по паролю*\n\nВведи пароль одним сообщением:",
        parse_mode="Markdown",
    )


async def _send_help(update: Update, user_id: int):
    is_admin = auth.is_admin(user_id)

    if is_admin:
        text = (
            "⚽ *Бот-помощник для прогнозов* (админ)\n\n"
            "📋 *Команды:*\n"
            "/add — добавить прогнозы\n"
            "/list — список активных\n"
            "/delete <id> — удалить один\n"
            "/clear — очистить все\n"
            "/settings — настройки\n"
            "/today — матчи на сегодня со статусом\n"
            "/quiet, /block, /unblock, /unmute — личные фильтры\n"
            "/broadcast — рассылка всем\n\n"
            "🩺 *Диагностика:*\n"
            "/status — здоровье бота (Фонбет, Discord)\n"
            "/misses — почему матч не нашёлся на Фонбете\n"
            "/undetermined — матчи с неопределённой датой\n"
            "/aliases, /unalias — запомненные названия команд\n\n"
            "👥 *Управление пользователями:*\n"
            "/users, /ban, /unban, /remove\n\n"
            "🔔 Уведомления за 30 и 5 минут до матча.\n"
            "🗑 Матч уходит из списка после окна мониторинга (~16 мин после старта).\n\n"
            "Полный список команд: /commands"
        )
    else:
        text = (
            "⚽ *Бот-помощник для прогнозов*\n\n"
            "📋 *Доступные команды:*\n"
            "/list — посмотреть активные прогнозы\n"
            "/today — матчи на сегодня и что с ними на Фонбете\n"
            "/settings — твои настройки (часовой пояс, уведомления)\n"
            "/quiet 23 8 — тихие часы (не беспокоить с 23 до 8), `/quiet off` — выключить\n"
            "/block Poland — не получать матчи страны (`/unblock`, список — `/block`)\n"
            "/unmute — вернуть заглушённые матчи\n"
            "/feedback — сообщить о баге или оставить отзыв\n\n"
            "🔔 Уведомления о матчах ты получаешь автоматически.\n"
            "Полный список команд: /commands\n\n"
            "_Добавление и редактирование прогнозов доступно только админам._"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /commands: единый реестр всех команд ─────────────────────────────────────
# Единственный источник правды для списка команд. Тест в test_bot.py сверяет его с
# CommandHandler-ами в bot.py: добавил команду в bot.py, но забыл сюда — тест упадёт.
# (имя без «/», аргументы, описание)

COMMANDS_USER = [
    ("start",    "",                    "начало работы / ввод пароля"),
    ("commands", "",                    "этот список (работает и /comands, /help)"),
    ("list",     "",                    "активные прогнозы"),
    ("today",    "",                    "матчи на сегодня и статус каждого на Фонбете"),
    ("settings", "",                    "часовой пояс и категории уведомлений"),
    ("quiet",    "23 8 | off",          "тихие часы: не беспокоить в этот интервал"),
    ("block",    "<страна>",            "не получать матчи страны (без аргумента — список)"),
    ("unblock",  "<страна>",            "снова получать матчи страны"),
    ("unmute",   "",                    "вернуть заглушённые матчи"),
    ("feedback", "",                    "отправить отзыв или баг-репорт админу"),
    ("cancel",   "",                    "отменить текущий ввод"),
]

COMMANDS_ADMIN = [
    ("📝 Прогнозы", [
        ("add",       "",                 "добавить прогнозы"),
        ("delete",    "<id>",             "удалить один прогноз"),
        ("clear",     "",                 "очистить все прогнозы"),
        ("broadcast", "",                 "рассылка всем пользователям"),
    ]),
    ("👥 Пользователи", [
        ("users",  "",             "список авторизованных"),
        ("ban",    "<id>",         "заблокировать пользователя"),
        ("unban",  "<id>",         "разблокировать"),
        ("remove", "<id>",         "удалить из whitelist"),
    ]),
    ("🩺 Диагностика", [
        ("status",       "",                "здоровье бота: Фонбет, Discord, расписание, тревоги"),
        ("misses",       "[N] | found",     "почему матчи не нашлись на Фонбете / что нашлось"),
        ("undetermined", "",                "матчи с неопределённой датой (кнопки выбора)"),
        ("checkfonbet",  "",                "сопоставление прогнозов с Фонбетом: линия/лайв, % совпадения"),
        ("factors",      "[N]",             "дамп всех рынков матча (для кодов форовых рынков)"),
        ("bk",           "[N]",             "проверить прогноз №N на BetBoom / Winline / Лиге Ставок"),
        ("aliases",      "",                "запомненные пары названий команд"),
        ("unalias",      "<название>",      "удалить ошибочную пару названий"),
        ("syncdiscord",  "",                "принудительный перечит Discord-канала"),
        ("voice",        "",                "диагностика слежения за голосовым каналом Discord"),
        ("debug",        "",                "диагностика конфигурации"),
    ]),
]


def all_command_names() -> set:
    """Имена всех команд из реестра (для теста сверки с bot.py)."""
    names = {c[0] for c in COMMANDS_USER}
    for _title, items in COMMANDS_ADMIN:
        names |= {c[0] for c in items}
    return names


def _fmt_command(name: str, args: str, desc: str) -> str:
    return f"/{name}{' ' + args if args else ''} — {desc}"


def build_commands_text(is_admin: bool) -> str:
    """Текст для /commands. Простой текст без Markdown: «<id>» и «|» в нём ломали бы разметку."""
    lines = ["📋 Все команды бота\n", "👤 Для всех:"]
    lines += [_fmt_command(*c) for c in COMMANDS_USER]
    if is_admin:
        for title, items in COMMANDS_ADMIN:
            lines += ["", f"{title}:"]
            lines += [_fmt_command(*c) for c in items]
    else:
        lines += ["", "🔒 Админские команды (управление прогнозами и пользователями, диагностика) "
                      "скрыты — они доступны только админам."]
    return "\n".join(lines)


@require_auth
async def cmd_commands(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выдаёт список всех команд (админам — с админскими). Также доступна как /comands."""
    await update.message.reply_text(build_commands_text(auth.is_admin(update.effective_user.id)))


# Синонимы /commands, которые бот тоже понимает (в справке отдельной строкой не нужны)
COMMAND_ALIASES = {"comands", "help"}


def command_menu(is_admin: bool) -> list[tuple[str, str]]:
    """[(имя, описание)] для меню «/» в Telegram. Обычным — свои команды, админам — все."""
    items = [(c[0], c[2]) for c in COMMANDS_USER]
    if is_admin:
        for _title, group in COMMANDS_ADMIN:
            items += [(c[0], c[2]) for c in group]
    return [(n, d[:256]) for n, d in items]


async def publish_command_menu(bot):
    """
    Публикует список команд в меню «/» Telegram (подсказки при вводе слэша):
    всем — пользовательские, каждому админу — полный набор. Ошибки не критичны.
    """
    from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
    try:
        await bot.set_my_commands([BotCommand(n, d) for n, d in command_menu(False)],
                                  scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning(f"set_my_commands (default) не удалось: {e}")
    for admin_id in config.ADMIN_CHAT_IDS:
        try:
            await bot.set_my_commands([BotCommand(n, d) for n, d in command_menu(True)],
                                      scope=BotCommandScopeChat(admin_id))
        except Exception as e:   # админ мог ещё не писать боту — для него просто не будет расширенного меню
            logger.warning(f"set_my_commands для админа {admin_id} не удалось: {e}")


@require_auth
async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Незнакомая команда: вместо молчания — подсказка и ссылка на /commands.
    В группах отвечаем только если команда адресована этому боту (/cmd@бот),
    чужие слэш-команды (других ботов) игнорируем.
    """
    msg = update.message
    parts = (msg.text or "").split()
    if not parts:
        return
    name, _, target = parts[0].lstrip("/").partition("@")
    if target and target.lower() != (getattr(ctx.bot, "username", "") or "").lower():
        return
    if not target and update.effective_chat.type != "private":
        return
    name = name.lower()
    close = difflib.get_close_matches(name, sorted(all_command_names() | COMMAND_ALIASES), n=2, cutoff=0.6)
    hint = f"\nВозможно, ты имел в виду: {', '.join('/' + c for c in close)}" if close else ""
    await msg.reply_text(f"🤷 Не знаю команды /{name}.{hint}\nСписок всех команд: /commands")


# Одинаковые ошибки не чаще раза в 10 минут шлём админам (иначе повторяющийся сбой завалит чат)
_ERR_SEEN: dict = {}
ERR_ALERT_COOLDOWN_SEC = 600


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Единый обработчик ошибок Telegram-части. Раньше его не было, и PTB печатал сырые трейсбеки
    («No error handlers are registered»). Теперь:
      • Conflict (тот же токен читает другой экземпляр) — одна строка в лог + учёт для алерта;
      • сетевые сбои — предупреждение в одну строку;
      • исключение в команде/кнопке — трейсбек в лог и короткое сообщение админам.
    Тип проверяем по имени класса: так обработчик не зависит от импорта telegram.error.
    """
    err = ctx.error
    name = type(err).__name__

    if name == "Conflict":
        health.telegram_conflict()
        logger.warning("Telegram Conflict: тот же BOT_TOKEN читает ещё один экземпляр бота. "
                       "При редеплое проходит за секунды; если повторяется — проверь Railway → Deployments")
        return
    if name in ("NetworkError", "TimedOut", "RetryAfter"):
        logger.warning(f"Сеть Telegram ({name}): {err}")
        return

    logger.error("Необработанная ошибка в обработчике Telegram", exc_info=err)

    key = (name, str(err)[:80])
    now = time.time()
    if now - _ERR_SEEN.get(key, 0) < ERR_ALERT_COOLDOWN_SEC:
        return
    _ERR_SEEN[key] = now

    what = ""
    try:
        cq = getattr(update, "callback_query", None)
        msg = getattr(update, "message", None)
        if cq is not None and getattr(cq, "data", None):
            what = f"\nКнопка: {cq.data[:60]}"
        elif msg is not None and getattr(msg, "text", None):
            what = f"\nСообщение: {msg.text[:60]}"
    except Exception:
        pass
    text = f"⚠️ Ошибка в боте: {name}: {str(err)[:200]}{what}\nПодробности — в логах Railway."
    for admin_id in config.ADMIN_CHAT_IDS:
        try:
            await ctx.bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Не удалось отправить алерт об ошибке админу {admin_id}: {e}")


# ── Команды чтения (доступны всем авторизованным) ────────────────────────────

@require_auth
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    preds = storage.load_predictions()
    s = storage.load_settings(user_id)

    if not preds:
        await update.message.reply_text("📋 Список пуст.")
        return

    is_adm = auth.is_admin(user_id)

    lines = [f"📋 Активных прогнозов: {len(preds)}\n"]
    for i, p in enumerate(preds, 1):
        t = format_time_local(p, s.timezone_offset)
        display_text = display_text_without_time(p, s.timezone_offset)
        status = " 🔔" if p.notified_30 else ""
        status = " ✅" if p.notified_5 else status
        src = " 🤖" if p.source == "discord" else ""
        # Админам показываем ID для удаления (без Markdown — скобки в названиях ломают разметку)
        id_line = f"\n   🆔 {p.id}" if is_adm else ""
        lines.append(f"{i}. ⏰ {t}{status}{src}\n   {display_text}{id_line}")

    if is_adm:
        lines.append("\nДля удаления: /delete <id>")

    # Telegram ограничивает сообщение ~4096 символами.
    # Разбиваем на куски по строкам, не превышая лимит.
    await _send_chunked(update, lines, limit=3900)


async def _send_chunked(update: Update, lines: list[str], limit: int = 3900):
    """Отправляет список строк, разбивая на сообщения не длиннее limit символов."""
    buffer = ""
    for line in lines:
        # +1 на перенос строки
        if len(buffer) + len(line) + 1 > limit:
            if buffer:
                await update.message.reply_text(buffer)
            buffer = line
        else:
            buffer = f"{buffer}\n{line}" if buffer else line
    if buffer:
        await update.message.reply_text(buffer)


@require_auth
async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обратная связь: пользователь отправляет сообщение, оно уходит админам."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Если текст указан сразу после команды
    if ctx.args:
        text = " ".join(ctx.args)
        await _send_feedback(update, ctx, text)
        return

    PENDING_INPUT[(chat_id, user_id)] = "feedback"
    await update.message.reply_text(
        "✍️ Напиши свой отзыв или описание бага следующим сообщением.\n"
        "Оно будет отправлено администратору.\n\n"
        "Для отмены — /cancel"
    )


async def _send_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    text = text.strip()
    if not text:
        await update.message.reply_text("❌ Пустое сообщение.")
        return

    user = update.effective_user
    uname = f"@{user.username}" if user.username else "—"
    report = (
        f"📨 Обратная связь\n\n"
        f"От: {user.first_name or '—'} ({uname})\n"
        f"🆔 {user.id}\n\n"
        f"Сообщение:\n{text}"
    )

    sent = 0
    for admin_id in config.ADMIN_CHAT_IDS:
        try:
            await ctx.bot.send_message(chat_id=admin_id, text=report)
            sent += 1
        except Exception as e:
            logger.error(f"feedback to admin {admin_id} failed: {e}")

    if sent > 0:
        await update.message.reply_text("✅ Спасибо! Сообщение отправлено администратору.")
    else:
        await update.message.reply_text("❌ Не удалось отправить. Попробуй позже.")


# ── Команды записи (только админы) ───────────────────────────────────────────

@require_admin
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    PENDING_INPUT[(chat_id, user_id)] = "predictions"
    await update.message.reply_text(
        "📥 Вставь прогнозы — каждый отделяй пустой строкой.\n\n"
        "Пример:\n`Soccer. Brazil. 2-00`\n`Santa Cruz — Independencia ф1-4,5`",
        parse_mode="Markdown",
    )


@require_admin
async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Укажи ID: `/delete abc12345`", parse_mode="Markdown")
        return

    ok = storage.delete_prediction(chat_id, ctx.args[0])
    msg = f"🗑 Удалён `{ctx.args[0]}`." if ok else f"❌ Не найден `{ctx.args[0]}`."
    await update.message.reply_text(msg, parse_mode="Markdown")


@require_admin
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[
        InlineKeyboardButton("✅ Да", callback_data="clear_yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="clear_no"),
    ]]
    await update.message.reply_text("⚠️ Очистить все прогнозы?", reply_markup=InlineKeyboardMarkup(kb))


def _settings_keyboard(s):
    """Строит клавиатуру настроек из текущих значений."""
    def mark(v):
        return "✅" if v else "☐"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 Часовой пояс: UTC+{s.timezone_offset}", callback_data="set_tz")],
        [InlineKeyboardButton(f"{mark(s.notify_reminders)} 🔔 Напоминания (30/5 мин)", callback_data="tg_reminders")],
        [InlineKeyboardButton(f"{mark(s.notify_match_out)} 🔴 Выход матчей (линия/лайв)", callback_data="tg_matchout")],
        [InlineKeyboardButton(f"{mark(s.notify_crooked)} 💰 Кривые матчи", callback_data="tg_crooked")],
        [InlineKeyboardButton(f"{mark(s.notify_underdog)} 🐶 Андердоги", callback_data="tg_underdog")],
        [InlineKeyboardButton(f"{mark(s.notify_close_match)} 🎯 Высокое совпадение", callback_data="tg_closematch")],
        [InlineKeyboardButton(f"{mark(s.notify_new_preds)} 📥 Новые прогнозы", callback_data="tg_newpreds")],
        [InlineKeyboardButton(f"{mark(s.notify_voice)} 🎧 Заход в войс Discord", callback_data="tg_voice")],
    ])


_SETTINGS_TEXT = (
    "⚙️ *Твои настройки уведомлений*\n\n"
    "Нажми на пункт чтобы включить/выключить.\n"
    "🌐 Часовой пояс — для отображения времени матчей."
)


@require_auth
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = storage.load_settings(user_id)
    await update.message.reply_text(
        _SETTINGS_TEXT,
        reply_markup=_settings_keyboard(s),
        parse_mode="Markdown",
    )


# ── Админ-команды управления пользователями ──────────────────────────────────

@require_admin
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info(f"/users вызвана пользователем {update.effective_user.id}")
    try:
        users = auth.list_users()
        logger.info(f"/users: список из {len(users)} пользователей загружен")
    except Exception as e:
        logger.exception(f"/users: ошибка чтения списка: {e}")
        await update.message.reply_text(f"❌ Ошибка чтения списка: {e}")
        return

    if not users:
        await update.message.reply_text("📭 Авторизованных пользователей пока нет.")
        return

    lines = [f"👥 *Авторизованных пользователей: {len(users)}*\n"]
    for u in users:
        flag = "🚫" if u.banned else ("👑" if u.user_id in config.ADMIN_CHAT_IDS else "✅")
        username = f"@{u.username}" if u.username else "—"
        date = u.authorized_at[:10] if u.authorized_at else "?"
        lines.append(
            f"{flag} *{u.first_name or 'Без имени'}*\n"
            f"   {username}\n"
            f"   🆔 `{u.user_id}`\n"
            f"   📅 {date}"
        )

    lines.append("\n*Команды:*")
    lines.append("`/ban <user_id>` `/unban <user_id>` `/remove <user_id>`")
    try:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.exception(f"/users: ошибка отправки: {e}")
        # Если упало из-за Markdown — пробуем без него
        try:
            await update.message.reply_text("\n".join(lines))
        except Exception as e2:
            await update.message.reply_text(f"❌ Не удалось отправить: {e2}")


@require_admin
async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: `/ban 123456789`", parse_mode="Markdown")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return

    if target in config.ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Нельзя забанить админа.")
        return

    ok = auth.set_banned(target, True)
    msg = f"🚫 `{target}` забанен." if ok else f"❌ `{target}` не найден."
    await update.message.reply_text(msg, parse_mode="Markdown")


@require_admin
async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: `/unban 123456789`", parse_mode="Markdown")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return

    ok = auth.set_banned(target, False)
    msg = f"✅ `{target}` разбанен." if ok else f"❌ `{target}` не найден."
    await update.message.reply_text(msg, parse_mode="Markdown")


@require_admin
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Использование: `/remove 123456789`", parse_mode="Markdown")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return

    if target in config.ADMIN_CHAT_IDS:
        await update.message.reply_text("⛔ Нельзя удалить админа.")
        return

    ok = auth.remove_user(target)
    msg = f"🗑 `{target}` удалён из вайтлиста." if ok else f"❌ `{target}` не найден."
    await update.message.reply_text(msg, parse_mode="Markdown")


@require_admin
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Рассылка сообщения всем авторизованным пользователям от имени админа.
    Можно использовать двумя способами:
    1. /broadcast <текст сообщения>
    2. /broadcast (пустая команда) → бот попросит ввести текст следующим сообщением
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Если текст указан после команды — рассылаем сразу
    if ctx.args:
        text = " ".join(ctx.args)
        await _do_broadcast(update, ctx, text)
        return

    # Иначе включаем режим ожидания текста
    PENDING_INPUT[(chat_id, user_id)] = "broadcast"
    await update.message.reply_text(
        "📢 Введи текст для рассылки следующим сообщением.\n"
        "Сообщение получат все авторизованные пользователи.\n\n"
        "Чтобы отменить — напиши /cancel"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает любой режим ожидания ввода."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if PENDING_INPUT.pop((chat_id, user_id), None):
        await update.message.reply_text("❌ Отменено.")
    else:
        await update.message.reply_text("Нечего отменять.")


async def _do_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Выполняет рассылку текста всем авторизованным."""
    text = text.strip()
    if not text:
        await update.message.reply_text("❌ Пустое сообщение, нечего рассылать.")
        return

    sender = update.effective_user
    sender_name = sender.first_name or "Админ"

    recipients = storage.get_all_recipient_chat_ids()
    if not recipients:
        await update.message.reply_text("📭 Нет получателей.")
        return

    message = f"📢 Сообщение от {sender_name}:\n\n{text}"

    sent = 0
    failed = 0
    for rid in recipients:
        # Не отправляем самому отправителю — он и так видит что напечатал
        if rid == update.effective_chat.id:
            continue
        try:
            await ctx.bot.send_message(chat_id=rid, text=message)
            sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"broadcast to {rid} failed: {e}")

    summary = f"✅ Разослано: {sent}"
    if failed > 0:
        summary += f" (не доставлено: {failed})"
    await update.message.reply_text(summary)


@require_admin
async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Диагностика конфигурации бота."""
    import gist_storage

    pwd_set      = "✅ задан" if config.ACCESS_PASSWORD else "❌ НЕ ЗАДАН"
    pwd_len      = len(config.ACCESS_PASSWORD) if config.ACCESS_PASSWORD else 0
    admins       = config.ADMIN_CHAT_IDS or "❌ пусто"
    gist_token   = "✅ задан" if config.GITHUB_TOKEN else "❌ НЕ ЗАДАН"
    gist_id      = config.GIST_ID or "❌ НЕ ЗАДАН"

    # Проверка Gist
    try:
        users_data = gist_storage.read(gist_storage.FILE_USERS)
        gist_status = f"✅ работает, пользователей: {len(users_data)}"
    except Exception as e:
        gist_status = f"❌ ошибка: {e}"

    user_id = update.effective_user.id
    is_auth = auth.is_authorized(user_id)
    is_adm  = auth.is_admin(user_id)

    text = (
        "🔧 *Диагностика бота*\n\n"
        f"*ACCESS_PASSWORD:* {pwd_set} (длина: {pwd_len})\n"
        f"*ADMIN_CHAT_IDS:* `{admins}`\n"
        f"*GITHUB_TOKEN:* {gist_token}\n"
        f"*GIST_ID:* `{gist_id}`\n"
        f"*Gist:* {gist_status}\n\n"
        f"*Твой user_id:* `{user_id}`\n"
        f"*Авторизован:* {'✅' if is_auth else '❌'}\n"
        f"*Админ:* {'✅' if is_adm else '❌'}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@require_admin
async def cmd_checkfonbet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Тест парсера Fonbet: запрашивает события прямо сейчас и сверяет
    с текущим списком прогнозов. Показывает что найдено, что нет.
    """
    logger.info(f"/checkfonbet вызвана пользователем {update.effective_user.id}")

    try:
        import fonbet
    except Exception as e:
        logger.exception(f"/checkfonbet: не удалось импортировать fonbet: {e}")
        await update.message.reply_text(f"❌ Модуль fonbet не загружен: {e}")
        return

    try:
        msg = await update.message.reply_text("🔄 Запрашиваю Fonbet...")
    except Exception as e:
        logger.exception(f"/checkfonbet: ошибка отправки: {e}")
        return

    try:
        # 1. Проверяем что URL находится
        host = fonbet.get_working_host()
        if not host:
            await msg.edit_text(
                "❌ Не удалось найти рабочий URL Fonbet.\n"
                "Ни автодетект, ни fallback-хосты не ответили."
            )
            return

        # 2. Получаем события
        events = fonbet.fetch_events()
        if not events:
            await msg.edit_text(
                f"⚠️ URL найден ({host}), но события не получены.\n"
                "Возможно изменился формат API."
            )
            return

        # 3. Сверяем с прогнозами
        predictions = storage.load_predictions()
        if not predictions:
            await msg.edit_text(
                f"✅ Fonbet работает!\n"
                f"Хост: {host}\n"
                f"Получено событий: {len(events)}\n\n"
                f"📋 Список прогнозов пуст — добавь через /add чтобы проверить матчинг."
            )
            return

        lines = [
            f"✅ Fonbet работает!",
            f"Хост: {host}",
            f"Событий получено: {len(events)}",
            f"Прогнозов в списке: {len(predictions)}",
            "",
            "── Результаты матчинга ──",
        ]

        found_count = 0
        for pred in predictions:
            team1, team2 = fonbet.extract_teams_from_prediction(pred.text)
            event = fonbet.find_matching_event(pred.text, events, expected_utc=pred.match_time)

            if event:
                found_count += 1
                status = "🔴 ЛАЙВ" if event["is_live"] else "📋 ЛИНИЯ"
                status += f" [{event.get('live_reason')}]"
                odds = ""
                if event.get("odd_p1") or event.get("odd_p2"):
                    p1 = f"{event['odd_p1']:.2f}" if event.get("odd_p1") else "—"
                    p2 = f"{event['odd_p2']:.2f}" if event.get("odd_p2") else "—"
                    odds = f" (П1 {p1} / П2 {p2})"
                lines.append(
                    f"\n✅ {status}{odds}\n"
                    f"   Прогноз: {team1} — {team2}\n"
                    f"   Fonbet: {event['team1']} — {event['team2']}\n"
                    f"   Совпадение: {event.get('match_score')}% [{event.get('match_tier')}]"
                    f" · лига: {event.get('league_name') or 'неизвестна'}"
                )
            else:
                lines.append(f"\n❌ Не найден: {team1} — {team2}")

        lines.append(f"\n\n📊 Итого найдено: {found_count} из {len(predictions)}")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (список обрезан)"

        await msg.edit_text(text)

    except Exception as e:
        logger.exception(f"/checkfonbet: ошибка выполнения: {e}")
        try:
            await msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            await update.message.reply_text(f"❌ Ошибка: {e}")


@require_admin
async def cmd_syncdiscord(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительно перечитать Discord-канал на новые прогнозы."""
    if not config.DISCORD_TOKEN:
        await update.message.reply_text("⚠️ Discord не настроен (нет DISCORD_TOKEN).")
        return

    msg = await update.message.reply_text("🔄 Перечитываю Discord-канал...")

    try:
        import discord_listener
        ok, info = discord_listener.trigger_manual_recheck()
    except Exception as e:
        logger.exception(f"/syncdiscord error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    if ok:
        await msg.edit_text(f"✅ Синхронизация завершена.\n\n{info}")
    else:
        await msg.edit_text(f"❌ {info}")


@require_admin
async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Диагностика слежения за голосовым каналом Discord."""
    try:
        import discord_listener
        text = discord_listener.get_voice_status()
    except Exception as e:
        logger.exception(f"/voice error: {e}")
        text = f"❌ Ошибка: {e}"
    # Без parse_mode: в названиях каналов бывают _ и *, Markdown на них ломается
    await update.message.reply_text(text)


@require_admin
async def cmd_factors(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Диагностика: дампит ВСЕ факторы (рынки) первого матча из листа прогнозов.
    Нужно для определения кодов форовых рынков Фонбета.
    Использование: /factors  — берёт первый прогноз
                   /factors 3 — берёт прогноз №3 из /list
    """
    import fonbet

    predictions = storage.load_predictions()
    if not predictions:
        await update.message.reply_text("📋 Список пуст. Добавь прогноз через /add")
        return

    # Какой прогноз дампить
    idx = 0
    if ctx.args:
        try:
            idx = int(ctx.args[0]) - 1
            if idx < 0 or idx >= len(predictions):
                await update.message.reply_text(f"❌ Нет прогноза №{ctx.args[0]}. Всего: {len(predictions)}")
                return
        except ValueError:
            await update.message.reply_text("❌ Укажи номер: /factors 3")
            return

    pred = predictions[idx]
    msg = await update.message.reply_text(f"🔄 Ищу факторы матча:\n{pred.text[:80]}...")

    try:
        result = fonbet.dump_event_factors(pred.text)
    except Exception as e:
        logger.exception(f"/factors error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    if not result:
        await msg.edit_text(
            f"❌ Матч не найден на Фонбете:\n{pred.text[:80]}\n\n"
            "Возможно он сейчас не в линии, или названия слишком разные."
        )
        return

    factors = result.get("factors", [])
    status = "🔴 ЛАЙВ" if result["is_live"] else "📋 ЛИНИЯ"

    lines = [
        f"{status}  (совпадение {result.get('match_score')}%)",
        f"{result['team1']} — {result['team2']}",
        f"Всего факторов: {len(factors)}",
    ]
    raw = result.get("raw_fields")
    if raw:
        lines.append("\n🔧 Поля события (для ссылки):")
        for k, v in raw.items():
            lines.append(f"  {k} = {v}")
    lines += [
        "",
        "Код | Кэф | Параметр",
        "─────────────────────",
    ]
    for f in factors:
        code = f.get("f")
        val = f.get("v")
        pt = f.get("pt")
        p = f.get("p")
        param = ""
        if pt is not None:
            param = f"  pt={pt}"
        elif p is not None:
            param = f"  p={p}"
        lines.append(f"{code} | {val}{param}")

    text = "\n".join(lines)
    # Telegram лимит — режем на куски по 4000
    if len(text) <= 4000:
        await msg.edit_text(text)
    else:
        await msg.edit_text(text[:4000])
        rest = text[4000:]
        while rest:
            await update.message.reply_text(rest[:4000])
            rest = rest[4000:]


@require_admin
async def cmd_bk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Проверка других контор (BetBoom / Winline / Лига Ставок) по прогнозу из листа.
    Использование: /bk        — первый прогноз
                   /bk 3      — прогноз №3 из /list
    Показывает, нашёлся ли матч и есть ли рынок из прогноза.
    """
    import fonbet
    import bookmakers

    predictions = storage.load_predictions()
    if not predictions:
        await update.message.reply_text("📋 Список пуст. Добавь прогноз через /add")
        return

    idx = 0
    if ctx.args:
        try:
            idx = int(ctx.args[0]) - 1
            if idx < 0 or idx >= len(predictions):
                await update.message.reply_text(f"❌ Нет прогноза №{ctx.args[0]}. Всего: {len(predictions)}")
                return
        except ValueError:
            await update.message.reply_text("❌ Укажи номер: /bk 3")
            return

    pred = predictions[idx]
    team1, team2 = fonbet.extract_teams_from_prediction(pred.text)
    bet = fonbet.parse_bet_from_prediction(pred.text)

    if not bet:
        await update.message.reply_text(
            f"❌ Не смог распознать ставку в прогнозе:\n{pred.text[:120]}"
        )
        return

    msg = await update.message.reply_text(
        f"🔄 Проверяю конторы:\n{team1} — {team2}\n"
        f"Рынок: {bet['type']} {bet.get('value') or bet.get('team')}"
    )

    try:
        import asyncio
        results = await asyncio.to_thread(
            bookmakers.check_all, team1, team2, bet, pred.match_time
        )
    except Exception as e:
        logger.exception(f"/bk error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")
        return

    lines = [f"{team1} — {team2}", ""]
    for r in results:
        if r["status"] == "ok":
            lines.append(f"✅ {r['name']}: {r['label']} = {r['odd']:.2f}"
                         + (" 🔴 лайв" if r["is_live"] else ""))
        elif r["status"] == "no_market":
            lines.append(f"➖ {r['name']}: матч есть, рынка {r['label']} нет")
        elif r["status"] == "no_match":
            lines.append(f"❌ {r['name']}: матч не найден")
        else:
            lines.append(f"⚠️ {r['name']}: контора не ответила / формат не распознан")

    lines.append("")
    lines.append("Если контора не отвечает — запусти bk_probe.py (см. README).")
    await msg.edit_text("\n".join(lines))


# ── Обработка текста ─────────────────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id
    key = (chat_id, user_id)
    mode = PENDING_INPUT.pop(key, None)

    # В группе бот реагирует только на команды и активные диалоги — никаких "Используй /add"
    is_group = update.effective_chat.type in ("group", "supergroup")
    if mode is None:
        if not is_group:
            await update.message.reply_text("Используй /start для начала работы.")
        return

    # ── Ввод пароля (только в личке) ─────────────────────────────────────────
    if mode == "password":
        entered = update.message.text.strip()
        if entered == config.ACCESS_PASSWORD:
            auth.authorize(user_id, user.username or "", user.first_name or "")
            await update.message.reply_text("✅ Доступ открыт!")
            await _send_help(update, user_id)
            await _notify_admins_new_user(ctx, user, user_id)
        else:
            await update.message.reply_text("❌ Неверный пароль. Попробуй ещё раз через /start")
            await _notify_admins_wrong_password(ctx, user, user_id)
        return

    # Остальные режимы требуют авторизации
    if not auth.is_authorized(user_id):
        await update.message.reply_text("🔒 Доступ закрыт.")
        return

    s = storage.load_settings(chat_id)

    # ── Обратная связь (любой авторизованный) ─────────────────────────────
    if mode == "feedback":
        await _send_feedback(update, ctx, update.message.text)
        return

    # ── Рассылка от админа ────────────────────────────────────────────────
    if mode == "broadcast":
        if not auth.is_admin(user_id):
            return
        await _do_broadcast(update, ctx, update.message.text)
        return

    if mode == "timezone":
        # Персональная настройка — доступна всем авторизованным
        try:
            offset = int(update.message.text.strip().replace("+", ""))
            if not -12 <= offset <= 14:
                raise ValueError
            us = storage.load_settings(user_id)
            us.timezone_offset = offset
            storage.save_settings(us)
            await update.message.reply_text(f"✅ Часовой пояс: UTC+{offset}")
        except ValueError:
            await update.message.reply_text("❌ Введи число от -12 до 14")
        return

    if mode == "predictions":
        if not auth.is_admin(user_id):
            return
        try:
            preds = parse_predictions(update.message.text, s.timezone_offset, source="manual")
            logger.info(f"/add: распознано {len(preds)} прогнозов от {user_id}")
        except Exception as e:
            logger.exception(f"/add parse error: {e}")
            await update.message.reply_text(f"❌ Ошибка разбора: {e}")
            return

        if not preds:
            await update.message.reply_text(
                "❌ Не нашёл время матча. Формат: `2-00` или `14:30`",
                parse_mode="Markdown",
            )
            return

        try:
            added = storage.add_predictions(new_preds=preds)
            logger.info(f"/add: добавлено {added} в хранилище")
        except Exception as e:
            logger.exception(f"/add storage error: {e}")
            await update.message.reply_text(f"❌ Ошибка сохранения: {e}")
            return

        skipped = len(preds) - added
        lines = [f"✅ Добавлено: {added}" + (f"  (дублей: {skipped})" if skipped else "")]
        for i, p in enumerate(preds, 1):
            t = format_time_local(p, s.timezone_offset)
            lines.append(f"\n{i}. ⏰ {t}\n   {display_text_without_time(p, s.timezone_offset)}")

        # Без Markdown — названия команд могут содержать спецсимволы (_ * [ ])
        try:
            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            logger.exception(f"/add reply error: {e}")

        # Broadcast другим авторизованным пользователям (кроме того кто добавил)
        if added > 0:
            try:
                recipients = storage.get_all_recipient_chat_ids()
                for rid in recipients:
                    if rid == chat_id:
                        continue
                    try:
                        rs = storage.load_settings(rid)
                        rlines = [f"📥 Админ добавил +{added} прогнозов"]
                        for p in preds[-added:]:
                            t = format_time_local(p, rs.timezone_offset)
                            rlines.append(f"⏰ {t}  {display_text_without_time(p, rs.timezone_offset)}")
                        await ctx.bot.send_message(chat_id=rid, text="\n".join(rlines))
                    except Exception as e:
                        logger.error(f"broadcast add to {rid} failed: {e}")
            except Exception as e:
                logger.exception(f"/add broadcast error: {e}")
        return


# ── Кнопки ───────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if (q.data or "").startswith(("mute|", "blk|", "ud|")):
        return await on_action_callback(update, ctx)
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    await q.answer()

    if not auth.is_authorized(user_id):
        await q.edit_message_text("🔒 Доступ закрыт.")
        return

    # Очистка списка — только админ
    if q.data in ("clear_yes", "clear_no") and not auth.is_admin(user_id):
        await q.answer("⛔ Только админ.", show_alert=True)
        return

    if q.data == "clear_yes":
        storage.clear_predictions(chat_id)
        await q.edit_message_text("🗑 Все прогнозы удалены.")

    elif q.data == "clear_no":
        await q.edit_message_text("Отмена.")

    elif q.data == "set_tz":
        # Персональная настройка — ключ по user_id
        PENDING_INPUT[(chat_id, user_id)] = "timezone"
        await q.edit_message_text(
            "🌐 Введи смещение UTC+N (`3` — Москва, `0` — UTC):",
            parse_mode="Markdown",
        )

    elif q.data in ("tg_reminders", "tg_matchout", "tg_crooked",
                    "tg_underdog", "tg_closematch", "tg_newpreds", "tg_voice"):
        # Атомарное переключение — защита от затирания при быстрых нажатиях
        field_map = {
            "tg_reminders": "notify_reminders",
            "tg_matchout": "notify_match_out",
            "tg_crooked": "notify_crooked",
            "tg_underdog": "notify_underdog",
            "tg_closematch": "notify_close_match",
            "tg_newpreds": "notify_new_preds",
            "tg_voice": "notify_voice",
        }
        s = storage.toggle_setting(user_id, field_map[q.data])
        await q.edit_message_text(
            _SETTINGS_TEXT,
            reply_markup=_settings_keyboard(s),
            parse_mode="Markdown",
        )


# ── /today: матчи на сегодня и статус на Фонбете ─────────────────────────────

@require_auth
async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = storage.load_settings(user_id)
    now = datetime.utcnow()
    tz = timedelta(hours=s.timezone_offset)
    today = (now + tz).date()
    preds = storage.load_predictions()

    mine = [p for p in preds if (p.match_time + tz).date() <= today]
    later = [p for p in preds if (p.match_time + tz).date() > today]
    blocked = set(s.blocked_countries or [])

    if not mine:
        text = f"📅 На сегодня ({today:%d.%m}) прогнозов нет."
        if later:
            text += f"\nНа следующие дни: {len(later)} — /list"
        await update.message.reply_text(text)
        return

    lines = [f"📅 Сегодня, {today:%d.%m}: {len(mine)}\n"]
    for i, p in enumerate(mine, 1):
        emoji, status = userprefs.match_status(p, now)
        marks = ""
        if p.id in (s.muted_ids or []):
            marks += " 🔕"
        if blocked and userprefs.country_key(p.text) in blocked:
            marks += " 🚫"
        t = format_time_local(p, s.timezone_offset)
        lines.append(f"{i}. ⏰ {t} {emoji} {status}{marks}\n   {display_text_without_time(p, s.timezone_offset)}")
    if later:
        lines.append(f"\nНа следующие дни: {len(later)} — /list")
    if s.quiet_from is not None and userprefs.in_quiet_hours(s.quiet_from, s.quiet_to, now, s.timezone_offset):
        lines.append(f"\n🌙 Сейчас тихие часы ({s.quiet_from}:00–{s.quiet_to}:00) — уведомления не приходят.")
    await _send_chunked(update, lines, limit=3900)


# ── Личные фильтры: тихие часы, страны, заглушённые матчи ────────────────────

@require_auth
async def cmd_quiet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    action, a, b = userprefs.parse_quiet_args(ctx.args or [])
    if action == "show":
        s = storage.load_settings(user_id)
        if s.quiet_from is None:
            await update.message.reply_text(
                "🌙 Тихие часы выключены.\nВключить: `/quiet 23 8` — не беспокоить с 23:00 до 8:00 "
                "(по твоему часовому поясу).", parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f"🌙 Тихие часы: {s.quiet_from}:00–{s.quiet_to}:00 (UTC+{s.timezone_offset}).\n"
                f"Выключить: `/quiet off`", parse_mode="Markdown")
    elif action == "off":
        storage.update_settings(user_id, lambda s: (setattr(s, "quiet_from", None), setattr(s, "quiet_to", None)))
        await update.message.reply_text("🌙 Тихие часы выключены.")
    elif action == "set":
        def mut(s):
            s.quiet_from, s.quiet_to = a, b
        s = storage.update_settings(user_id, mut)
        await update.message.reply_text(
            f"🌙 Тихие часы: {a}:00–{b}:00 (UTC+{s.timezone_offset}). В это время уведомления "
            f"не приходят (пропущенные не копятся). Выключить: /quiet off")
    else:
        await update.message.reply_text("Формат: `/quiet 23 8` (часы 0–23, начало и конец разные) или `/quiet off`",
                                        parse_mode="Markdown")


@require_auth
async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = userprefs.normalize_country(" ".join(ctx.args or []))
    if not name:
        s = storage.load_settings(user_id)
        if not s.blocked_countries:
            await update.message.reply_text("🚫 Заблокированных стран нет.\nДобавить: `/block Poland` "
                                            "(название — как в начале прогноза).", parse_mode="Markdown")
        else:
            await update.message.reply_text("🚫 Заблокированные страны:\n" +
                                            "\n".join(f"• {c.title()}" for c in s.blocked_countries) +
                                            "\n\nУбрать: `/unblock Poland`", parse_mode="Markdown")
        return
    storage.update_settings(user_id, lambda s: s.blocked_countries.append(name)
                            if name not in s.blocked_countries else None)
    await update.message.reply_text(f"🚫 Матчи страны «{name.title()}» больше не приходят. Вернуть: /unblock {name}")


@require_auth
async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = userprefs.normalize_country(" ".join(ctx.args or []))
    if not name:
        await update.message.reply_text("Формат: `/unblock Poland`", parse_mode="Markdown")
        return
    had = []

    def mut(s):
        if name in s.blocked_countries:
            s.blocked_countries.remove(name)
            had.append(1)
    storage.update_settings(user_id, mut)
    await update.message.reply_text(f"✅ «{name.title()}» разблокирована." if had else f"«{name.title()}» не была заблокирована.")


@require_auth
async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = []
    storage.update_settings(user_id, lambda s: (count.append(len(s.muted_ids)), s.muted_ids.clear()))
    await update.message.reply_text(f"🔔 Заглушённые матчи возвращены: {count[0] if count else 0}.")


# ── Диагностика (админ) ──────────────────────────────────────────────────────

@require_admin
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    snap = health.snapshot()
    now = time.time()
    f, d = snap["fonbet"], snap["discord"]
    preds = storage.load_predictions()
    stats = storage.journal_stats()

    if f["last_ok"] is None and f["last_fail"] is None:
        fb = "⏳ ещё не запрашивался (нет матчей в окне мониторинга)"
    elif f["streak"] == 0:
        fb = f"✅ отвечает, последний успех {health.ago(f['last_ok'], now)}, событий: {f['events']}"
    else:
        fb = (f"🚨 не отвечает: {f['streak']} неудач подряд, причина: {f['error']}; "
              f"последний успех {health.ago(f['last_ok'], now)}")

    if not d["enabled"]:
        dc = "отключён (нет DISCORD_TOKEN)"
    else:
        link = {True: "✅ подключён", False: "🚨 нет соединения", None: "⏳ подключается"}[d["connected"]]
        dc = f"{link}; перечит: {health.ago(d['last_sync_ok'], now)}"
        if d["last_sync_error"]:
            dc += f"\n   последняя ошибка: {d['last_sync_error']}"
        if d["last_sync_stats"]:
            st = d["last_sync_stats"]
            dc += (f"\n   последний проход: найдено {st.get('found', 0)}, новых {st.get('added', 0)}, "
                   f"пропущено прошедших {st.get('skipped_stale', 0)}, неясных {st.get('skipped_undetermined', 0)}")

    from_dc = sum(1 for p in preds if p.source == "discord")
    matched = sum(1 for p in preds if p.fonbet_matched)
    undetermined = len(storage.list_undetermined(1000))
    alerts = ", ".join(snap["alerts"]) or "нет"

    text = (
        "🩺 Состояние бота\n\n"
        f"Фонбет: {fb}\n\n"
        f"Discord: {dc}\n\n"
        f"Расписание: {len(preds)} прогнозов (из Discord: {from_dc}), найдено на Фонбете: {matched}\n"
        f"Матчей без даты ждут решения: {undetermined}" + (" → /undetermined" if undetermined else "") + "\n"
        f"Журнал матчинга: найдено {stats['found']}, промахов {stats['miss']} → /misses\n"
        f"Telegram: конфликтов getUpdates за 10 мин: {health.telegram_conflicts_recent()}\n"
        f"Активные тревоги: {alerts}\n"
        f"Сборка: {version.BUILD} · аптайм: {health._fmt_dur(now - snap['started_at'])}"
    )
    await update.message.reply_text(text)


@require_admin
async def cmd_misses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/misses [N] — последние промахи с причинами; /misses found [N] — последние найденные (уровень, %)."""
    args = list(ctx.args or [])
    kind = "found" if args and args[0].lower() == "found" else "miss"
    if args and args[0].lower() == "found":
        args = args[1:]
    n = 8
    if args and args[0].isdigit():
        n = max(1, min(int(args[0]), 20))

    stats = storage.journal_stats()
    entries = storage.journal_recent(n, kind)
    tier_s = ", ".join(f"{k} {v}" for k, v in stats["tiers"].items()) or "—"
    reason_s = ", ".join(f"{k} {v}" for k, v in stats["reasons"].items()) or "—"
    tz = timedelta(hours=storage.load_settings(update.effective_user.id).timezone_offset)

    lines = [f"📊 Журнал матчинга ({stats['total']} записей): найдено {stats['found']} ({tier_s}), "
             f"промахов {stats['miss']} ({reason_s})\n"]
    if not entries:
        lines.append("Записей нет." if kind == "miss" else "Найденных пока нет.")
    for i, e in enumerate(entries, 1):
        if kind == "miss":
            try:
                mt = datetime.fromisoformat(e["match_time"]) + tz
                when = f"{mt:%d.%m %H:%M}"
            except Exception:
                when = "?"
            lines.append(f"{i}. ⏰ {when} · {e.get('text', '')[:90]}\n   ❌ {e.get('why')}")
            for c in (e.get("best") or [])[:3]:
                lg = f" [{c['league']}]" if c.get("league") else ""
                lines.append(f"   • {c['score']}% {c['match']}{lg}: {c['why']}")
        else:
            lines.append(f"{i}. {e.get('score')}% [{e.get('tier')}] за {e.get('mins_before_start')} мин до старта"
                         f"{' (лайв)' if e.get('live') else ''}\n   {e.get('text', '')[:80]}\n   → {e.get('match')}"
                         f"{' · ' + e['league'] if e.get('league') else ''}")
    lines.append("\nЕщё: /misses 15 · /misses found")
    await _send_chunked(update, lines, limit=3900)


@require_admin
async def cmd_aliases(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = storage.list_aliases(40)
    if not rows:
        await update.message.reply_text("🔤 Запомненных пар названий пока нет. Бот добавляет их сам, когда уверенно "
                                        "находит матч, хотя названия различались.")
        return
    lines = [f"🔤 Запомненные пары (прогноз → Фонбет), {len(rows)}:\n"]
    for r in rows:
        lines.append(f"• {r['pred']} → {', '.join(r['evs'])} (×{r['n']})")
    lines.append("\nУдалить ошибочную: /unalias <название из прогноза>")
    await _send_chunked(update, lines, limit=3900)


@require_admin
async def cmd_unalias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = " ".join(ctx.args or []).strip()
    if not name:
        await update.message.reply_text("Формат: /unalias <название команды как в прогнозе>")
        return
    ok = storage.remove_alias(name)
    await update.message.reply_text("🗑 Алиас удалён." if ok else "Такого алиаса нет (см. /aliases).")


@require_admin
async def cmd_undetermined(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = storage.list_undetermined(10)
    if not items:
        await update.message.reply_text("✅ Матчей с неопределённой датой нет.")
        return
    for item in items:
        btns = storage.ud_buttons(item)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(l, callback_data=d) for l, d in btns[:2]],
            [InlineKeyboardButton(l, callback_data=d) for l, d in btns[2:]],
        ])
        await update.message.reply_text(
            f"❓ Дата не определена\n\n{item['text'][:300]}\n\n{item['reason']}.\n\nКогда он играет?",
            reply_markup=markup,
        )


# ── Кнопки действий: заглушить матч / страну, дата неопределённого матча ─────

async def on_action_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    if not auth.is_authorized(user_id):
        await q.answer("🔒 Доступ закрыт.", show_alert=True)
        return
    kind, _, rest = (q.data or "").partition("|")

    if kind == "mute":
        pred_id = rest

        def mut(s):
            if pred_id not in s.muted_ids:
                s.muted_ids.append(pred_id)
            live = {p.id for p in storage.load_predictions()}
            s.muted_ids[:] = [i for i in s.muted_ids if i in live]   # старые id чистим
        storage.update_settings(user_id, mut)
        await q.answer("🔕 Матч заглушен — по нему больше не будет уведомлений. Вернуть: /unmute", show_alert=True)

    elif kind == "blk":
        pred = next((p for p in storage.load_predictions() if p.id == rest), None)
        country = userprefs.country_key(pred.text) if pred else ""
        if not country:
            await q.answer("Матч уже завершён — заблокируй страну командой /block <страна>", show_alert=True)
            return
        storage.update_settings(user_id, lambda s: s.blocked_countries.append(country)
                                if country not in s.blocked_countries else None)
        await q.answer(f"🚫 Страна «{country.title()}» заблокирована. Вернуть: /unblock {country}", show_alert=True)

    elif kind == "ud":
        if not auth.is_admin(user_id):
            await q.answer("⛔ Только админ.", show_alert=True)
            return
        try:
            mid, h8, choice = rest.split("|")
        except ValueError:
            await q.answer("Некорректная кнопка", show_alert=True)
            return
        res = await asyncio.to_thread(storage.resolve_undetermined, mid, h8, choice)
        head = (q.message.text or "").split("\n\n")[1] if q.message and q.message.text else res["text"]
        status = res["status"]
        if status == "notfound":
            msg = "ℹ️ Этот вопрос уже неактуален (матч решён или сообщение пропало)."
        elif status == "skipped":
            msg = f"🚫 Пропущен: {head}"
        elif status == "past":
            msg = f"⏭ Этот матч уже прошёл на выбранную дату — не добавлен: {head}"
        else:
            local = res["dt"] + timedelta(hours=config.DEFAULT_TZ_OFFSET)
            msg = f"✅ {'Добавлен' if status == 'added' else 'Уже в расписании'} на {local:%d.%m %H:%M}: {head}"
        await q.answer()
        await q.edit_message_text(msg)
        if status == "added" and res.get("pred"):
            try:
                import discord_listener
                await discord_listener._notify_added([res["pred"]], "дата выбрана админом")
            except Exception as e:
                logger.error(f"notify_added after ud failed: {e}")


# ── Уведомления админам ──────────────────────────────────────────────────────

async def _notify_admins_new_user(ctx, user, user_id):
    for admin_id in config.ADMIN_CHAT_IDS:
        if admin_id == user_id:
            continue
        try:
            uname = f"@{user.username}" if user.username else "—"
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🆕 *Новый пользователь авторизовался*\n\n"
                    f"Имя: {user.first_name or '—'}\n"
                    f"Username: {uname}\n"
                    f"🆔 `{user_id}`"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")


async def _notify_admins_wrong_password(ctx, user, user_id):
    for admin_id in config.ADMIN_CHAT_IDS:
        try:
            uname = f"@{user.username}" if user.username else "—"
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"⚠️ *Неверный пароль*\n\n"
                    f"От: {user.first_name or '—'} ({uname})\n"
                    f"🆔 `{user_id}`"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass
