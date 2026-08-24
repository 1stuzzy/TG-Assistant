"""
TelegramAccountManager инкапсулирует реальную работу с Telegram API
через библиотеку Telethon: запрос кода, подтверждение кода, 2FA,
проверку состояния сессии и удаление (logout) аккаунта.

AccountStore используется только для хранения метаданных (номер, имя,
статус, путь к файлу сессии) — сама сессия Telegram живёт в .session
файле Telethon и здесь не дублируется.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

from telethon import TelegramClient
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from app.config import settings
from app.models.account import Account, AccountStatus
from app.services.account_store import AccountStore


@dataclass
class PendingLogin:
    """Состояние одного незавершённого входа: номер -> код -> (2FA)."""

    login_id: str
    phone: str
    client: TelegramClient
    phone_code_hash: str
    tenant_id: str = ""


class TelegramAccountManager:
    def __init__(self, store: AccountStore):
        self._store = store
        self._pending: Dict[str, PendingLogin] = {}

    # ---------------------------------------------------------------
    # Шаг 1 — запросить код на номер телефона
    # ---------------------------------------------------------------
    async def start_login(self, login_id: str, phone: str, tenant_id: str = "") -> None:
        session_path = self._session_path(phone)
        client = self._new_client(session_path)
        await client.connect()

        if await client.is_user_authorized():
            # Сессия для этого номера уже существует и валидна —
            # просто зафиксируем аккаунт как активный.
            await self._register_existing(client, phone, session_path, tenant_id)
            await client.disconnect()
            raise ValueError("Этот номер уже подключён и авторизован")

        sent = await client.send_code_request(phone)
        self._pending[login_id] = PendingLogin(
            login_id=login_id,
            phone=phone,
            client=client,
            phone_code_hash=sent.phone_code_hash,
            tenant_id=tenant_id,
        )

    # ---------------------------------------------------------------
    # Шаг 2 — подтвердить код из Telegram
    # ---------------------------------------------------------------
    async def confirm_code(self, login_id: str, code: str) -> str:
        """Возвращает "need_2fa" или "success"."""
        pending = self._require_pending(login_id)
        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=code,
                phone_code_hash=pending.phone_code_hash,
            )
        except SessionPasswordNeededError:
            return "need_2fa"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            raise ValueError("Неверный или истёкший код подтверждения") from exc

        await self._finalize_login(pending)
        return "success"

    # ---------------------------------------------------------------
    # Шаг 3 (опционально) — облачный пароль 2FA
    # ---------------------------------------------------------------
    async def confirm_password(self, login_id: str, password: str) -> None:
        pending = self._require_pending(login_id)
        try:
            await pending.client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise ValueError("Неверный облачный пароль") from exc
        await self._finalize_login(pending)

    # ---------------------------------------------------------------
    # Проверка состояния и удаление уже добавленного аккаунта
    # ---------------------------------------------------------------
    async def check_status(self, account_id: str) -> Account:
        account = self._require_account(account_id)
        client = self._new_client(Path(account.session_file))
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            account.status = AccountStatus.ACTIVE if authorized else AccountStatus.INACTIVE
        except Exception:
            account.status = AccountStatus.INACTIVE
        finally:
            if client.is_connected():
                await client.disconnect()

        account.last_check = datetime.now(timezone.utc).isoformat()
        self._store.update(account)
        return account

    async def delete_account(self, account_id: str) -> None:
        account = self._require_account(account_id)
        client = self._new_client(Path(account.session_file))
        try:
            await client.connect()
            if await client.is_user_authorized():
                await client.log_out()  # реально завершает сессию на стороне Telegram
        except Exception:
            pass
        finally:
            if client.is_connected():
                await client.disconnect()

        session_path = Path(account.session_file)
        for suffix in ("", "-journal"):
            p = Path(str(session_path) + suffix)
            if p.exists():
                p.unlink()

        self._store.delete(account_id)

    def list_accounts(self) -> list[Account]:
        return self._store.list()

    # ---------------------------------------------------------------
    # Внутренние помощники
    # ---------------------------------------------------------------
    def _session_path(self, phone: str) -> Path:
        safe = "".join(ch for ch in phone if ch.isdigit())
        return settings.sessions_dir / f"{safe}.session"

    def _new_client(self, session_path: Path) -> TelegramClient:
        return TelegramClient(str(session_path), settings.api_id, settings.api_hash)

    async def _finalize_login(self, pending: PendingLogin) -> Account:
        me = await pending.client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or (
            f"@{me.username}" if me.username else pending.phone
        )
        account = Account(
            phone=pending.phone,
            name=name,
            status=AccountStatus.ACTIVE,
            session_file=str(self._session_path(pending.phone)),
            tenant_id=pending.tenant_id,
        )
        self._store.add(account)
        await pending.client.disconnect()
        del self._pending[pending.login_id]
        return account

    async def _register_existing(self, client: TelegramClient, phone: str, session_path: Path, tenant_id: str = "") -> None:
        if self._store.get_by_phone(phone):
            return
        me = await client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name])) or phone
        self._store.add(
            Account(
                phone=phone,
                name=name,
                status=AccountStatus.ACTIVE,
                session_file=str(session_path),
                tenant_id=tenant_id,
            )
        )

    def _require_pending(self, login_id: str) -> PendingLogin:
        pending = self._pending.get(login_id)
        if not pending:
            raise ValueError("Сессия входа не найдена или истекла — начните заново")
        return pending

    def _require_account(self, account_id: str) -> Account:
        account = self._store.get(account_id)
        if not account:
            raise ValueError("Аккаунт не найден")
        return account
