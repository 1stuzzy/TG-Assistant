"""
Доменная модель Telegram-аккаунта.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CHECKING = "checking"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Account:
    """Одна запись в списке управляемых аккаунтов."""

    phone: str
    name: str = "Новый аккаунт"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: AccountStatus = AccountStatus.INACTIVE
    session_file: str = ""
    last_check: str = field(default_factory=_now_iso)
    created_at: str = field(default_factory=_now_iso)
    tenant_id: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = (
            self.status.value if isinstance(self.status, AccountStatus) else self.status
        )
        return data

    @staticmethod
    def from_dict(data: dict) -> "Account":
        data = dict(data)
        data["status"] = AccountStatus(data.get("status", "inactive"))
        data["tenant_id"] = data.get("tenant_id") or ""
        allowed = set(Account.__dataclass_fields__)
        return Account(**{k: v for k, v in data.items() if k in allowed})
