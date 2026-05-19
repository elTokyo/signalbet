"""
Авторизация через GitHub Gist (модуль gist_storage).
Ключ — user_id (TG ID пользователя).
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
    if user_id in config.ADMIN_CHAT_IDS:
        return True
    data = gist_storage.read(FILE)
    user = data.get(str(user_id))
    if not user:
        return False
    return not user.get("banned", False)


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_CHAT_IDS


def authorize(user_id: int, username: str, first_name: str) -> AuthorizedUser:
    with _lock:
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
        logger.info(f"Авторизован: {user_id} (@{username}). Всего: {len(data)}")
        return user


def list_users() -> list[AuthorizedUser]:
    data = gist_storage.read(FILE)
    return [AuthorizedUser.from_dict(u) for u in data.values()]


def remove_user(user_id: int) -> bool:
    with _lock:
        data = gist_storage.read(FILE)
        if str(user_id) in data:
            del data[str(user_id)]
            gist_storage.write(FILE, data)
            return True
        return False


def set_banned(user_id: int, banned: bool) -> bool:
    with _lock:
        data = gist_storage.read(FILE)
        if str(user_id) not in data:
            return False
        data[str(user_id)]["banned"] = banned
        gist_storage.write(FILE, data)
        return True
