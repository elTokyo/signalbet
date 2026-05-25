"""
Хранилище через GitHub Gist API.
Кэш с TTL: чтения быстрые, но устаревают через CACHE_TTL секунд.
Записи всегда инвалидируют кэш.
"""
import json
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

FILE_USERS       = "users.json"
FILE_PREDICTIONS = "predictions.json"
FILE_SETTINGS    = "settings.json"
FILE_HEARTBEAT   = "heartbeat.json"

# Кэш живёт 10 секунд. За это время делается обычно <50 операций → 6 запросов/мин к Gist
CACHE_TTL = 10

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_cache_time: dict[str, float] = {}


def _ensure_setup():
    if not GITHUB_TOKEN or not GIST_ID:
        raise RuntimeError("GITHUB_TOKEN или GIST_ID не заданы")


def _fetch_gist() -> dict:
    """Скачивает все файлы Gist."""
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
                        logger.error(f"Gist file {name} corrupted")
                        result[name] = {}
                return result
            elif r.status_code == 404:
                logger.error(f"Gist {GIST_ID} не найден")
                return {}
            elif r.status_code in (401, 403):
                logger.error(f"GitHub auth error: {r.status_code}")
                return {}
            else:
                logger.warning(f"Gist fetch attempt {attempt+1}: {r.status_code}")
        except requests.RequestException as e:
            logger.warning(f"Gist fetch error: {e}")
        time.sleep(1 + attempt)
    return {}


def _push_files(updates: dict[str, dict]):
    """Загружает обновлённые файлы в Gist."""
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
                logger.info(f"Gist обновлён: {list(updates.keys())}")
                return True
            logger.warning(f"Gist push {attempt+1}: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Gist push error: {e}")
        time.sleep(1 + attempt)
    logger.error("Не удалось записать в Gist!")
    return False


def read(filename: str) -> dict:
    """
    Читает данные конкретного файла из Gist.
    Использует кэш с TTL=10сек — баланс между скоростью и свежестью.
    """
    with _lock:
        now = time.time()
        last_load = _cache_time.get(filename, 0)

        if filename in _cache and (now - last_load) < CACHE_TTL:
            return dict(_cache[filename])  # копия чтобы не мутировали

        # Кэш протух или пустой — перезагружаем все файлы одним запросом
        files = _fetch_gist()
        _cache[FILE_USERS]       = files.get(FILE_USERS, {}) or {}
        _cache[FILE_PREDICTIONS] = files.get(FILE_PREDICTIONS, {}) or {}
        _cache[FILE_SETTINGS]    = files.get(FILE_SETTINGS, {}) or {}

        now = time.time()
        _cache_time[FILE_USERS]       = now
        _cache_time[FILE_PREDICTIONS] = now
        _cache_time[FILE_SETTINGS]    = now

        return dict(_cache.get(filename, {}))


def write(filename: str, data: dict):
    """
    Записывает данные. ВАЖНО: после записи инвалидирует кэш у всех инстансов
    (через TTL=0) — следующее чтение в любом месте подтянет свежие данные.
    """
    with _lock:
        _push_files({filename: data})
        # Обновляем локальный кэш
        _cache[filename] = data
        _cache_time[filename] = time.time()


def invalidate_cache():
    """Принудительно сбросить кэш — чтобы следующее чтение пошло в Gist."""
    with _lock:
        _cache_time.clear()
        _cache.clear()


# ── Heartbeat (детект нескольких инстансов) ──────────────────────────────────

def read_heartbeat() -> dict:
    """Читает heartbeat напрямую из Gist (без общего кэша файлов)."""
    files = _fetch_gist()
    return files.get(FILE_HEARTBEAT, {}) or {}


def write_heartbeat(data: dict):
    """Пишет heartbeat в Gist напрямую."""
    _push_files({FILE_HEARTBEAT: data})
