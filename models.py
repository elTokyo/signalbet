from dataclasses import dataclass, field
from datetime import datetime
import uuid


@dataclass
class Prediction:
    text: str               # полная строка прогноза as-is
    match_time: datetime    # время матча в UTC (naive)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    notified_30: bool = False
    notified_5: bool = False
    source: str = "manual"  # "manual" | "discord"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "match_time": self.match_time.isoformat(),
            "notified_30": self.notified_30,
            "notified_5": self.notified_5,
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
            source=d.get("source", "manual"),
        )


@dataclass
class UserSettings:
    chat_id: int
    timezone_offset: int = 3   # UTC+3 (Москва) по умолчанию

    def to_dict(self) -> dict:
        return {"chat_id": self.chat_id, "timezone_offset": self.timezone_offset}

    @classmethod
    def from_dict(cls, d: dict) -> "UserSettings":
        return cls(chat_id=d["chat_id"], timezone_offset=d.get("timezone_offset", 3))
