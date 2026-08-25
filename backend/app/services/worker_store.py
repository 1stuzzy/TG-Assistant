"""Сохранённые удалённые ПК с моделью (инференс-воркеры)."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class WorkerStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write([])

    def list(self) -> list[dict]:
        return [self._public(w) for w in self._read()]

    def get(self, worker_id: str) -> Optional[dict]:
        return next((w for w in self._read() if w.get("id") == worker_id), None)

    def create(self, data: dict) -> dict:
        url = (data.get("url") or "").strip().rstrip("/")
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите имя компьютера")
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL должен начинаться с http:// или https://")
        item = {
            "id": str(uuid.uuid4()),
            "name": name,
            "url": url,
            "api_key": (data.get("api_key") or "").strip(),
        }
        with self._lock:
            items = self._read_unlocked()
            items.append(item)
            self._write_unlocked(items)
        return self._public(item)

    def delete(self, worker_id: str) -> None:
        with self._lock:
            items = self._read_unlocked()
            remaining = [w for w in items if w.get("id") != worker_id]
            if len(remaining) == len(items):
                raise ValueError("Воркер не найден")
            self._write_unlocked(remaining)

    async def ping(self, worker_id: str) -> dict:
        worker = self.get(worker_id)
        if not worker:
            raise ValueError("Воркер не найден")
        return await ping_url(worker["url"], worker.get("api_key") or "")

    async def wait_ready(self, worker_id: str, timeout: float = 600) -> dict:
        worker = self.get(worker_id)
        if not worker:
            raise ValueError("Воркер не найден")
        deadline = asyncio.get_running_loop().time() + timeout
        last: dict = {}
        while True:
            last = await ping_url(worker["url"], worker.get("api_key") or "")
            if last.get("error"):
                raise ValueError(
                    f"Модель на «{worker.get('name') or 'удалённом ПК'}» не загрузилась: {last['error']}"
                )
            if last.get("ok") and not last.get("loading"):
                return last
            left = int(deadline - asyncio.get_running_loop().time())
            if left <= 0:
                raise ValueError(
                    f"Модель на «{worker.get('name')}» ещё загружается. "
                    "В окне start.bat дождитесь строки «Готово» и запустите агента снова."
                )
            logger.info(
                "Ждём модель на %s, осталось ~%s сек",
                worker.get("name"),
                left,
            )
            await asyncio.sleep(3)

    def _public(self, worker: dict) -> dict:
        return {
            "id": worker.get("id"),
            "name": worker.get("name"),
            "url": worker.get("url"),
            "has_key": bool(worker.get("api_key")),
        }

    def _read(self) -> list[dict]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> list[dict]:
        if not self._path.exists():
            return []
        raw = self._path.read_text(encoding="utf-8").strip()
        return json.loads(raw) if raw else []

    def _write(self, items: list[dict]) -> None:
        with self._lock:
            self._write_unlocked(items)

    def _write_unlocked(self, items: list[dict]) -> None:
        self._path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _explain_unreachable(base: str, exc: Exception) -> str:
    text = str(exc)
    return (
        f"API недоступен ({base}): порт не принимает подключения. "
        "На том ПК должно быть открыто окно start.bat (строка Uvicorn running) "
        "и входящее правило брандмауэра TCP 8088 — запустите open-firewall.bat от администратора. "
        f"Технически: {text}"
    )


async def ping_url(url: str, api_key: str = "") -> dict:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    base = (url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=6.0, trust_env=False) as client:
        for path in ("/health", "/v1/models"):
            try:
                res = await client.get(base + path, headers=headers)
                res.raise_for_status()
                data = res.json() if res.content else {}
                if not isinstance(data, dict):
                    data = {}
                if path == "/health":
                    status = str(data.get("status") or "").lower()
                    if data.get("loading") is True or status in {"loading", "starting"}:
                        return {
                            "ok": False,
                            "loading": True,
                            "url": base,
                            "model": data.get("model") or "загружается…",
                            "device": data.get("device"),
                        }
                    err = data.get("error")
                    if isinstance(err, str) and err.strip():
                        raise ValueError(f"Модель на удалённом ПК не загрузилась: {err}")
                    model = data.get("model")
                    if not model:
                        try:
                            models = await client.get(base + "/v1/models", headers=headers)
                            if models.is_success:
                                payload = models.json() if models.content else {}
                                rows = payload.get("data") if isinstance(payload, dict) else None
                                if isinstance(rows, list) and rows:
                                    model = rows[0].get("id")
                        except Exception:
                            model = None
                    return {
                        "ok": True,
                        "loading": False,
                        "url": base,
                        "model": model or "llama-server",
                        "device": data.get("device"),
                    }
                model = data.get("model")
                if not model and isinstance(data.get("data"), list) and data["data"]:
                    model = data["data"][0].get("id")
                return {"ok": True, "loading": False, "url": base, "model": model, "device": data.get("device")}
            except httpx.ConnectError as exc:
                last_error = exc
                break
            except ValueError:
                raise
            except Exception as exc:
                last_error = exc
    if isinstance(last_error, httpx.ConnectError):
        raise ValueError(_explain_unreachable(base, last_error)) from last_error
    raise ValueError(f"API недоступен ({base}): {last_error}") from last_error
