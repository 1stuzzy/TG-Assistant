"""Журнал действий модели и сохранённые диалоги агента."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

KIND_LABELS = {
    "incoming": "Входящее",
    "skip": "Пропуск",
    "delay": "Пауза",
    "read": "Прочитала",
    "history": "История чата",
    "prompt": "Собрала промт",
    "generate": "Генерирует ответ",
    "action": "Статус в Telegram",
    "typing": "Печатает",
    "rewrite": "Пересборка",
    "reply": "Готовый текст",
    "send": "Отправила в Telegram",
    "error": "Ошибка",
    "fallback": "Запасной ответ",
    "playground": "Тест в панели",
    "load": "Загрузка модели",
    "nudge": "Сама написала",
}

_MAX_EVENTS = 600
_MAX_CHATS = 40
_MAX_MSGS = 220
_TYPING_EVERY = 0.45


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ModelTrace:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path or (settings.accounts_file.parent / "agent_dialogs.json"))
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._dialogs: dict[str, dict[str, Any]] = {}
        self._live: dict[str, str] = {}
        self._last_typing: dict[str, float] = {}
        self._queues: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._load()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def event(
        self,
        kind: str,
        *,
        account_id: str = "",
        chat_id: str = "",
        peer: str = "",
        detail: str = "",
        extra: Optional[dict] = None,
    ) -> None:
        row = {
            "ts": _now_clock(),
            "iso": _now_iso(),
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind),
            "account_id": account_id or "",
            "chat_id": str(chat_id or ""),
            "peer": (peer or "")[:80],
            "detail": (detail or "")[:8000],
            "extra": extra or {},
        }
        with self._lock:
            self._events.append(row)
            queues = list(self._queues)
        self._broadcast(row, queues)

    def set_live(
        self,
        key: str,
        text: str,
        *,
        emit: bool = False,
        peer: str = "",
        chat_id: str = "",
    ) -> None:
        text = (text or "")[:800]
        with self._lock:
            self._live[str(key)] = text
        if not emit:
            return
        now = time.monotonic()
        last = self._last_typing.get(str(key), 0.0)
        if text and now - last < _TYPING_EVERY:
            return
        self._last_typing[str(key)] = now
        if text:
            self.event("typing", account_id=str(key), chat_id=chat_id, peer=peer, detail=text)

    def live_text(self, key: str) -> str:
        with self._lock:
            return self._live.get(str(key), "")

    def add_message(
        self,
        account_id: str,
        chat_id: int | str,
        peer: str,
        role: str,
        content: str,
    ) -> None:
        if not account_id or not content:
            return
        chat_key = str(chat_id)
        msg = {
            "role": role,
            "content": (content or "").strip()[:4000],
            "ts": _now_iso(),
        }
        with self._lock:
            acc = self._dialogs.setdefault(account_id, {"chats": {}})
            chats = acc.setdefault("chats", {})
            chat = chats.get(chat_key)
            if chat is None:
                if len(chats) >= _MAX_CHATS:
                    oldest = next(iter(chats))
                    chats.pop(oldest, None)
                chat = {"peer": peer or chat_key, "messages": []}
                chats[chat_key] = chat
            if peer:
                chat["peer"] = peer
            chat["messages"].append(msg)
            if len(chat["messages"]) > _MAX_MSGS:
                chat["messages"] = chat["messages"][-_MAX_MSGS:]
            chat["updated"] = _now_iso()
        self._save()

    def dialogs_for(self, account_id: str) -> list[dict]:
        with self._lock:
            acc = self._dialogs.get(account_id) or {}
            chats = acc.get("chats") or {}
            out = []
            for chat_id, chat in chats.items():
                out.append(
                    {
                        "chat_id": chat_id,
                        "peer": chat.get("peer") or chat_id,
                        "updated": chat.get("updated") or "",
                        "messages": list(chat.get("messages") or []),
                    }
                )
        out.sort(key=lambda c: c.get("updated") or "", reverse=True)
        return out

    def events(self, account_id: str = "") -> list[dict]:
        with self._lock:
            rows = list(self._events)
        if account_id:
            rows = [r for r in rows if r.get("account_id") == account_id]
        return rows

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        with self._lock:
            self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues = [q for q in self._queues if q is not queue]

    def _broadcast(self, row: dict, queues: list[asyncio.Queue]) -> None:
        loop = self._loop
        if loop is None or not queues:
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, row)
            except Exception:
                pass

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._dialogs = data
        except Exception:
            logger.debug("Не прочитана история диалогов", exc_info=True)
            self._dialogs = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with self._lock:
                payload = json.dumps(self._dialogs, ensure_ascii=False)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            logger.debug("Не сохранена история диалогов", exc_info=True)


model_trace = ModelTrace()
