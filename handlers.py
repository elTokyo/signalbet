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
            "/settings — настройки\n"
            "/broadcast — рассылка всем\n\n"
            "👥 *Управление пользователями:*\n"
            "/users, /ban, /unban, /remove\n\n"
            "🔔 Уведомления за 30 и 5 минут до матча.\n"
            "🗑 Автоудаление через 5 минут после старта."
        )
    else:
        text = (
            "⚽ *Бот-помощник для прогнозов*\n\n"
            "📋 *Доступные команды:*\n"
            "/list — посмотреть активные прогнозы\n"
            "/settings — твои настройки (часовой пояс, уведомления)\n"
            "/feedback — сообщить о баге или оставить отзыв\n\n"
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


@require_auth
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = storage.load_settings(user_id)
    fb_label = "✅ ВКЛ" if s.fonbet_notifications else "☐ ВЫКЛ"
    kb = [
        [InlineKeyboardButton(f"🌐 Часовой пояс: UTC+{s.timezone_offset}", callback_data="set_tz")],
        [InlineKeyboardButton(f"🔴 Уведомления Fonbet: {fb_label}", callback_data="toggle_fonbet")],
    ]
    await update.message.reply_text(
        "⚙️ *Твои настройки*\n\n"
        "🌐 Часовой пояс — для отображения времени матчей.\n"
        "🔴 Уведомления Fonbet — получать ли сигналы о выходе матчей.\n\n"
        "_Уведомления о матчах приходят за 30 и 5 минут до старта._",
        reply_markup=InlineKeyboardMarkup(kb),
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
            event = fonbet.find_matching_event(pred.text, events)

            if event:
                found_count += 1
                status = "🔴 LIVE" if event["is_live"] else "📋 Прематч"
                odds = ""
                if event.get("odd_p1") or event.get("odd_p2"):
                    p1 = f"{event['odd_p1']:.2f}" if event.get("odd_p1") else "—"
                    p2 = f"{event['odd_p2']:.2f}" if event.get("odd_p2") else "—"
                    odds = f" (П1 {p1} / П2 {p2})"
                lines.append(
                    f"\n✅ {status}{odds}\n"
                    f"   Прогноз: {team1} — {team2}\n"
                    f"   Fonbet: {event['team1']} — {event['team2']}"
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
        await msg.edit_text(f"✅ Готово. {info}\n\nНовые прогнозы (если были) уже добавлены.")
    else:
        await msg.edit_text(f"❌ {info}")


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
    status = "🔴 LIVE" if result["is_live"] else "📋 Прематч"

    lines = [
        f"{status}  (совпадение {result.get('match_score')}%)",
        f"{result['team1']} — {result['team2']}",
        f"Всего факторов: {len(factors)}",
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
            lines.append(f"\n{i}. ⏰ {t}\n   {p.text}")

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
                            rlines.append(f"⏰ {t}  {p.text}")
                        await ctx.bot.send_message(chat_id=rid, text="\n".join(rlines))
                    except Exception as e:
                        logger.error(f"broadcast add to {rid} failed: {e}")
            except Exception as e:
                logger.exception(f"/add broadcast error: {e}")
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

    elif q.data == "toggle_fonbet":
        # Персональная настройка по user_id
        s = storage.load_settings(user_id)
        s.fonbet_notifications = not s.fonbet_notifications
        storage.save_settings(s)
        fb_label = "✅ ВКЛ" if s.fonbet_notifications else "☐ ВЫКЛ"
        kb = [
            [InlineKeyboardButton(f"🌐 Часовой пояс: UTC+{s.timezone_offset}", callback_data="set_tz")],
            [InlineKeyboardButton(f"🔴 Уведомления Fonbet: {fb_label}", callback_data="toggle_fonbet")],
        ]
        await q.edit_message_text(
            "⚙️ *Твои настройки*\n\n"
            "🌐 Часовой пояс — для отображения времени матчей.\n"
            "🔴 Уведомления Fonbet — получать ли сигналы о выходе матчей.\n\n"
            "_Уведомления о матчах приходят за 30 и 5 минут до старта._",
            reply_markup=InlineKeyboardMarkup(kb),
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
