"""
AccountStore отвечает только за персистентность метаданных аккаунтов
(JSON-файл на диске). Ничего не знает о Telegram/Telethon — это чистый
репозиторий, что позволяет при желании заменить его на БД без изменений
в TelegramAccountManager.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List, Optional

from app.models.account import Account


class AccountStore:
    def __init__(self, file_path: Path):
        self._file_path = file_path
        self._lock = threading.Lock()
        if not self._file_path.exists():
            self._write_all([])

    # ---------- низкоуровневые операции с файлом ----------

    def _read_all(self) -> List[dict]:
        with self._lock:
            if not self._file_path.exists():
                return []
            with open(self._file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else []

    def _write_all(self, items: List[dict]) -> None:
        with self._lock:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)

    # ---------- публичное API ----------

    def list(self) -> List[Account]:
        return [Account.from_dict(d) for d in self._read_all()]

    def get(self, account_id: str) -> Optional[Account]:
        return next((a for a in self.list() if a.id == account_id), None)

    def get_by_phone(self, phone: str) -> Optional[Account]:
        return next((a for a in self.list() if a.phone == phone), None)

    def list_by_tenant(self, tenant_id: str) -> List[Account]:
        return [a for a in self.list() if a.tenant_id == tenant_id]

    def add(self, account: Account) -> Account:
        items = self._read_all()
        items.append(account.to_dict())
        self._write_all(items)
        return account

    def update(self, account: Account) -> Account:
        items = self._read_all()
        items = [account.to_dict() if i["id"] == account.id else i for i in items]
        self._write_all(items)
        return account

    def delete(self, account_id: str) -> bool:
        items = self._read_all()
        remaining = [i for i in items if i["id"] != account_id]
        changed = len(remaining) != len(items)
        if changed:
            self._write_all(remaining)
        return changed
