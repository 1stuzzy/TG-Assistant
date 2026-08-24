"""Кольцевой буфер логов + рассылка в WebSocket-консоль."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any, Optional


class LogHub:
    def __init__(self, maxlen: int = 800) -> None:
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._queues: list[asyncio.Queue] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def push(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._buf.append(entry)
            queues = list(self._queues)
        loop = self._loop
        if loop is None or not queues:
            return
        for queue in queues:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, entry)
            except Exception:
                pass

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buf)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        with self._lock:
            self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues = [q for q in self._queues if q is not queue]


log_hub = LogHub()


class HubLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message}: {record.exc_info[1]}"
            log_hub.push(
                {
                    "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )
        except Exception:
            pass


def attach_log_hub() -> None:
    root = logging.getLogger()
    if any(isinstance(h, HubLogHandler) for h in root.handlers):
        return
    handler = HubLogHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
