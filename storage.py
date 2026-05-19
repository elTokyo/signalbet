"""
Хранилище прогнозов и настроек через GitHub Gist.
"""
import threading
import logging
from models import Prediction, UserSettings
import config
import gist_storage

logger = logging.getLogger(__name__)

FILE_PREDS    = gist_storage.FILE_PREDICTIONS
FILE_SETTINGS = gist_storage.FILE_SETTINGS

_lock = threading.RLock()


# ── Predictions ──────────────────────────────────────────────────────────────

def load_predictions(chat_id: int) -> list[Prediction]:
    data = gist_storage.read(FILE_PREDS)
    return [Prediction.from_dict(p) for p in data.get(str(chat_id), [])]


def save_predictions(chat_id: int, predictions: list[Prediction]):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        predictions = sorted(predictions, key=lambda p: p.match_time)
        data[str(chat_id)] = [p.to_dict() for p in predictions]
        gist_storage.write(FILE_PREDS, data)


def add_predictions(chat_id: int, new_preds: list[Prediction]) -> int:
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        existing = [Prediction.from_dict(p) for p in data.get(str(chat_id), [])]
        existing_texts = {p.text.strip().lower() for p in existing}

        added = []
        for p in new_preds:
            if p.text.strip().lower() not in existing_texts:
                added.append(p)
                existing_texts.add(p.text.strip().lower())

        if added:
            all_preds = sorted(existing + added, key=lambda p: p.match_time)
            data[str(chat_id)] = [p.to_dict() for p in all_preds]
            gist_storage.write(FILE_PREDS, data)
        return len(added)


def update_prediction(chat_id: int, pred_id: str, **kwargs):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(str(chat_id), [])]
        for p in preds:
            if p.id == pred_id:
                for k, v in kwargs.items():
                    setattr(p, k, v)
        data[str(chat_id)] = [p.to_dict() for p in preds]
        gist_storage.write(FILE_PREDS, data)


def delete_prediction(chat_id: int, pred_id: str) -> bool:
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        preds = [Prediction.from_dict(p) for p in data.get(str(chat_id), [])]
        new_preds = [p for p in preds if p.id != pred_id]
        if len(new_preds) == len(preds):
            return False
        data[str(chat_id)] = [p.to_dict() for p in new_preds]
        gist_storage.write(FILE_PREDS, data)
        return True


def clear_predictions(chat_id: int):
    with _lock:
        data = gist_storage.read(FILE_PREDS)
        data[str(chat_id)] = []
        gist_storage.write(FILE_PREDS, data)


def get_all_chat_ids() -> list[int]:
    data = gist_storage.read(FILE_PREDS)
    return [int(k) for k in data.keys()]


# ── Settings ─────────────────────────────────────────────────────────────────

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
