"""
Версия сборки и самопроверка комплекта файлов.

Бот деплоится заливкой файлов вручную, и легко залить только часть: тогда на сервере
работает смесь старых и новых модулей (команды «не срабатывают», фоновые задачи не
запускаются, а в логах тишина). При старте bot.py вызывает check_consistency():
для каждого файла проверяется «маркер» — то, что появилось в его последней версии.
Файлы без маркера считаются устаревшими, список уходит в лог и админам в Telegram.

Выкладываешь новую версию — поднимай BUILD.
"""
import importlib
import logging

logger = logging.getLogger(__name__)

BUILD = "2026-11-05.5"


def _has(mod, *names) -> bool:
    return all(hasattr(mod, n) for n in names)


def check_consistency(check_discord: bool = True) -> list[str]:
    """
    Возвращает список файлов, которые НЕ соответствуют текущей сборке
    (отсутствуют или старые). Пустой список — комплект целый.
    """
    problems: list[str] = []

    def check(filename: str, module: str, test):
        try:
            mod = importlib.import_module(module)
            ok = bool(test(mod))
        except Exception as e:      # нет файла / ошибка импорта / не тот интерфейс
            logger.warning(f"Самопроверка: {filename} — {type(e).__name__}: {e}")
            ok = False
        if not ok:
            problems.append(filename)

    check("models.py", "models", lambda m: {"fonbet_matched", "msg_id"} <= set(m.Prediction.__dataclass_fields__)
          and "quiet_from" in m.UserSettings.__dataclass_fields__)
    check("match_dates.py", "match_dates", lambda m: _has(m, "retention_minutes", "resolve_match_datetime"))
    check("parser.py", "parser", lambda m: _has(m, "split_blocks", "extract_raw_matches"))
    check("fonbet.py", "fonbet", lambda m: _has(m, "LAST_ERROR", "prediction_script", "league_matches"))
    check("storage.py", "storage", lambda m: _has(m, "apply_discord_messages", "journal_add",
                                                  "resolve_undetermined", "update_settings"))
    check("health.py", "health", lambda m: _has(m, "evaluate", "telegram_conflict"))
    check("userprefs.py", "userprefs", lambda m: _has(m, "user_allows", "match_status"))
    check("scheduler.py", "scheduler", lambda m: _has(m, "health_tick", "_last_diag"))
    check("handlers.py", "handlers", lambda m: _has(m, "cmd_commands", "cmd_unknown", "on_error",
                                                    "on_action_callback"))
    if check_discord:
        check("discord_listener.py", "discord_listener", lambda m: _has(m, "_ask_admins_undetermined"))
    return problems


def startup_text(handlers_count: int, problems: list[str]) -> str:
    """Сообщение админам при старте: что запущено и целый ли комплект файлов."""
    lines = [f"✅ Бот запущен · сборка {BUILD} · обработчиков: {handlers_count}"]
    if problems:
        lines.append(
            "\n⚠️ Файлы не от этой сборки (залиты не все или старые): " + ", ".join(problems) +
            "\nЧасть функций и команд работать не будет. Залей комплект целиком и перезапусти бота."
        )
    else:
        lines.append("Комплект файлов целый. Список команд: /commands")
    return "\n".join(lines)
