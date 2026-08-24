"""
Аренда панелей: SQLite с арендаторами, логинами, сессиями и слотами чатов.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request, WebSocket

POWER_PRESETS = {
    "low": {"max_accounts": 2, "max_chats": 8, "max_agents": 1},
    "medium": {"max_accounts": 5, "max_chats": 25, "max_agents": 3},
    "high": {"max_accounts": 15, "max_chats": 80, "max_agents": 8},
}

DEFAULT_READ_DELAY_MS = 800
DEFAULT_REPLY_DELAY_MS = 1500
MAX_DELAY_MS = 60_000


def clamp_delay_ms(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, MAX_DELAY_MS))

PBKDF2_ITERS = 120_000
TOKEN_DAYS = 14
COOKIE = "tg_session"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERS).hex()
    return f"pbkdf2${PBKDF2_ITERS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(check, digest)


class RentalStore:
    def __init__(self, db_path: Path):
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    power TEXT NOT NULL DEFAULT 'medium',
                    max_accounts INTEGER NOT NULL DEFAULT 5,
                    max_chats INTEGER NOT NULL DEFAULT 25,
                    max_agents INTEGER NOT NULL DEFAULT 3,
                    model_name TEXT DEFAULT '',
                    engine TEXT NOT NULL DEFAULT 'local',
                    worker_id TEXT DEFAULT '',
                    note TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    read_delay_ms INTEGER NOT NULL DEFAULT 800,
                    reply_delay_ms INTEGER NOT NULL DEFAULT 1500
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    login TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    tenant_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            cols = {row["name"] for row in db.execute("PRAGMA table_info(tenants)").fetchall()}
            if "read_delay_ms" not in cols:
                db.execute(
                    "ALTER TABLE tenants ADD COLUMN read_delay_ms INTEGER NOT NULL DEFAULT 800"
                )
            if "reply_delay_ms" not in cols:
                db.execute(
                    "ALTER TABLE tenants ADD COLUMN reply_delay_ms INTEGER NOT NULL DEFAULT 1500"
                )
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, chat_id)
                );
                """
            )

    def ensure_admin(self, login: str, password: str) -> None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
            if row:
                return
            db.execute(
                "INSERT INTO users (id, login, password_hash, role, tenant_id, created_at) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), login.strip(), hash_password(password), "admin", "", _now()),
            )

    def login(self, login: str, password: str) -> Optional[tuple[dict, str]]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE login=?", (login.strip(),)).fetchone()
            if not row or not verify_password(password, row["password_hash"]):
                return None
            user = dict(row)
            if user["role"] == "tenant":
                tenant = db.execute("SELECT * FROM tenants WHERE id=?", (user["tenant_id"],)).fetchone()
                if not tenant:
                    return None
                if tenant["status"] == "revoked":
                    return None
                if tenant["status"] == "suspended":
                    raise PermissionError("Панель приостановлена")
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)).isoformat()
            db.execute(
                "INSERT INTO tokens (token, user_id, expires_at) VALUES (?,?,?)",
                (token, user["id"], expires),
            )
            return self._public_user(user), token

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM tokens WHERE token=?", (token,))

    def user_from_request(self, request: Request | WebSocket) -> Optional[dict]:
        token = request.cookies.get(COOKIE) or ""
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        if request.query_params.get("token"):
            token = request.query_params.get("token") or token
        return self.user_by_token(token)

    def user_by_token(self, token: str) -> Optional[dict]:
        if not token:
            return None
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=? AND t.expires_at > ?",
                (token, _now()),
            ).fetchone()
            return self._public_user(dict(row)) if row else None

    def _public_user(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "login": row["login"],
            "role": row["role"],
            "tenant_id": row.get("tenant_id") or "",
        }

    def get_tenant(self, tenant_id: str) -> Optional[dict]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    def list_tenants(self) -> list[dict]:
        with self._lock, self._connect() as db:
            tenants = [dict(r) for r in db.execute("SELECT * FROM tenants ORDER BY created_at DESC")]
            users = {r["tenant_id"]: dict(r) for r in db.execute("SELECT * FROM users WHERE role='tenant'")}
        for t in tenants:
            u = users.get(t["id"]) or {}
            t["login"] = u.get("login") or ""
        return tenants

    def create_tenant(self, data: dict) -> dict:
        login = (data.get("login") or "").strip()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите название панели")
        if not login or not password:
            raise ValueError("Укажите логин и пароль арендатора")
        if len(password) < 4:
            raise ValueError("Пароль слишком короткий")
        power = (data.get("power") or "medium").strip()
        if power not in POWER_PRESETS:
            power = "medium"
        preset = POWER_PRESETS[power]
        tenant_id = str(uuid.uuid4())
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
                raise ValueError("Такой логин уже занят")
            db.execute(
                """INSERT INTO tenants
                   (id, name, status, power, max_accounts, max_chats, max_agents,
                    model_name, engine, worker_id, note, created_at,
                    read_delay_ms, reply_delay_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tenant_id,
                    name,
                    "active",
                    power,
                    int(data.get("max_accounts") or preset["max_accounts"]),
                    int(data.get("max_chats") or preset["max_chats"]),
                    int(data.get("max_agents") or preset["max_agents"]),
                    (data.get("model_name") or "").strip(),
                    (data.get("engine") or "local").strip() or "local",
                    (data.get("worker_id") or "").strip(),
                    (data.get("note") or "").strip(),
                    _now(),
                    clamp_delay_ms(data.get("read_delay_ms"), DEFAULT_READ_DELAY_MS),
                    clamp_delay_ms(data.get("reply_delay_ms"), DEFAULT_REPLY_DELAY_MS),
                ),
            )
            db.execute(
                "INSERT INTO users (id, login, password_hash, role, tenant_id, created_at) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), login, hash_password(password), "tenant", tenant_id, _now()),
            )
        tenant = self.get_tenant(tenant_id)
        tenant["login"] = login
        return tenant

    def update_tenant(self, tenant_id: str, data: dict) -> dict:
        tenant = self.get_tenant(tenant_id)
        if not tenant:
            raise ValueError("Панель не найдена")
        fields = {
            "name",
            "status",
            "power",
            "max_accounts",
            "max_chats",
            "max_agents",
            "model_name",
            "engine",
            "worker_id",
            "note",
            "read_delay_ms",
            "reply_delay_ms",
        }
        if data.get("power") in POWER_PRESETS and not any(k in data for k in ("max_accounts", "max_chats", "max_agents")):
            data = {**POWER_PRESETS[data["power"]], **data}
        if data.get("status") and data["status"] not in {"active", "suspended", "revoked"}:
            raise ValueError("Некорректный статус")
        sets = []
        vals = []
        for key in fields:
            if key in data and data[key] is not None:
                sets.append(f"{key}=?")
                val = data[key]
                if key in {"max_accounts", "max_chats", "max_agents"}:
                    val = int(val)
                if key in {"read_delay_ms", "reply_delay_ms"}:
                    val = clamp_delay_ms(
                        val,
                        DEFAULT_READ_DELAY_MS if key == "read_delay_ms" else DEFAULT_REPLY_DELAY_MS,
                    )
                sets[-1] = f"{key}=?"
                vals.append(val)
        if not sets and not data.get("password") and not data.get("login"):
            return {**tenant, "login": self._tenant_login(tenant_id)}
        with self._lock, self._connect() as db:
            if sets:
                vals.append(tenant_id)
                db.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id=?", vals)
            if data.get("login"):
                login = data["login"].strip()
                clash = db.execute(
                    "SELECT id FROM users WHERE login=? AND tenant_id!=?",
                    (login, tenant_id),
                ).fetchone()
                if clash:
                    raise ValueError("Такой логин уже занят")
                db.execute("UPDATE users SET login=? WHERE tenant_id=? AND role='tenant'", (login, tenant_id))
            if data.get("password"):
                if len(str(data["password"])) < 4:
                    raise ValueError("Пароль слишком короткий")
                db.execute(
                    "UPDATE users SET password_hash=? WHERE tenant_id=? AND role='tenant'",
                    (hash_password(str(data["password"])), tenant_id),
                )
                db.execute(
                    "DELETE FROM tokens WHERE user_id IN (SELECT id FROM users WHERE tenant_id=?)",
                    (tenant_id,),
                )
        tenant = self.get_tenant(tenant_id)
        tenant["login"] = self._tenant_login(tenant_id)
        return tenant

    def _tenant_login(self, tenant_id: str) -> str:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT login FROM users WHERE tenant_id=? AND role='tenant'", (tenant_id,)
            ).fetchone()
            return row["login"] if row else ""

    def set_access(self, tenant_id: str, granted: bool) -> dict:
        status = "active" if granted else "revoked"
        return self.update_tenant(tenant_id, {"status": status})

    def drop_tenant_tokens(self, tenant_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM tokens WHERE user_id IN (SELECT id FROM users WHERE tenant_id=?)",
                (tenant_id,),
            )

    def allow_chat(self, tenant_id: str, account_id: str, chat_id: str, max_chats: int) -> bool:
        if not tenant_id:
            return True
        if max_chats <= 0:
            return True
        chat_id = str(chat_id)
        with self._lock, self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM chats WHERE account_id=? AND chat_id=?",
                (account_id, chat_id),
            ).fetchone()
            if exists:
                return True
            count = db.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE account_id=?", (account_id,)
            ).fetchone()["n"]
            if count >= max_chats:
                return False
            db.execute(
                "INSERT INTO chats (account_id, chat_id, tenant_id, created_at) VALUES (?,?,?,?)",
                (account_id, chat_id, tenant_id, _now()),
            )
            return True

    def chats_used(self, account_id: str) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE account_id=?", (account_id,)
            ).fetchone()
            return int(row["n"] if row else 0)

    def chats_used_tenant(self, tenant_id: str) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            return int(row["n"] if row else 0)
