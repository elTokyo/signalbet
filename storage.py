"""
Хранилище прогнозов и настроек через GitHub Gist.

ЛОГИКА:
- Прогнозы — ОБЩИЕ для всех авторизованных пользователей.
  Хранятся под ключом "shared" в predictions.json.
- Настройки — ИНДИВИДУАЛЬНЫЕ для каждого пользователя (часовой пояс).
"""
import threading
import logging
from models import Prediction, UserSettings
import config
import gist_storage

logger = logging.getLogger(__name__)

FILE_PREDS    = gist_storage.FILE_PREDICTIONS
FILE_SETTINGS = gist_storage.FILE_SETTINGS

# Единый ключ для общего списка прогнозов
SHARED_KEY = "shared"

_lock = threading.RLock()


# ── Predictions (общие для всех) ─────────────────────────────────────────────

def load_predictions(chat_id: int = None) -> list[Prediction]:
    """chat_id игнорируется — список общий."""
    data = gist_storage.read(FILE_PREDS)

    # Авто-миграция: если есть данные под старыми ключами (chat_id), но нет под "shared" —
    # объединяем всё в один общий список
    if SHARED_KEY not in data and any(k.lstrip("-").isdigit() for k in data.keys()):
        with _lock:
            data = gist_storage.read(FILE_PREDS)
            merged = []
            seen_texts = set()
            for key, items in list(data.items()):
                if key == SHARED_KEY:
                    continue
                if not key.lstrip("-").isdigit():
                    continue
                for item in items:
                    text_key = item.get("text", "").strip().lower()
                    if text_key and text_key not in seen_texts:
                        seen_texts.add(text_key)
                        merged.append(item)
                del data[key]
            merged.sort(key=lambda p: p.get("match_time", ""))
            data[SHARED_KEY] = merged
            gist_storage.write(FILE_PREDS, data)
            logger.info(f"Миграция: объединено {len(merged)} прогнозов в общий список")

    return [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]


def save_predictions(chat_id: int = None, predictions: list[Prediction] = None):
    """chat_id игнорируется — список общий."""
    if predictions is None:
        predictions = []
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        predictions = sorted(predictions, key=lambda p: p.match_time)
        data[SHARED_KEY] = [p.to_dict() for p in predictions]
        gist_storage.write(FILE_PREDS, data)


def add_predictions(chat_id: int = None, new_preds: list[Prediction] = None) -> int:
    """Добавляет в общий список. Пропускает дубли по тексту."""
    if not new_preds:
        return 0
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        existing = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        existing_texts = {p.text.strip().lower() for p in existing}

        added = []
        for p in new_preds:
            if p.text.strip().lower() not in existing_texts:
                added.append(p)
                existing_texts.add(p.text.strip().lower())

        if added:
            all_preds = sorted(existing + added, key=lambda p: p.match_time)
            data[SHARED_KEY] = [p.to_dict() for p in all_preds]
            gist_storage.write(FILE_PREDS, data)
        return len(added)


def _match_key(pred: Prediction) -> str:
    """
    Ключ матча для сравнения «тот же матч»: команды + время.
    Команды берём из текста, нормализуем (убираем регистр, лишние пробелы).
    """
    import re
    from fonbet import extract_teams_from_prediction
    t1, t2 = extract_teams_from_prediction(pred.text)
    # Нормализация: нижний регистр, только буквы/цифры
    def norm(s):
        return re.sub(r'[^a-zа-я0-9]', '', (s or "").lower())
    teams = "|".join(sorted([norm(t1), norm(t2)]))  # сорт чтобы порядок команд не влиял
    # Время с точностью до минуты
    tm = pred.match_time.strftime("%Y%m%d%H%M")
    return f"{teams}@{tm}"


