import logging
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import storage
import auth
import config
from parser import parse_predictions, format_time_local

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
            "/settings — настройки\n\n"
            "👥 *Управление пользователями:*\n"
            "/users, /ban, /unban, /remove\n\n"
            "🔔 Уведомления за 30 и 5 минут до матча.\n"
            "🗑 Автоудаление через 5 минут после старта."
        )
    else:
        text = (
            "⚽ *Бот-помощник для прогнозов*\n\n"
            "📋 *Доступные команды:*\n"
            "/list — посмотреть активные прогнозы\n\n"
            "🔔 Уведомления о матчах ты получаешь автоматически.\n\n"
            "_Добавление и редактирование прогнозов доступно только админам._"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Команды чтения (доступны всем авторизованным) ────────────────────────────

@require_auth
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    preds = storage.load_predictions(chat_id)
    s = storage.load_settings(chat_id)

    if not preds:
        await update.message.reply_text("📋 Список пуст.")
        return

    lines = [f"📋 *Активных прогнозов: {len(preds)}*\n"]
    for i, p in enumerate(preds, 1):
        t = format_time_local(p, s.timezone_offset)
        status = " 🔔" if p.notified_30 else ""
        status = " ✅" if p.notified_5 else status
        src = " 🤖" if p.source == "discord" else ""
        # Админам показываем ID для удаления, остальным — нет
        id_line = f"\n   🆔 `{p.id}`" if auth.is_admin(update.effective_user.id) else ""
        lines.append(f"{i}. ⏰ {t}{status}{src}\n   {p.text}{id_line}")

    if auth.is_admin(update.effective_user.id):
        lines.append("\nДля удаления: `/delete <id>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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


@require_admin
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = storage.load_settings(chat_id)
    kb = [[InlineKeyboardButton(f"🌐 Часовой пояс: UTC+{s.timezone_offset}", callback_data="set_tz")]]
    await update.message.reply_text(
        "⚙️ *Настройки*\n\nУведомления: за 30 и 5 минут.\nАвтоудаление: через 5 минут после старта.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )


# ── Админ-команды управления пользователями ──────────────────────────────────

@require_admin
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    users = auth.list_users()
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
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    if mode == "timezone":
        if not auth.is_admin(user_id):
            return
        try:
            offset = int(update.message.text.strip().replace("+", ""))
            if not -12 <= offset <= 14:
                raise ValueError
            s.timezone_offset = offset
            storage.save_settings(s)
            await update.message.reply_text(f"✅ Часовой пояс: UTC+{offset}")
        except ValueError:
            await update.message.reply_text("❌ Введи число от -12 до 14")
        return

    if mode == "predictions":
        if not auth.is_admin(user_id):
            return
        preds = parse_predictions(update.message.text, s.timezone_offset, source="manual")
        if not preds:
            await update.message.reply_text(
                "❌ Не нашёл время матча. Формат: `2-00` или `14:30`",
                parse_mode="Markdown",
            )
            return

        added = storage.add_predictions(chat_id, preds)
        skipped = len(preds) - added
        lines = [f"✅ *Добавлено: {added}*" + (f"  (дублей: {skipped})" if skipped else "")]
        for i, p in enumerate(preds, 1):
            t = format_time_local(p, s.timezone_offset)
            lines.append(f"\n{i}. ⏰ {t}\n   {p.text}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return


# ── Кнопки ───────────────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user_id = q.from_user.id
    chat_id = q.message.chat_id
    await q.answer()

    if not auth.is_authorized(user_id):
        await q.edit_message_text("🔒 Доступ закрыт.")
        return

    if q.data in ("clear_yes", "clear_no", "set_tz") and not auth.is_admin(user_id):
        await q.answer("⛔ Только админ.", show_alert=True)
        return

    if q.data == "clear_yes":
        storage.clear_predictions(chat_id)
        await q.edit_message_text("🗑 Все прогнозы удалены.")

    elif q.data == "clear_no":
        await q.edit_message_text("Отмена.")

    elif q.data == "set_tz":
        PENDING_INPUT[(chat_id, user_id)] = "timezone"
        await q.edit_message_text(
            "🌐 Введи смещение UTC+N (`3` — Москва, `0` — UTC):",
            parse_mode="Markdown",
        )


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
