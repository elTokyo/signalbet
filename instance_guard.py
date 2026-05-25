"""
Защита от запуска нескольких инстансов бота одновременно.

Каждый инстанс при старте генерирует уникальный ID и периодически
пишет heartbeat в Gist. Если видит свежий heartbeat от ДРУГОГО инстанса —
пишет громкое предупреждение в логи (две копии = дубли уведомлений).

Это не останавливает второй инстанс жёстко (чтобы не было ложных срабатываний
при переплыве деплоев Railway), но делает проблему сразу видимой в логах.
"""
import os
import time
import uuid
import logging
import threading
from datetime import datetime, timezone

import gist_storage

logger = logging.getLogger(__name__)

# Уникальный ID этого процесса
INSTANCE_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# Как часто обновлять heartbeat (секунды)
HEARTBEAT_INTERVAL = 30

# Если чужой heartbeat свежее этого порога — считаем что инстанс активен
STALE_THRESHOLD = 90

_stop = threading.Event()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def check_other_instances() -> bool:
    """
    Проверяет есть ли ДРУГОЙ активный инстанс.
    Возвращает True если обнаружен чужой свежий heartbeat.
    """
    try:
        hb = gist_storage.read_heartbeat()
    except Exception as e:
        logger.warning(f"Heartbeat read failed: {e}")
        return False

    other_id = hb.get("instance_id")
    other_ts = hb.get("ts", 0)

    if not other_id or other_id == INSTANCE_ID:
        return False

    age = _now_ts() - other_ts
    if age < STALE_THRESHOLD:
        logger.warning(
            f"⚠️⚠️⚠️ ОБНАРУЖЕН ДРУГОЙ ИНСТАНС БОТА! "
            f"id={other_id}, обновлялся {age:.0f}с назад. "
            f"Это вызывает ДУБЛИ уведомлений! "
            f"Проверь Railway Deployments и второй проект с тем же BOT_TOKEN."
        )
        return True
    return False


def _heartbeat_loop():
    """Фоновый цикл: пишет свой heartbeat и проверяет чужие."""
    while not _stop.is_set():
        try:
            # Сначала проверяем чужие
            check_other_instances()
            # Потом пишем свой
            gist_storage.write_heartbeat({
                "instance_id": INSTANCE_ID,
                "ts": _now_ts(),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        except Exception as e:
            logger.warning(f"Heartbeat loop error: {e}")
        _stop.wait(HEARTBEAT_INTERVAL)


def start():
    """Запускает heartbeat в фоновом потоке. Вызывать один раз при старте бота."""
    logger.info(f"Instance guard запущен. ID этого инстанса: {INSTANCE_ID}")
    # Первая проверка сразу при старте
    if check_other_instances():
        logger.warning("При старте уже обнаружен другой активный инстанс!")
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()


def stop():
    _stop.set()
