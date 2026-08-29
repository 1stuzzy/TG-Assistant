"""
ИИ-агент на Telethon-сессии: читает личные чаты и отвечает GGUF-моделью
локально или на удалённом воркере.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
    SendReactionRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.types import DialogFilter, InputPeerSelf, ReactionEmoji, User

from app.config import settings
from app.services.account_store import AccountStore
from app.services.character_store import CharacterStore, build_persona, first_name
from app.services.chat_memory import ChatMemory
from app.services.llm_engine import DEFAULT_PERSONA, LLMEngine, fallback_reply
from app.services.model_catalog import ModelCatalog
from app.services.worker_store import WorkerStore
from app.services.world_context import clock as world_clock

logger = logging.getLogger(__name__)

SKIP_USER_IDS = {777000, 42777}
POLL_EVERY_SEC = 5
DIALOG_SCAN_LIMIT = 25
RECENT_INCOMING_MINUTES = 15
REPLY_FOLDER_TITLE = "TG-Assistant"
HISTORY_FETCH = 300
HISTORY_CHAR_BUDGET = 2400
BURST_QUIET_SEC = 4.5
BURST_MAX_SEC = 18.0
VOICE_REPLIES = (
    "напиши текстом, я без наушников",
    "голосовые ща не слушаю, кинь письменно)",
    "в дороге, давай текстом",
    "не могу слушать, что там?",
)
REPLY_FOLDER_ALIASES = {REPLY_FOLDER_TITLE.lower(), "tg-assistant", "ии-агент"}


@dataclass
class AgentState:
    running: bool = False
    status: str = "stopped"
    model: Optional[str] = None
    persona: Optional[str] = None
    character_name: Optional[str] = None
    character_city: Optional[str] = None
    character_gender: Optional[str] = None
    engine: str = "local"
    worker_name: Optional[str] = None
    replies: int = 0
    received: int = 0
    processed: int = 0
    typing_text: str = ""
    last_error: Optional[str] = None
    started_at: Optional[str] = None
    last_reply_at: Optional[str] = None
    last_incoming_at: Optional[str] = None
    folder_title: str = REPLY_FOLDER_TITLE
    folder_limit: int = 0
    folder_chats: list = field(default_factory=list)
    folder_hint: str = ""

    def snapshot(self) -> dict:
        pending = max(0, self.received - self.processed)
        return {
            "running": self.running,
            "status": self.status,
            "model": self.model,
            "replies": self.replies,
            "received": self.received,
            "processed": self.processed,
            "pending": pending,
            "typing_text": self.typing_text,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "last_reply_at": self.last_reply_at,
            "last_incoming_at": self.last_incoming_at,
            "character_name": self.character_name,
            "engine": self.engine,
            "worker_name": self.worker_name,
            "folder_title": self.folder_title,
            "folder_limit": self.folder_limit,
            "folder_chats": list(self.folder_chats),
            "folder_hint": self.folder_hint,
        }


class AgentManager:
    def __init__(
        self,
        store: AccountStore,
        llm: LLMEngine,
        catalog: ModelCatalog,
        characters: CharacterStore,
        workers: WorkerStore,
        rental=None,
    ):
        self._store = store
        self._llm = llm
        self._catalog = catalog
        self._characters = characters
        self._workers = workers
        self._rental = rental
        self._memory = ChatMemory(settings.accounts_file.parent / "chat_memory.json")
        self._states: dict[str, AgentState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._clients: dict[str, TelegramClient] = {}
        self._latest: dict[str, dict[int, tuple]] = {}
        self._replied: dict[str, dict[int, int]] = {}
        self._inflight: dict[str, set[int]] = {}
        self._chat_locks: dict[str, dict[int, asyncio.Lock]] = {}
        self._generating_counts: dict[str, int] = {}
        self._remote: dict[str, tuple[str, str]] = {}
        self._folder_ids: dict[str, set[int]] = {}

    def is_running(self, account_id: str) -> bool:
        state = self._states.get(account_id)
        return bool(state and state.running)

    def snapshot(self, account_id: str) -> dict:
        state = self._states.get(account_id)
        return state.snapshot() if state else AgentState().snapshot()

    def _delays_for(self, account_id: str) -> tuple[int, int]:
        read_ms, reply_ms = 800, 1500
        account = self._store.get(account_id)
        if account and account.tenant_id and self._rental:
            tenant = self._rental.get_tenant(account.tenant_id)
            if tenant:
                try:
                    read_ms = int(tenant.get("read_delay_ms") or 800)
                except (TypeError, ValueError):
                    read_ms = 800
                try:
                    reply_ms = int(tenant.get("reply_delay_ms") or 1500)
                except (TypeError, ValueError):
                    reply_ms = 1500
        return max(0, min(read_ms, 60_000)), max(0, min(reply_ms, 60_000))

    async def start(
        self,
        account_id: str,
        model_name: Optional[str] = None,
        persona: Optional[str] = None,
        character_id: Optional[str] = None,
        engine: str = "local",
        worker_id: Optional[str] = None,
    ) -> dict:
        account = self._store.get(account_id)
        if not account:
            raise ValueError("Аккаунт не найден")
        if self.is_running(account_id):
            raise ValueError("ИИ-агент уже запущен на этом аккаунте")
        if not account.session_file:
            raise ValueError("У аккаунта нет файла сессии")
        if account.tenant_id and self._rental:
            tenant = self._rental.get_tenant(account.tenant_id)
            if not tenant or tenant["status"] != "active":
                raise ValueError("Панель неактивна — запуск агента запрещён")
            running = sum(
                1
                for acc in self._store.list_by_tenant(account.tenant_id)
                if self.is_running(acc.id)
            )
            if running >= int(tenant["max_agents"] or 0):
                raise ValueError(
                    f"Лимит агентов панели исчерпан ({tenant['max_agents']})"
                )
            if tenant.get("engine"):
                engine = tenant["engine"]
            if tenant.get("model_name") and engine == "local":
                model_name = tenant["model_name"]
            if tenant.get("worker_id") and engine == "remote":
                worker_id = tenant["worker_id"]

        engine = (engine or "local").strip().lower()
        if engine not in {"local", "remote"}:
            raise ValueError("engine должен быть local или remote")

        character = None
        if character_id:
            character = self._characters.get(character_id)
            if not character:
                raise ValueError("Персонаж не найден")
        extra_style = (persona or "").strip()
        if character:
            system_prompt = build_persona(character, extra_style or None)
        else:
            system_prompt = DEFAULT_PERSONA
            if extra_style:
                system_prompt += "\n\nДополнительный стиль (обязательно соблюдай):\n" + extra_style
        logger.info(
            "Промт агента account=%s character=%s extra_style=%s prompt_chars=%s",
            account.phone,
            character.get("name") if character else "—",
            bool(extra_style),
            len(system_prompt),
        )
        if extra_style:
            logger.info("Доп. стиль: %s", extra_style[:300])

        model_path: Optional[Path] = None
        model_label = "remote"
        worker = None
        if engine == "local":
            if not model_name:
                raise ValueError("Для запуска на этом ПК выберите GGUF-модель")
            model_path = self._catalog.resolve(model_name)
            model_label = model_path.name
        else:
            if not worker_id:
                raise ValueError("Для удалённого запуска выберите компьютер с моделью")
            worker = self._workers.get(worker_id)
            if not worker:
                raise ValueError("Удалённый компьютер не найден — добавьте его в Настройках")
            await self._workers.wait_ready(worker_id)
            model_label = f"remote:{worker['name']}"

        state = AgentState(
            running=True,
            status="starting",
            model=model_label,
            persona=system_prompt,
            character_name=character["name"] if character else None,
            character_city=(character.get("city") or "").strip() or None if character else None,
            character_gender=(character.get("gender") or "").strip() or None if character else None,
            engine=engine,
            worker_name=worker["name"] if worker else None,
        )
        self._states[account_id] = state
        self._latest[account_id] = {}
        self._replied.setdefault(account_id, {})
        self._inflight[account_id] = set()
        self._chat_locks[account_id] = {}
        if worker:
            self._remote[account_id] = (worker["url"], worker.get("api_key") or "")
        else:
            self._remote.pop(account_id, None)

        logger.info(
            "Старт агента account=%s engine=%s model=%s character=%s",
            account.phone,
            engine,
            model_label,
            state.character_name or "—",
        )
        try:
            clk = world_clock(state.character_city)
            logger.info(
                "Мир агента персонаж=%s gender=%s city=%s tz=%s сейчас=%s %s %s",
                state.character_name or "—",
                state.character_gender or "—",
                clk.city or "—",
                clk.zone,
                clk.weekday,
                clk.time_line,
                clk.part,
            )
        except Exception:
            logger.debug("Часы персонажа недоступны", exc_info=True)
        self._tasks[account_id] = asyncio.create_task(
            self._run(account_id, model_path, system_prompt)
        )
        return state.snapshot()

    async def stop(self, account_id: str) -> dict:
        stop = self._stops.get(account_id)
        if stop:
            stop.set()
        task = self._tasks.get(account_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=12)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        state = self._states.get(account_id)
        if state:
            state.running = False
            state.typing_text = ""
            if state.status != "error":
                state.status = "stopped"
        self._clients.pop(account_id, None)
        self._stops.pop(account_id, None)
        self._tasks.pop(account_id, None)
        self._remote.pop(account_id, None)
        self._folder_ids.pop(account_id, None)
        return self.snapshot(account_id)

    async def stop_all(self) -> None:
        for account_id in list(self._tasks.keys()):
            await self.stop(account_id)

    def _lock_for(self, account_id: str, chat_id: int) -> asyncio.Lock:
        locks = self._chat_locks.setdefault(account_id, {})
        if chat_id not in locks:
            locks[chat_id] = asyncio.Lock()
        return locks[chat_id]

    async def _run(self, account_id: str, model_path: Optional[Path], persona: Optional[str]) -> None:
        state = self._states[account_id]
        stop = asyncio.Event()
        self._stops[account_id] = stop
        client: Optional[TelegramClient] = None
        poll_task: Optional[asyncio.Task] = None
        try:
            if model_path is not None:
                state.status = "loading_model"
                logger.info("Загрузка модели в RAM: %s", model_path.name)
                await self._llm.ensure_loaded(model_path)
            else:
                logger.info("Локальная модель не грузится — ответы пойдут на удалённый ПК")

            account = self._store.get(account_id)
            if not account:
                raise RuntimeError("Аккаунт исчез во время запуска")

            state.status = "connecting"
            logger.info("Подключение Telethon-сессии %s", account.session_file)
            client = TelegramClient(
                str(account.session_file),
                settings.api_id,
                settings.api_hash,
                sequential_updates=True,
            )
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия Telegram недействительна — проверьте аккаунт")

            me = await client.get_me()
            await client(GetStateRequest())
            self._clients[account_id] = client
            await self._sync_reply_folder(account_id, client)

            @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
            async def on_new_message(event):
                await self._on_new_message(account_id, client, event, persona)

            @client.on(events.Raw)
            async def on_raw(update):
                if "DialogFilter" in type(update).__name__:
                    await self._sync_reply_folder(account_id, client)

            state.status = "running"
            state.started_at = datetime.now(timezone.utc).isoformat()
            who = f"@{me.username}" if getattr(me, "username", None) else (me.first_name or account.phone)
            logger.info(
                "Агент слушает ЛС для %s (%s). Пишите из ДРУГОГО Telegram. engine=%s",
                account.phone,
                who,
                state.engine,
            )

            poll_task = asyncio.create_task(
                self._poll_inbox(account_id, client, persona, stop)
            )
            await self._wait_stop_or_disconnect(client, stop)
        except Exception as exc:
            logger.exception("ИИ-агент %s остановился с ошибкой", account_id)
            state.last_error = str(exc)
            state.status = "error"
        finally:
            if poll_task:
                poll_task.cancel()
                try:
                    await poll_task
                except (asyncio.CancelledError, Exception):
                    pass
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            state.running = False
            state.typing_text = ""
            if state.status in {"running", "generating"}:
                state.status = "stopped"
            self._clients.pop(account_id, None)
            self._stops.pop(account_id, None)
            self._tasks.pop(account_id, None)
            self._remote.pop(account_id, None)
            self._folder_ids.pop(account_id, None)
            logger.info("Агент account=%s остановлен", account_id)

    async def _wait_stop_or_disconnect(self, client: TelegramClient, stop: asyncio.Event) -> None:
        stop_task = asyncio.create_task(stop.wait())
        disc_task = asyncio.ensure_future(client.disconnected)
        try:
            done, _pending = await asyncio.wait(
                {stop_task, disc_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disc_task in done and not stop.is_set():
                err = getattr(client, "_updates_error", None)
                raise RuntimeError(
                    f"Telegram отключился, пока агент слушал чаты: {err or 'disconnected'}"
                )
        finally:
            stop_task.cancel()

    async def _on_new_message(self, account_id: str, client: TelegramClient, event, persona: Optional[str]) -> None:
        text = self._message_text(event.message)
        if not text:
            return
        sender = await event.get_sender()
        if not self._should_reply(sender):
            logger.info(
                "Пропуск отправителя chat=%s bot=%s self=%s",
                event.chat_id,
                getattr(sender, "bot", None),
                getattr(sender, "is_self", None),
            )
            return

        state = self._states.get(account_id)
        if state:
            state.last_incoming_at = datetime.now(timezone.utc).isoformat()
        logger.info("Входящее ЛС chat=%s: %s", event.chat_id, text[:120].replace("\n", " "))

        chat_id = event.chat_id
        token = object()
        self._latest.setdefault(account_id, {})[chat_id] = (token, event)
        await asyncio.sleep(1.0)
        current = self._latest.get(account_id, {}).get(chat_id)
        if not current or current[0] is not token:
            return
        latest_event = current[1]
        await self._reply(account_id, client, latest_event.message, persona, chat_id)

    async def _poll_inbox(
        self,
        account_id: str,
        client: TelegramClient,
        persona: Optional[str],
        stop: asyncio.Event,
    ) -> None:
        first = True
        while not stop.is_set():
            await self._sync_reply_folder(account_id, client)
            await self._scan_dialogs(account_id, client, persona, startup=first)
            first = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_EVERY_SEC)
                return
            except asyncio.TimeoutError:
                continue

    async def _scan_dialogs(
        self,
        account_id: str,
        client: TelegramClient,
        persona: Optional[str],
        *,
        startup: bool,
    ) -> None:
        state = self._states.get(account_id)
        if not state or not state.running:
            return
        started = self._parse_iso(state.started_at) or datetime.now(timezone.utc)
        recent_cut = datetime.now(timezone.utc) - timedelta(minutes=RECENT_INCOMING_MINUTES)
        try:
            async for dialog in client.iter_dialogs(limit=DIALOG_SCAN_LIMIT):
                if not state.running:
                    return
                if not dialog.is_user:
                    continue
                if not self._folder_allows(account_id, dialog.id):
                    continue
                entity = dialog.entity
                if not self._should_reply(entity):
                    continue
                messages = await client.get_messages(entity, limit=8)
                if not messages:
                    continue
                incoming = [
                    m
                    for m in messages
                    if m
                    and not getattr(m, "out", False)
                    and self._message_text(m)
                ]
                if not incoming:
                    continue
                last = max(incoming, key=lambda m: int(m.id))
                already = self._replied.get(account_id, {}).get(dialog.id, 0)
                if int(last.id) <= int(already or 0):
                    continue
                msg_date = last.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                unread = int(getattr(dialog, "unread_count", 0) or 0)
                if startup:
                    if unread <= 0 and msg_date < recent_cut:
                        continue
                elif msg_date < started - timedelta(seconds=5):
                    continue
                logger.info(
                    "Найдено входящее в диалоге %s (unread=%s, startup=%s)",
                    dialog.id,
                    unread,
                    startup,
                )
                state.last_incoming_at = datetime.now(timezone.utc).isoformat()
                await self._reply(account_id, client, last, persona, dialog.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка при разборе личных диалогов")

    async def _reply(
        self,
        account_id: str,
        client: TelegramClient,
        message,
        persona: Optional[str],
        chat_id: int,
    ) -> None:
        state = self._states.get(account_id)
        if not state or not state.running:
            return
        account = self._store.get(account_id)
        if account and account.tenant_id and self._rental:
            tenant = self._rental.get_tenant(account.tenant_id)
            if not tenant or tenant["status"] != "active":
                return
            if not self._folder_allows(account_id, chat_id):
                logger.info("Чат %s не в папке «%s» — пропуск", chat_id, REPLY_FOLDER_TITLE)
                return
            if not self._rental.allow_chat(
                account.tenant_id,
                account_id,
                str(chat_id),
                int(tenant["max_chats"] or 0),
            ):
                logger.info("Чат %s вне лимита панели", chat_id)
                return
        if not self._message_text(message):
            return

        lock = self._lock_for(account_id, chat_id)
        async with lock:
            already = self._replied.setdefault(account_id, {})
            inflight = self._inflight.setdefault(account_id, set())
            latest_id = await self._wait_for_burst(client, chat_id, message.id)
            if already.get(chat_id, 0) >= latest_id:
                return
            inflight.add(chat_id)
            state.received += 1
            try:
                await self._generate_and_send(
                    account_id, client, message, persona, chat_id, state, latest_id
                )
            finally:
                inflight.discard(chat_id)

    async def _generate_and_send(
        self,
        account_id: str,
        client: TelegramClient,
        message,
        persona: Optional[str],
        chat_id: int,
        state: AgentState,
        latest_id: int,
    ) -> None:
        msg_id = latest_id or message.id
        read_ms, reply_ms = self._delays_for(account_id)
        if read_ms:
            await asyncio.sleep(read_ms / 1000.0)
        try:
            await client.send_read_acknowledge(chat_id, message)
        except Exception:
            logger.debug("Не удалось отметить сообщение прочитанным chat=%s", chat_id, exc_info=True)
        if reply_ms:
            await asyncio.sleep(reply_ms / 1000.0)
        await self._maybe_react(client, chat_id, msg_id)
        typing_stop = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(client, chat_id, typing_stop))
        self._generating_counts[account_id] = self._generating_counts.get(account_id, 0) + 1
        state.processed += 1
        state.status = "generating"
        state.typing_text = ""
        try:
            remote = self._remote.get(account_id)
            reply = ""
            peer = await self._peer_label(client, chat_id)
            for attempt in range(5):
                extra_wait = await self._wait_for_burst(client, chat_id, msg_id)
                msg_id = max(msg_id, extra_wait)
                history, last_in_id = await self._history(client, chat_id)
                if last_in_id:
                    msg_id = max(msg_id, last_in_id)
                memory = self._memory.remember(account_id, chat_id, history, peer)
                logger.info(
                    "Генерация chat=%s peer=%s реплик=%s память=%s попытка=%s",
                    chat_id,
                    (peer or "—")[:40],
                    len(history),
                    len(memory),
                    attempt + 1,
                )

                def on_partial(text: str) -> None:
                    state.typing_text = (text or "")[:380]

                voice_reply = self._voice_only_reply(history)
                if voice_reply:
                    reply = voice_reply
                else:
                    account_persona = (state.persona or persona or DEFAULT_PERSONA)
                    who = first_name({"name": state.character_name}) if state.character_name else None
                    reply = await self._llm.generate(
                        history,
                        account_persona,
                        remote_url=remote[0] if remote else None,
                        remote_key=remote[1] if remote else None,
                        on_partial=on_partial,
                        peer=peer,
                        memory=memory,
                        city=state.character_city,
                        name=who,
                        gender=state.character_gender,
                    )
                newer_id = await self._latest_incoming_id(client, chat_id)
                if newer_id <= msg_id:
                    break
                msg_id = newer_id
                logger.info("Пока писали, пришли новые сообщения chat=%s — пересобираю ответ", chat_id)
            state.typing_text = reply
            if not (reply or "").strip():
                last_text = next(
                    (m.get("content") or "" for m in reversed(history) if m.get("role") == "user"),
                    "",
                )
                who = first_name({"name": state.character_name}) if state.character_name else None
                reply = fallback_reply(last_text, who, state.character_gender, state.character_city)
            await asyncio.sleep(min(0.25 + len(reply) * 0.012, 0.9))
            await client.send_message(chat_id, reply)
            self._replied.setdefault(account_id, {})[chat_id] = msg_id
            state.replies += 1
            state.last_reply_at = datetime.now(timezone.utc).isoformat()
            state.last_error = None
            logger.info("Ответ отправлен chat=%s: %s", chat_id, reply[:120].replace("\n", " "))
        except FloodWaitError as exc:
            logger.warning("FloodWait %s сек", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
        except Exception as exc:
            logger.exception("Не удалось ответить в чате %s: %s", chat_id, exc)
            state.last_error = str(exc)
        finally:
            state.typing_text = ""
            typing_stop.set()
            typing_task.cancel()
            try:
                await typing_task
            except (asyncio.CancelledError, Exception):
                pass
            self._generating_counts[account_id] = max(
                0, self._generating_counts.get(account_id, 1) - 1
            )
            if (
                state.running
                and self._generating_counts.get(account_id, 0) == 0
                and state.status == "generating"
            ):
                state.status = "running"

    async def _keep_typing(self, client: TelegramClient, chat_id: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with client.action(chat_id, "typing"):
                    await asyncio.wait_for(stop.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                return

    async def _wait_for_burst(self, client: TelegramClient, chat_id: int, known_id: int) -> int:
        """Ждём паузу в наборе, чтобы собрать несколько сообщений в одно обращение."""
        latest = known_id
        deadline = time.monotonic() + BURST_MAX_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(BURST_QUIET_SEC)
            try:
                messages = await client.get_messages(chat_id, limit=16)
            except Exception:
                break
            incoming = [
                m.id
                for m in messages
                if not getattr(m, "out", False) and self._message_text(m)
            ]
            if not incoming:
                break
            newest = max(incoming)
            if newest <= latest:
                break
            latest = newest
        return latest

    async def _latest_incoming_id(self, client: TelegramClient, chat_id: int) -> int:
        try:
            messages = await client.get_messages(chat_id, limit=12)
        except Exception:
            return 0
        last_in_id = 0
        for msg in messages or []:
            if getattr(msg, "out", False) or not self._message_text(msg):
                continue
            last_in_id = max(last_in_id, int(getattr(msg, "id", 0) or 0))
        return last_in_id

    async def _history(self, client: TelegramClient, chat_id: int) -> tuple[list[dict], int]:
        messages = await client.get_messages(chat_id, limit=HISTORY_FETCH)
        history: list[dict] = []
        last_in_id = 0
        for msg in reversed(list(messages)):
            text = self._message_text(msg)
            if not text:
                continue
            role = "assistant" if msg.out else "user"
            if not msg.out:
                last_in_id = max(last_in_id, int(msg.id))
            if history and history[-1]["role"] == role:
                history[-1]["content"] = (history[-1]["content"] + "\n" + text).strip()
            else:
                history.append({"role": role, "content": text})
        return history, last_in_id

    @staticmethod
    async def _peer_label(client: TelegramClient, chat_id: int) -> str:
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            return ""
        name = " ".join(
            part
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part
        ).strip()
        return name or (getattr(entity, "username", None) or "")

    @staticmethod
    def _sticker_emoji(message) -> str:
        sticker = getattr(message, "sticker", None)
        if not sticker:
            return ""
        file = getattr(message, "file", None)
        emoji = getattr(file, "emoji", None) if file is not None else None
        if emoji:
            return str(emoji)
        for attr in getattr(sticker, "attributes", None) or []:
            alt = getattr(attr, "alt", None)
            if alt:
                return str(alt)
        return ""

    @classmethod
    def _message_text(cls, message) -> str:
        if message is None:
            return ""
        text = (getattr(message, "message", None) or getattr(message, "raw_text", None) or "").strip()
        bits: list[str] = []
        if getattr(message, "sticker", None):
            emoji = cls._sticker_emoji(message)
            bits.append(f"[стикер{(' ' + emoji) if emoji else ''}]")
        elif getattr(message, "voice", None):
            bits.append("[голосовое]")
        elif getattr(message, "video_note", None):
            bits.append("[кружок]")
        elif getattr(message, "gif", None):
            bits.append("[гифка]")
        elif getattr(message, "photo", None):
            bits.append("[фото]")
        elif getattr(message, "video", None):
            bits.append("[видео]")
        elif getattr(message, "audio", None):
            bits.append("[аудио]")
        elif getattr(message, "media", None) and not text:
            bits.append("[вложение]")
        if bits and text:
            return f"{text} {' '.join(bits)}"
        if bits:
            return bits[0]
        return text

    @staticmethod
    def _last_user_text(history: list[dict]) -> str:
        for item in reversed(history):
            if item.get("role") == "user":
                return (item.get("content") or "").strip()
        return ""

    @classmethod
    def _voice_only_reply(cls, history: list[dict]) -> str:
        text = cls._last_user_text(history)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""
        if all(ln in {"[голосовое]", "[кружок]"} for ln in lines):
            return random.choice(VOICE_REPLIES)
        return ""

    def _react_emoji(self, message) -> str:
        if getattr(message, "sticker", None):
            raw = self._sticker_emoji(message) or "😂"
            return raw[:2] if raw else "😂"
        if getattr(message, "voice", None) or getattr(message, "video_note", None):
            return "🔥"
        if getattr(message, "photo", None) or getattr(message, "gif", None):
            return "🔥"
        return ""

    async def _send_reaction(self, client: TelegramClient, chat_id: int, msg_id: int, emoji: str) -> None:
        try:
            peer = await client.get_input_entity(chat_id)
        except Exception:
            peer = chat_id
        for candidate in (emoji, "👍"):
            if not candidate:
                continue
            try:
                await client(
                    SendReactionRequest(
                        peer=peer,
                        msg_id=int(msg_id),
                        reaction=[ReactionEmoji(emoticon=candidate)],
                    )
                )
                return
            except Exception:
                continue
        logger.debug("Не удалось поставить реакцию chat=%s msg=%s", chat_id, msg_id)

    async def _maybe_react(self, client: TelegramClient, chat_id: int, msg_id: int) -> None:
        try:
            messages = await client.get_messages(chat_id, limit=8)
        except Exception:
            return
        incoming = [
            m
            for m in messages
            if m and not getattr(m, "out", False) and int(getattr(m, "id", 0) or 0) <= int(msg_id)
        ]
        for message in incoming:
            emoji = self._react_emoji(message)
            if emoji:
                await self._send_reaction(client, chat_id, int(message.id), emoji)
                return

    def _folder_limit_for(self, account_id: str) -> int:
        account = self._store.get(account_id)
        if not account or not account.tenant_id or not self._rental:
            return 0
        tenant = self._rental.get_tenant(account.tenant_id)
        if not tenant:
            return 0
        try:
            return max(0, int(tenant.get("max_chats") or 0))
        except (TypeError, ValueError):
            return 0

    def _uses_reply_folder(self, account_id: str) -> bool:
        account = self._store.get(account_id)
        return bool(account and account.tenant_id)

    def _folder_allows(self, account_id: str, chat_id: int) -> bool:
        account = self._store.get(account_id)
        if not account or not account.tenant_id:
            return True
        allowed = self._folder_ids.get(account_id)
        if allowed is None:
            return False
        return int(chat_id) in allowed

    def _make_dialog_filter(self, filter_id: int, include_peers: list):
        kwargs = dict(
            id=filter_id,
            title=REPLY_FOLDER_TITLE,
            pinned_peers=[],
            include_peers=include_peers,
            exclude_peers=[],
            emoticon="💬",
        )
        try:
            return DialogFilter(**kwargs)
        except TypeError:
            from telethon.tl.types import TextWithEntities
            kwargs["title"] = TextWithEntities(text=REPLY_FOLDER_TITLE, entities=[])
            return DialogFilter(**kwargs)

    async def _sync_reply_folder(self, account_id: str, client: TelegramClient) -> None:
        state = self._states.get(account_id)
        limit = self._folder_limit_for(account_id)
        if state:
            state.folder_title = REPLY_FOLDER_TITLE
            state.folder_limit = limit
        try:
            raw = await client(GetDialogFiltersRequest())
            filters = list(getattr(raw, "filters", None) or raw or [])
        except Exception:
            logger.exception("Не удалось прочитать папки Telegram")
            self._folder_ids[account_id] = set()
            if state:
                state.folder_chats = []
                state.folder_hint = "Не удалось прочитать папки в Telegram. Проверьте сессию."
            return

        found = None
        used_ids: set[int] = set()
        for filt in filters:
            fid = getattr(filt, "id", None)
            if isinstance(fid, int):
                used_ids.add(fid)
            if _dialog_filter_title(filt).lower() in REPLY_FOLDER_ALIASES:
                found = filt
                break

        if found is None:
            new_id = next((i for i in range(2, 256) if i not in used_ids), None)
            if new_id is None:
                self._folder_ids[account_id] = set()
                if state:
                    state.folder_hint = (
                        f"Нет свободного слота папки. Создайте вручную папку «{REPLY_FOLDER_TITLE}»."
                    )
                return
            created = self._make_dialog_filter(new_id, [InputPeerSelf()])
            try:
                await client(UpdateDialogFilterRequest(id=new_id, filter=created))
                logger.info("Создана папка «%s» для account=%s", REPLY_FOLDER_TITLE, account_id)
                raw = await client(GetDialogFiltersRequest())
                filters = list(getattr(raw, "filters", None) or raw or [])
                for filt in filters:
                    if _dialog_filter_title(filt).lower() in REPLY_FOLDER_ALIASES:
                        found = filt
                        break
                if found is None:
                    found = created
            except Exception as exc:
                logger.warning("Не удалось создать папку «%s»: %s", REPLY_FOLDER_TITLE, exc)
                self._folder_ids[account_id] = set()
                if state:
                    state.folder_chats = []
                    state.folder_hint = (
                        f"Создайте в Telegram папку «{REPLY_FOLDER_TITLE}» и перетащите туда личные чаты."
                    )
                return

        me_id = 0
        try:
            me = await client.get_me()
            me_id = int(getattr(me, "id", 0) or 0)
        except Exception:
            pass

        peer_ids: list[int] = []
        seen: set[int] = set()
        for peer in list(getattr(found, "pinned_peers", None) or []) + list(
            getattr(found, "include_peers", None) or []
        ):
            uid = getattr(peer, "user_id", None)
            if not uid:
                continue
            uid = int(uid)
            if uid == me_id or uid in seen:
                continue
            seen.add(uid)
            peer_ids.append(uid)

        total = len(peer_ids)
        chosen = peer_ids if limit <= 0 else peer_ids[:limit]
        self._folder_ids[account_id] = set(chosen)

        chats = []
        for uid in chosen:
            name = str(uid)
            try:
                entity = await client.get_entity(uid)
                name = (
                    " ".join(
                        part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part
                    ).strip()
                    or getattr(entity, "username", None)
                    or str(uid)
                )
            except Exception:
                pass
            chats.append({"id": uid, "name": name})
        if state:
            state.folder_chats = chats
            if total == 0:
                state.folder_hint = (
                    f"Папка «{REPLY_FOLDER_TITLE}» создана. Откройте этот аккаунт в Telegram "
                    "и перетащите туда личные чаты — бот ответит только им."
                )
            elif limit and total > limit:
                state.folder_hint = (
                    f"В папке {total} чатов, лимит {limit}. Отвечаем первым {limit}."
                )
            else:
                state.folder_hint = ""

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _should_reply(sender) -> bool:
        if sender is None or not isinstance(sender, User):
            return False
        if sender.bot or sender.is_self:
            return False
        if sender.id in SKIP_USER_IDS:
            return False
        return True


def _dialog_filter_title(filt) -> str:
    raw = getattr(filt, "title", "") or ""
    if hasattr(raw, "text"):
        raw = raw.text
    return str(raw).strip()
