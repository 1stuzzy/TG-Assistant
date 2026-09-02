"""Выгрузка живых личек с подключённого аккаунта в few-shot для модели."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import User

from app.services.conversation_director import pack_dir, reload_pack, style_shot_ok

SKIP_USER_IDS = {777000, 42777}
PREFERRED_FOLDERS = {"парсинг", "parsing"}

logger = logging.getLogger(__name__)

LIVE_NAME = "dialogues.live.jsonl"
CHAT_LIMIT = 80
PAIR_LIMIT = 20_000
LIVE_CAP = 20_000
MIN_CHARS = 2
MAX_CHARS = 400

_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_AT = re.compile(r"@[A-Za-z0-9_]{4,}")
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.I)
_CODE = re.compile(r"```|^\s*(def |import |function |SELECT )", re.M)
_SPACE = re.compile(r"\s+")
_MEDIA_MARK = re.compile(
    r"^\[(стикер|фото|видео|гифка|голосовое|кружок|аудио|вложение)\b",
    re.I,
)


def _is_session_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _filter_title(filt) -> str:
    raw = getattr(filt, "title", "") or ""
    if hasattr(raw, "text"):
        raw = raw.text
    return str(raw).strip()


def _is_photo_or_audio(message) -> bool:
    if message is None:
        return False
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return True
    if getattr(message, "photo", None):
        return True
    if getattr(message, "video_note", None):
        return True
    return False


def _clean(text: str) -> str:
    text = _URL.sub("", text)
    text = _PHONE.sub("", text)
    text = _AT.sub("", text)
    return _SPACE.sub(" ", text).strip()


def _plain_text(message) -> str:
    if message is None:
        return ""
    if getattr(message, "action", None):
        return ""
    if _is_photo_or_audio(message):
        return ""
    text = (getattr(message, "message", None) or getattr(message, "raw_text", None) or "").strip()
    if not text or _MEDIA_MARK.match(text) or _CODE.search(text):
        return ""
    return _clean(text)


def _ok_shot(text: str) -> bool:
    if not text or not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if _MEDIA_MARK.match(text) or _CODE.search(text):
        return False
    return True


def _clip(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_CHARS:
        return text
    cut = text[:MAX_CHARS].rsplit(" ", 1)[0].strip()
    return cut or text[:MAX_CHARS]


def _display_name(entity) -> str:
    if entity is None:
        return "Без имени"
    parts = [
        str(getattr(entity, "first_name", None) or "").strip(),
        str(getattr(entity, "last_name", None) or "").strip(),
    ]
    name = " ".join(p for p in parts if p).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return "Без имени"


def _last_preview(dialog) -> str:
    msg = getattr(dialog, "message", None)
    text = _plain_text(msg) if msg is not None else ""
    if not text:
        raw = (getattr(msg, "message", None) or "") if msg is not None else ""
        text = _SPACE.sub(" ", raw).strip()
    if len(text) > 72:
        text = text[:71] + "…"
    return text


def _pairs_from_messages(messages, pair_limit: int = PAIR_LIMIT) -> list[dict]:
    """messages — от старого к новому (reverse=True у Telethon)."""
    turns: list[dict] = []
    for msg in messages or []:
        text = _plain_text(msg)
        if not text:
            continue
        role = "assistant" if getattr(msg, "out", False) else "user"
        if turns and turns[-1]["role"] == role:
            merged = _clip(turns[-1]["content"] + " " + text)
            turns[-1]["content"] = merged
        else:
            turns.append({"role": role, "content": _clip(text)})

    pairs: list[dict] = []
    i = 0
    cap = max(1, int(pair_limit or PAIR_LIMIT))
    while i < len(turns) - 1 and len(pairs) < cap:
        a, b = turns[i], turns[i + 1]
        if a["role"] == "user" and b["role"] == "assistant" and _ok_shot(a["content"]) and _ok_shot(b["content"]):
            pairs.append({"user": a["content"], "assistant": b["content"]})
            i += 2
            continue
        i += 1
    return pairs


def _folder_peer_ids(filt) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for peer in list(getattr(filt, "pinned_peers", None) or []) + list(
        getattr(filt, "include_peers", None) or []
    ):
        try:
            pid = int(utils.get_peer_id(peer))
        except Exception:
            uid = getattr(peer, "user_id", None)
            if not uid:
                continue
            pid = int(uid)
        if pid in seen:
            continue
        seen.add(pid)
        ids.append(pid)
    return ids


def _load_live_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Не прочитан %s: %s", path.name, exc)
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("messages"):
            rows.append(row)
    return rows


def _write_live_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if body:
        body += "\n"
    path.write_text(body, encoding="utf-8")


class DialogHarvest:
    def __init__(self, store, agents, telegram):
        self._store = store
        self._agents = agents
        self._telegram = telegram

    async def list_folders(self, account_id: str) -> dict:
        account = self._require(account_id)

        async def _run(client: TelegramClient) -> list[dict]:
            folders: list[dict] = []
            for filt in await self._filters(client):
                fid = getattr(filt, "id", None)
                if not isinstance(fid, int):
                    continue
                title = _filter_title(filt) or f"Папка {fid}"
                folders.append(
                    {
                        "id": str(int(fid)),
                        "title": title,
                        "chats": len(_folder_peer_ids(filt)),
                        "preferred": title.casefold() in PREFERRED_FOLDERS,
                    }
                )
            folders.sort(key=lambda item: (not item["preferred"], item["title"].casefold()))
            folders.append(
                {
                    "id": "all",
                    "title": "Все личные чаты",
                    "chats": None,
                    "preferred": False,
                }
            )
            return folders

        folders, via = await self._with_client(account, _run)
        return {
            "account_id": account.id,
            "account_name": account.name,
            "via": via,
            "folders": folders,
        }

    async def list_chats(self, account_id: str, folder_id: str | None = None) -> dict:
        account = self._require(account_id)
        folder_key = (folder_id or "").strip() or "all"

        async def _run(client: TelegramClient) -> tuple[str, list[dict]]:
            if folder_key == "all":
                chats = await self._list_private_dialogs(client)
                return "Все личные чаты", chats
            filt = await self._folder_by_id(client, folder_key)
            title = _filter_title(filt) or "Папка"
            chats = await self._list_folder_dialogs(client, filt)
            return title, chats

        (folder_title, chats), via = await self._with_client(account, _run)
        return {
            "account_id": account.id,
            "account_name": account.name,
            "folder_id": folder_key,
            "folder_title": folder_title,
            "via": via,
            "chats": chats,
        }

    async def import_chat(self, account_id: str, chat_id: str, max_pairs: int | None = None) -> dict:
        account = self._require(account_id)
        try:
            peer_id = int(str(chat_id).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректный чат") from exc
        cap = PAIR_LIMIT if not max_pairs else max(1, min(int(max_pairs), PAIR_LIMIT))

        async def _run(client: TelegramClient) -> tuple[str, list[dict], int]:
            try:
                entity = await client.get_entity(peer_id)
            except Exception as exc:
                raise ValueError("Чат не найден в этом аккаунте") from exc
            if not isinstance(entity, User) or getattr(entity, "bot", False) or getattr(entity, "is_self", False):
                raise ValueError("Можно выгрузить только личный чат с человеком")
            try:
                messages = await self._collect_messages(client, entity)
            except FloodWaitError as exc:
                raise ValueError(f"Telegram просит подождать {int(exc.seconds)} сек.") from exc
            pairs = _pairs_from_messages(messages, cap)
            return _display_name(entity), pairs, len(messages)

        (name, pairs, scanned), via = await self._with_client(account, _run)
        if not pairs:
            raise ValueError(
                "В этом чате нет текстовых реплик: фото и аудио пропускаются, нужен обычный текст"
            )
        added, total = self._save_pairs(account.id, str(peer_id), pairs)
        reload_pack()
        logger.info(
            "Выгружен чат %s/%s: %s пар из %s сообщений (via=%s, live=%s)",
            account.id,
            peer_id,
            added,
            scanned,
            via,
            total,
        )
        return {
            "account_id": account.id,
            "chat_id": str(peer_id),
            "name": name,
            "added": added,
            "scanned": scanned,
            "total_live": total,
            "via": via,
            "pairs": pairs[:8],
        }

    async def _filters(self, client: TelegramClient) -> list:
        raw = await client(GetDialogFiltersRequest())
        return list(getattr(raw, "filters", None) or raw or [])

    async def _folder_by_id(self, client: TelegramClient, folder_id: str):
        try:
            want = int(folder_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректная папка") from exc
        for filt in await self._filters(client):
            fid = getattr(filt, "id", None)
            if isinstance(fid, int) and fid == want:
                return filt
        raise ValueError("Папка не найдена. Проверьте, что она есть в Telegram на этом аккаунте.")

    async def _list_private_dialogs(self, client: TelegramClient) -> list[dict]:
        chats: list[dict] = []
        async for dialog in client.iter_dialogs(limit=CHAT_LIMIT + 20):
            row = self._chat_row(dialog.entity, dialog)
            if not row:
                continue
            chats.append(row)
            if len(chats) >= CHAT_LIMIT:
                break
        return chats

    async def _list_folder_dialogs(self, client: TelegramClient, filt) -> list[dict]:
        wanted = set(_folder_peer_ids(filt))
        if not wanted:
            return []
        found: dict[int, dict] = {}
        async for dialog in client.iter_dialogs(limit=max(120, len(wanted) * 4)):
            entity = dialog.entity
            try:
                pid = int(utils.get_peer_id(entity))
            except Exception:
                pid = int(getattr(dialog, "id", 0) or 0)
            if pid not in wanted:
                continue
            row = self._chat_row(entity, dialog)
            if row:
                found[pid] = row
            if len(found) >= len(wanted):
                break
        for pid in wanted:
            if pid in found:
                continue
            try:
                entity = await client.get_entity(pid)
            except Exception:
                continue
            row = self._chat_row(entity, None)
            if row:
                found[pid] = row
        return [found[pid] for pid in wanted if pid in found]

    def _chat_row(self, entity, dialog=None) -> dict | None:
        if not isinstance(entity, User):
            return None
        if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
            return None
        uid = int(getattr(entity, "id", 0) or 0)
        if uid in SKIP_USER_IDS:
            return None
        date = getattr(dialog, "date", None) if dialog is not None else None
        try:
            chat_id = str(int(utils.get_peer_id(entity)))
        except Exception:
            chat_id = str(uid)
        return {
            "id": chat_id,
            "name": _display_name(entity),
            "last": _last_preview(dialog) if dialog is not None else "",
            "unread": int(getattr(dialog, "unread_count", 0) or 0) if dialog is not None else 0,
            "date": date.isoformat() if date is not None else None,
        }

    async def _collect_messages(self, client: TelegramClient, entity) -> list:
        messages: list = []
        async for msg in client.iter_messages(entity, limit=None, reverse=True):
            messages.append(msg)
        return messages

    def _save_pairs(self, account_id: str, chat_id: str, pairs: list[dict]) -> tuple[int, int]:
        path = pack_dir() / LIVE_NAME
        source = f"tg:{account_id}:{chat_id}"
        rows = [row for row in _load_live_rows(path) if row.get("source") != source]
        fresh = [
            {
                "source": source,
                "messages": [
                    {"role": "user", "content": item["user"]},
                    {"role": "assistant", "content": item["assistant"]},
                ],
            }
            for item in pairs
            if style_shot_ok(item["user"], item["assistant"])
        ]
        merged = fresh + rows
        if len(merged) > LIVE_CAP:
            merged = merged[:LIVE_CAP]
        _write_live_rows(path, merged)
        return len(fresh), len(merged)

    def _require(self, account_id: str):
        account = self._store.get(account_id)
        if not account:
            raise ValueError("Аккаунт не найден")
        return account

    @asynccontextmanager
    async def _open_client(self, account):
        live = self._agents.live_client(account.id)
        if live:
            yield live, "agent"
            return
        path = Path(account.session_file)
        if not path.exists():
            raise ValueError("Нет файла сессии — сначала подключите аккаунт")
        client = self._telegram._new_client(path)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise ValueError("Сессия недействительна — проверьте аккаунт")
            yield client, "session"
        except Exception as exc:
            if _is_session_locked(exc):
                await asyncio.sleep(0.8)
                live = self._agents.live_client(account.id)
                if live:
                    yield live, "agent"
                    return
                raise ValueError(
                    "Сессия занята агентом. Дождитесь запуска ИИ или остановите его и повторите."
                ) from exc
            raise
        finally:
            if client.is_connected():
                await client.disconnect()

    async def _with_client(self, account, fn):
        try:
            async with self._open_client(account) as (client, via):
                return await fn(client), via
        except ValueError:
            raise
        except FloodWaitError as exc:
            raise ValueError(f"Telegram просит подождать {int(exc.seconds)} сек.") from exc
        except Exception as exc:
            if _is_session_locked(exc):
                raise ValueError(
                    "Сессия занята агентом. Дождитесь запуска ИИ или остановите его и повторите."
                ) from exc
            logger.exception("Выгрузка чата не удалась")
            raise ValueError(str(exc) or "Не удалось открыть Telegram") from exc
