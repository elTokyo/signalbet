from dataclasses import dataclass, field
from datetime import datetime
import uuid


@dataclass
class Prediction:
    text: str
    match_time: datetime
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    notified_30: bool = False
    notified_5: bool = False
    # Fonbet
    fonbet_notified_prematch: bool = False
    fonbet_notified_live: bool = False
    crooked_notified: bool = False
    source: str = "manual"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "match_time": self.match_time.isoformat(),
            "notified_30": self.notified_30,
            "notified_5": self.notified_5,
            "fonbet_notified_prematch": self.fonbet_notified_prematch,
            "fonbet_notified_live": self.fonbet_notified_live,
            "crooked_notified": self.crooked_notified,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Prediction":
        return cls(
            id=d["id"],
            text=d["text"],
            match_time=datetime.fromisoformat(d["match_time"]),
            notified_30=d.get("notified_30", False),
            notified_5=d.get("notified_5", False),
            fonbet_notified_prematch=d.get("fonbet_notified_prematch", False),
            fonbet_notified_live=d.get("fonbet_notified_live", False),
            crooked_notified=d.get("crooked_notified", False),
            source=d.get("source", "manual"),
        )


@dataclass
class UserSettings:
    chat_id: int
    timezone_offset: int = 3
    # Категории уведомлений (все включены по умолчанию)
    notify_reminders: bool = True        # напоминания за 30/5 мин
    notify_match_out: bool = True        # выход матча в прематч/лайв (Фонбет)
    notify_crooked: bool = True          # кривые матчи (value)
    notify_new_preds: bool = True        # новые прогнозы из Discord
    # Legacy-поле (раньше отвечало за прематч+лайв+кривые). Оставляем для миграции.
    fonbet_notifications: bool = True

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "timezone_offset": self.timezone_offset,
            "notify_reminders": self.notify_reminders,
            "notify_match_out": self.notify_match_out,
            "notify_crooked": self.notify_crooked,
            "notify_new_preds": self.notify_new_preds,
            "fonbet_notifications": self.fonbet_notifications,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserSettings":
        # Миграция: если новых полей нет, наследуем от старого fonbet_notifications
        legacy_fonbet = d.get("fonbet_notifications", True)
        return cls(
            chat_id=d["chat_id"],
            timezone_offset=d.get("timezone_offset", 3),
            notify_reminders=d.get("notify_reminders", True),
            notify_match_out=d.get("notify_match_out", legacy_fonbet),
            notify_crooked=d.get("notify_crooked", legacy_fonbet),
            notify_new_preds=d.get("notify_new_preds", True),
            fonbet_notifications=legacy_fonbet,
        )