def sync_from_discord(discord_preds: list[Prediction]) -> dict:
    """
    Полная синхронизация общего листа с актуальными прогнозами Discord.
    - новый матч (нет в листе) → добавить
    - матч есть, текст изменился → обновить текст, сбросить crooked_notified
    - discord-матч пропал из Discord и ещё НЕ начался → удалить

    Ручные прогнозы (source != 'discord') не трогаем при удалении.
    Возвращает статистику: {added, updated, removed}.
    """
    from datetime import datetime

    with _lock:
        data = gist_storage.read(FILE_PREDS)
        existing = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]

        # Индексируем существующие по ключу матча
        existing_by_key = {}
        for p in existing:
            existing_by_key[_match_key(p)] = p

        # Индексируем свежие из Discord
        discord_by_key = {}
        for p in discord_preds:
            discord_by_key[_match_key(p)] = p

        added = updated = removed = 0
        result = []
        now = datetime.utcnow()

        # 1. Проходим существующие: оставляем/обновляем/удаляем
        for key, old in existing_by_key.items():
            if key in discord_by_key:
                # Матч всё ещё в Discord — проверяем не изменился ли текст
                new = discord_by_key[key]
                if old.text.strip() != new.text.strip():
                    # Текст обновился (например дописали ставку) — обновляем
                    old.text = new.text
                    old.crooked_notified = False  # пересчитать кривизну
                    updated += 1
                result.append(old)
            else:
                # Матча нет в Discord
                is_discord = (old.source == "discord")
                not_started = old.match_time > now
                if is_discord and not_started:
                    # Удаляем (пропал из Discord, ещё не начался)
                    removed += 1
                    continue
                else:
                    # Ручной прогноз или уже начавшийся — оставляем
                    result.append(old)

        # 2. Добавляем новые матчи из Discord которых не было
        added_preds = []
        for key, new in discord_by_key.items():
            if key not in existing_by_key:
                result.append(new)
                added_preds.append(new)
                added += 1

        result.sort(key=lambda p: p.match_time)
        data[SHARED_KEY] = [p.to_dict() for p in result]
        gist_storage.write(FILE_PREDS, data)

        logger.info(f"sync_from_discord: +{added} ~{updated} -{removed}")
        return {
            "added": added, "updated": updated, "removed": removed,
            "added_preds": added_preds,   # сами добавленные прогнозы для уведомления
        }


def update_prediction(chat_id: int = None, pred_id: str = None, **kwargs):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        for p in preds:
            if p.id == pred_id:
                for k, v in kwargs.items():
                    setattr(p, k, v)
        data[SHARED_KEY] = [p.to_dict() for p in preds]
        gist_storage.write(FILE_PREDS, data)


def delete_prediction(chat_id: int = None, pred_id: str = None) -> bool:
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(SHARED_KEY, [])]
        new_preds = [p for p in preds if p.id != pred_id]
        if len(new_preds) == len(preds):
            return False
        data[SHARED_KEY] = [p.to_dict() for p in new_preds]
        gist_storage.write(FILE_PREDS, data)
        return True


def clear_predictions(chat_id: int = None):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        data[SHARED_KEY] = []
        gist_storage.write(FILE_PREDS, data)


def get_all_recipient_chat_ids() -> list[int]:
    """
    Возвращает chat_id ВСЕХ авторизованных не-забаненных пользователей —
    им нужно слать уведомления о прогнозах.
    """
    import auth
    users = auth.list_users()
    chat_ids = [u.user_id for u in users if not u.banned]
    # Админы тоже получают уведомления, даже если их нет в whitelist
    for admin_id in config.ADMIN_CHAT_IDS:
        if admin_id not in chat_ids:
            chat_ids.append(admin_id)
    return chat_ids


# Совместимость со старым кодом
def get_all_chat_ids() -> list[int]:
    return get_all_recipient_chat_ids()


# ── Settings (индивидуальные у каждого) ──────────────────────────────────────

def load_settings(chat_id: int) -> UserSettings:
    data = gist_storage.read(FILE_SETTINGS)
    raw = data.get(str(chat_id))
    if raw:
        return UserSettings.from_dict(raw)
    return UserSettings(chat_id=chat_id, timezone_offset=config.DEFAULT_TZ_OFFSET)


def save_settings(settings: UserSettings):
    with _lock:
        data = gist_storage.read(FILE_SETTINGS)
        data[str(settings.chat_id)] = settings.to_dict()
        gist_storage.write(FILE_SETTINGS, data)
