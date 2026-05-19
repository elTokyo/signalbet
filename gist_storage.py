"""
Хранилище через GitHub Gist API.
Все данные (users, predictions, settings) хранятся в одном приватном Gist
как три JSON-файла. Read-modify-write под локом для исключения race condition.
"""
import json
import os
import logging
import threading
import time
import requests

import config

logger = logging.getLogger(__name__)

GITHUB_TOKEN = config.GITHUB_TOKEN
GIST_ID      = config.GIST_ID

API_BASE = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Имена файлов внутри Gist
FILE_USERS       = "users.json"
FILE_PREDICTIONS = "predictions.json"
FILE_SETTINGS    = "settings.json"

# Глобальный лок (атомарность чтения-записи)
_lock = threading.RLock()

# Кэш содержимого Gist в памяти, чтобы не дёргать API на каждом чтении
_cache: dict[str, dict] = {}
_cache_loaded = False


def _ensure_setup():
    if not GITHUB_TOKEN or not GIST_ID:
        raise RuntimeError(
            "GITHUB_TOKEN или GIST_ID не заданы в переменных окружения"
        )


def _fetch_gist() -> dict:
    """Скачивает все файлы Gist и возвращает {filename: parsed_json}."""
    _ensure_setup()
    url = f"{API_BASE}/gists/{GIST_ID}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                files = r.json().get("files", {})
                result = {}
                for name, file_data in files.items():
                    content = file_data.get("content", "")
                    try:
                        result[name] = json.loads(content) if content.strip() else {}
                    except json.JSONDecodeError:
                        logger.error(f"Gist file {name} is corrupted, returning empty")
                        result[name] = {}
                return result
            elif r.status_code == 404:
                logger.error(f"Gist {GIST_ID} не найден!")
                return {}
            elif r.status_code in (401, 403):
                logger.error(f"GitHub auth error: {r.status_code} {r.text}")
                return {}
            else:
                logger.warning(f"Gist fetch attempt {attempt+1}: {r.status_code}")
        except requests.RequestException as e:
            logger.warning(f"Gist fetch attempt {attempt+1} error: {e}")
        time.sleep(1 + attempt)
    return {}


def _push_files(updates: dict[str, dict]):
    """Загружает обновлённые файлы в Gist. updates = {filename: data_dict}."""
    _ensure_setup()
    url = f"{API_BASE}/gists/{GIST_ID}"
    payload = {
        "files": {
            name: {"content": json.dumps(data, ensure_ascii=False, indent=2)}
            for name, data in updates.items()
        }
    }
    for attempt in range(3):
        try:
            r = requests.patch(url, headers=HEADERS, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            logger.warning(f"Gist push attempt {attempt+1}: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Gist push attempt {attempt+1} error: {e}")
        time.sleep(1 + attempt)
    logger.error("Не удалось записать в Gist после 3 попыток!")
    return False


def _load_cache():
    """При первом обращении подгружаем содержимое Gist в _cache."""
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    files = _fetch_gist()
    _cache = {
        FILE_USERS:       files.get(FILE_USERS, {}) or {},
        FILE_PREDICTIONS: files.get(FILE_PREDICTIONS, {}) or {},
        FILE_SETTINGS:    files.get(FILE_SETTINGS, {}) or {},
    }
    _cache_loaded = True
    logger.info(
        f"Gist загружен: users={len(_cache[FILE_USERS])}, "
        f"chats_with_preds={len(_cache[FILE_PREDICTIONS])}, "
        f"settings={len(_cache[FILE_SETTINGS])}"
    )


def read(filename: str) -> dict:
    """Читает данные определённого файла из кэша."""
    with _lock:
        _load_cache()
        return dict(_cache.get(filename, {}))   # копия чтоб не мутировали кэш


def write(filename: str, data: dict):
    """Записывает данные в кэш и пушит в Gist."""
    with _lock:
        _load_cache()
        _cache[filename] = data
        _push_files({filename: data})


def refresh_cache():
    """Принудительно перечитать Gist (на случай если данные меняли вручную)."""
    global _cache_loaded
    with _lock:
        _cache_loaded = False
        _load_cache()
