"""
Авторизация через GitHub Gist.
После любой мутации (authorize/remove/ban) кэш инвалидируется
чтобы все процессы при следующем чтении увидели свежие данные.
"""
import logging
import threading
from datetime import datetime
from dataclasses import dataclass

import config
import gist_storage

logger = logging.getLogger(__name__)

FILE = gist_storage.FILE_USERS
_lock = threading.RLock()


@dataclass
class AuthorizedUser:
    user_id: int
    username: str
    first_name: str
    authorized_at: str
    banned: bool = False

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "authorized_at": self.authorized_at,
            "banned": self.banned,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuthorizedUser":
        uid = d.get("user_id") or d.get("chat_id")
        return cls(
            user_id=int(uid),
            username=d.get("username", ""),
            first_name=d.get("first_name", ""),
            authorized_at=d.get("authorized_at", ""),
            banned=d.get("banned", False),
        )


def is_authorized(user_id: int) -> bool:
    """
    Админы всегда авторизованы.
    Для остальных — читаем из Gist (через кэш ~10 сек, чтобы не перегружать API).
    После /remove пользователь блокируется в течение ~10 секунд (приемлемо).
    """
    if user_id in config.ADMIN_CHAT_IDS:
        return True
    # Через кэш — снижает нагрузку на Gist API (раньше invalidate на каждое сообщение
    # упирался в rate limit). Задержка применения /remove до 10 сек некритична.
    data = gist_storage.read(FILE)
    user = data.get(str(user_id))
    if not user:
        return False
    return not user.get("banned", False)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_CHAT_IDS


def authorize(user_id: int, username: str, first_name: str) -> AuthorizedUser:
    with _lock:
        # Принудительно перечитываем из Gist — на случай если данные изменили другие процессы
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE)

        existing = data.get(str(user_id))
        authorized_at = (
            existing.get("authorized_at") if existing
            else datetime.utcnow().isoformat(timespec="seconds")
        )
        user = AuthorizedUser(
            user_id=user_id,
            username=username or "",
            first_name=first_name or "",
            authorized_at=authorized_at,
        )
        data[str(user_id)] = user.to_dict()
        gist_storage.write(FILE, data)
        logger.info(f"AUTHORIZED: {user_id} (@{username}). Всего юзеров: {len(data)}")
        return user


def list_users() -> list[AuthorizedUser]:
    # Всегда свежие данные при запросе списка
    gist_storage.invalidate_cache()
    data = gist_storage.read(FILE)
    return [AuthorizedUser.from_dict(u) for u in data.values()]


def remove_user(user_id: int) -> bool:
    with _lock:
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE)
        if str(user_id) not in data:
            logger.warning(f"REMOVE: {user_id} не найден в Gist (есть {list(data.keys())})")
            return False
        del data[str(user_id)]
        gist_storage.write(FILE, data)
        # Дополнительная инвалидация чтобы другие процессы не использовали свой кэш
        gist_storage.invalidate_cache()
        logger.info(f"REMOVED: {user_id}. Осталось юзеров: {len(data)}")
        return True


def set_banned(user_id: int, banned: bool) -> bool:
    with _lock:
        gist_storage.invalidate_cache()
        data = gist_storage.read(FILE)
        if str(user_id) not in data:
            logger.warning(f"BAN: {user_id} не найден")
            return False
        data[str(user_id)]["banned"] = banned
        gist_storage.write(FILE, data)
        gist_storage.invalidate_cache()
        logger.info(f"BAN={banned}: {user_id}")
        return True
