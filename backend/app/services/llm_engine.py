"""
Инференс GGUF: локальный llama.cpp или удалённый воркер (OpenAI-compatible).
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_PERSONA = (
    "Ты живой человек в Telegram. Пиши коротко, как в личке. Не бот и не ассистент."
)

_SHOTS = (
    ("привет", "привет)"),
    ("как дела?", "да норм, сижу просто"),
    ("все норм ты как?", "тоже ок, туплю в телефон"),
    ("ты бот?", "лол с чего"),
)

_HOW_ARE_YOU = re.compile(
    r"(?i)\b(ты как|как ты|как дела|чё как|че как|как самочувствие)\b"
)
_ASK_BACK = re.compile(
    r"(?i)(а ты как|как дела\s*\??\s*$|а ты\s*\??\s*$|как ты\s*\??\s*$)"
)
_GREET_ONLY = re.compile(r"(?i)^\s*(привет|хай|ку|здаров|здравствуй(те)?|йо)\s*[.!?…)]*\s*$")

_HOW_ARE_YOU_REPLIES = (
    "да норм, сижу)",
    "тоже ок",
    "нормально, просто отдыхаю",
    "да ничего, туплю)",
)

HUMAN_FALLBACKS = (
    "хаха",
    "ну ты чего",
    "ща",
    "неа",
    "мм?",
    "лол",
)

_BOT_NOISE = re.compile(
    r"(?i)("
    r"чем я могу помочь|чем могу помочь|чем могу быть полез|"
    r"с удовольствием отвеч|"
    r"задавай(те)? вопросы|"
    r"я обычный пользователь|"
    r"рад(а)? тебя встретить|"
    r"обращайся|"
    r"если есть вопросы|"
    r"я здесь,? чтобы помочь|"
    r"буду рад(а)? помочь|"
    r"конечно,? я помогу|"
    r"всегда рад(а)? помочь|"
    r"чем я могу быть полезен|"
    r"задай(те)? (мне )?вопрос|"
    r"нет,? я не бот|"
    r"я не бот|"
    r"я не (нейросеть|ии|ассистент)|"
    r"я живой (человек|пользователь)"
    r")"
)

_AI_LEAK = re.compile(
    r"(?i)("
    r"искусственн\w+\s+интеллект|"
    r"языков\w+\s+модель|"
    r"нейросет|"
    r"чат-?бот|"
    r"\bя бот\b|"
    r"\bя ии\b|"
    r"как ии\b|"
    r"обучен[аы]?\s+на|"
    r"обучающ\w+\s+(набор|данных|корпус|текст)|"
    r"название модели|"
    r"машинного обучения|"
    r"данным из интернета|"
    r"переписк\w+ пользователей|"
    r"алгоритм\w+ машин|"
    r"не имею конкретного (имени|названия) модели"
    r")"
)

_BIO_DUMP = re.compile(
    r"(?i)("
    r"вам можно обращаться|"
    r"вот (некоторые )?подробности|"
    r"образ[еу] жизни|"
    r"давайте знакомиться|"
    r"некоторые подробности"
    r")"
)


def _fallback() -> str:
    return random.choice(HUMAN_FALLBACKS)


def _cleanup(text: str) -> str:
    text = (text or "").strip().strip('"«»“”')
    text = re.sub(r"\s*\[[^\]]+\]", "", text).strip()
    for prefix in (
        "Ассистент:",
        "Assistant:",
        "Бот:",
        "AI:",
        "ИИ:",
        "Ответ:",
    ):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :].strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if _AI_LEAK.search(text) or _BIO_DUMP.search(text):
        return ""
    if re.search(r"(?m)^\s*\d+[\.)]\s", text) or re.search(r"(?m)^\s*[-•]\s", text):
        first = text.split("\n", 1)[0].strip()
        if (not first) or first.endswith(":") or _AI_LEAK.search(first) or _BIO_DUMP.search(first):
            return ""
        text = first
    kept: list[str] = []
    for part in re.split(r"(?<=[.!?…])\s+", text):
        chunk = part.strip()
        if not chunk:
            continue
        if _BOT_NOISE.search(chunk) or _AI_LEAK.search(chunk) or _BIO_DUMP.search(chunk):
            continue
        kept.append(chunk)
    text = " ".join(kept).strip()
    if len(text) > 220:
        text = text[:220].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.U)


def _too_alike(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return len(shorter) >= 8 and shorter in longer


def _turn_hint(last_user: str) -> str:
    text = (last_user or "").strip()
    if re.search(r"(?i)\b(ты бот|ты ии|нейросет)\b", text):
        return "Отшутись, не признавайся что бот."
    if _HOW_ARE_YOU.search(text):
        return "Он спросил КАК У ТЕБЯ. Ответь про себя. Не спрашивай «а ты как?» — он это уже написал."
    if _GREET_ONLY.match(text):
        return "Коротко поприветствуй, без «как дела?» и без представления."
    if re.search(r"(?i)знаком", text):
        return "Коротко согласись познакомиться, без биографии."
    return "Ответь по сути его фразы, 1–2 коротких предложения. Не повторяй свой прошлый ответ."


def _fix_reply(text: str, last_user: str, prev_assistant: str) -> str:
    reply = (text or "").strip()
    if _too_alike(reply, prev_assistant):
        reply = ""
    if last_user and _HOW_ARE_YOU.search(last_user) and (not reply or _ASK_BACK.search(reply)):
        return random.choice(_HOW_ARE_YOU_REPLIES)
    if last_user and _GREET_ONLY.match(last_user) and (not reply or "как дела" in reply.lower()):
        return random.choice(("привет)", "хай", "дарова"))
    return reply


def _messages(history: list[dict], persona: Optional[str]) -> list[dict]:
    system = (persona or DEFAULT_PERSONA).strip()
    system = (
        system
        + " Отвечай только текстом сообщения. Смотри историю: не переспрашивай то, что собеседник уже сказал."
    )
    messages: list[dict] = [{"role": "system", "content": system[:900]}]
    for user, assistant in _SHOTS:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    last_user = ""
    for item in history[-8:]:
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content[:600]})
            if role == "user":
                last_user = content
    if last_user:
        for i in range(len(messages) - 1, 0, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] = messages[i]["content"] + "\n\n[" + _turn_hint(last_user) + "]"
                break
    return messages


def _chat_format_for(path: Path) -> Optional[str]:
    name = path.name.lower()
    if "qwen" in name:
        return "qwen"
    if "llama-3" in name or "llama3" in name:
        return "llama-3"
    if "gemma" in name:
        return "gemma"
    if "mistral" in name or "mixtral" in name:
        return "mistral-instruct"
    return None


def _remote_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


class LLMEngine:
    def __init__(self) -> None:
        self._llm = None
        self._path: Optional[Path] = None
        self._load_lock = asyncio.Lock()
        self._gen_lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=6.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
                trust_env=False,
            )
        return self._http

    @property
    def loaded_name(self) -> Optional[str]:
        return self._path.name if self._path else None

    def is_loaded(self, path: Path) -> bool:
        return self._llm is not None and self._path == path

    async def ensure_loaded(self, model_path: Path) -> None:
        model_path = Path(model_path).resolve()
        async with self._load_lock:
            if self.is_loaded(model_path):
                return
            logger.info("Загрузка локальной модели %s …", model_path.name)
            await asyncio.to_thread(self._load_sync, model_path)

    def _load_sync(self, model_path: Path) -> None:
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise RuntimeError(
                "Не установлен llama-cpp-python. В папке backend выполните:\n"
                "pip install llama-cpp-python"
            ) from exc

        if not model_path.exists():
            raise FileNotFoundError(f"Файл модели не найден: {model_path}")

        self._unload_sync()
        n_threads = settings.llm_n_threads
        cpu = os.cpu_count() or 4
        if n_threads <= 0:
            n_threads = cpu
        n_ctx = max(512, min(settings.llm_n_ctx, 2048))
        kwargs = dict(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_gpu_layers=settings.llm_n_gpu_layers,
            n_batch=512,
            use_mmap=True,
            use_mlock=False,
            logits_all=False,
            embedding=False,
            chat_format=_chat_format_for(model_path),
            verbose=False,
        )
        try:
            self._llm = Llama(**kwargs, n_threads_batch=n_threads, n_ubatch=512)
        except TypeError:
            self._llm = Llama(**kwargs)
        self._path = model_path
        logger.info(
            "Модель загружена: %s (chat_format=%s, n_ctx=%s, threads=%s, gpu_layers=%s)",
            model_path.name,
            getattr(self._llm, "chat_format", None) or _chat_format_for(model_path),
            n_ctx,
            n_threads,
            settings.llm_n_gpu_layers,
        )

    def _unload_sync(self) -> None:
        if self._llm is not None:
            del self._llm
            self._llm = None
            self._path = None
            gc.collect()

    async def generate(
        self,
        history: list[dict],
        persona: Optional[str] = None,
        *,
        remote_url: Optional[str] = None,
        remote_key: Optional[str] = None,
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> str:
        async with self._gen_lock:
            if remote_url:
                return await self._generate_remote(
                    history, persona, remote_url, remote_key or "", on_partial
                )
            if self._llm is None:
                raise RuntimeError("Локальная модель не загружена")
            return await asyncio.to_thread(self._generate_sync, history, persona, on_partial)

    def _complete(self, messages: list[dict], on_partial: Optional[Callable[[str], None]]) -> str:
        kwargs = dict(
            messages=messages,
            max_tokens=min(64, settings.llm_max_tokens or 64),
            temperature=0.82,
            top_p=0.9,
            stop=["</s>", "<|im_end|>", "<|eot_id|>", "<|endoftext|>"],
        )
        def _run(**extra) -> str:
            if on_partial:
                acc = ""
                for chunk in self._llm.create_chat_completion(**kwargs, **extra, stream=True):
                    delta = _chunk_delta(chunk)
                    if not delta:
                        continue
                    acc += delta
                    on_partial(acc)
                return acc
            result = self._llm.create_chat_completion(**kwargs, **extra)
            return result["choices"][0]["message"].get("content") or ""

        try:
            return _run(repeat_penalty=1.18)
        except TypeError:
            return _run()

    def _generate_sync(
        self,
        history: list[dict],
        persona: Optional[str],
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> str:
        messages = _messages(history, persona)
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        logger.info("Промт: сообщений=%s символов=%s", len(messages), prompt_chars)
        t0 = time.perf_counter()
        try:
            text = self._complete(messages, on_partial)
        except Exception as exc:
            logger.exception("Генерация упала: %s", exc)
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "привет")
            mini = [
                {
                    "role": "system",
                    "content": "Ты человек в Telegram. Ответь одной короткой фразой. Не говори, что ты бот или ИИ.",
                },
                {"role": "user", "content": last_user[:400]},
            ]
            try:
                text = self._complete(mini, on_partial)
            except Exception as exc2:
                logger.exception("Повторная генерация тоже упала: %s", exc2)
                return _fallback()
        cleaned = _cleanup(text)
        last_user = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
        prev_assistant = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "assistant"), "")
        cleaned = _fix_reply(cleaned, last_user, prev_assistant)
        elapsed = time.perf_counter() - t0
        logger.info("Локальная генерация %.1f сек, символов: %s", elapsed, len(cleaned))
        return cleaned or _fallback()

    async def _generate_remote(
        self,
        history: list[dict],
        persona: Optional[str],
        url: str,
        api_key: str,
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> str:
        messages = _messages(history, persona)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": min(96, settings.llm_max_tokens or 64),
            "temperature": 0.82,
            "top_p": 0.9,
        }
        t0 = time.perf_counter()
        base = _remote_base(url)
        logger.info("Запрос к удалённому API %s …", base)
        client = await self._http_client()
        text = ""
        last_err = "нет ответа"
        for attempt in range(1, 13):
            try:
                streamed = await self._remote_stream(client, base, headers, payload, on_partial)
                if streamed is not None:
                    text = streamed
                    break
                res = await client.post(
                    base + "/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if res.status_code == 503:
                    last_err = _remote_error_text(res)
                    logger.info("Удалённая модель не готова (%s), попытка %s", last_err, attempt)
                    await asyncio.sleep(5)
                    continue
                res.raise_for_status()
                data = res.json()
                text = data["choices"][0]["message"].get("content") or ""
                break
            except httpx.HTTPStatusError as exc:
                last_err = _remote_error_text(exc.response)
                if exc.response is not None and exc.response.status_code == 503 and attempt < 12:
                    await asyncio.sleep(5)
                    continue
                raise ValueError(last_err) from exc
        else:
            raise ValueError(
                last_err
                or "На удалённом ПК модель ещё не готова. В окне start.bat дождитесь строки «Готово»."
            )
        cleaned = _cleanup(text)
        last_user = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
        prev_assistant = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "assistant"), "")
        cleaned = _fix_reply(cleaned, last_user, prev_assistant)
        elapsed = time.perf_counter() - t0
        logger.info("Удалённая генерация %.1f сек, символов: %s", elapsed, len(cleaned))
        return cleaned or _fallback()

    async def _remote_stream(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict,
        payload: dict,
        on_partial: Optional[Callable[[str], None]],
    ) -> Optional[str]:
        try:
            async with client.stream(
                "POST",
                base + "/v1/chat/completions",
                headers=headers,
                json={**payload, "stream": True},
            ) as res:
                if res.status_code >= 400:
                    await res.aread()
                    if res.status_code == 503:
                        raise httpx.HTTPStatusError(
                            "модель не готова",
                            request=res.request,
                            response=res,
                        )
                    return None
                ctype = (res.headers.get("content-type") or "").lower()
                if "text/event-stream" in ctype:
                    acc = ""
                    async for line in res.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = _chunk_delta(chunk)
                        if not delta:
                            continue
                        acc += delta
                        if on_partial:
                            on_partial(acc)
                    return acc
                raw = await res.aread()
                data = json.loads(raw)
                return data["choices"][0]["message"].get("content") or ""
        except httpx.HTTPStatusError:
            raise
        except Exception:
            logger.debug("Стрим удалённой модели недоступен, обычный запрос", exc_info=True)
            return None


def _remote_error_text(response: Optional[httpx.Response]) -> str:
    if response is None:
        return "Удалённый ПК не ответил"
    detail = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("error") or "")
        elif isinstance(data, str):
            detail = data
    except Exception:
        detail = (response.text or "")[:300]
    if response.status_code == 503:
        return detail or (
            "На удалённом ПК модель ещё не готова. "
            "В окне start.bat дождитесь строки «Готово» и напишите ещё раз."
        )
    return detail or f"Ошибка удалённого API: {response.status_code}"


def _chunk_delta(chunk: dict) -> str:
    try:
        choice = (chunk.get("choices") or [{}])[0]
    except (IndexError, TypeError, AttributeError):
        return ""
    delta = choice.get("delta") or {}
    if isinstance(delta, dict) and delta.get("content"):
        return str(delta["content"])
    if choice.get("text"):
        return str(choice["text"])
    message = choice.get("message") or {}
    if isinstance(message, dict) and message.get("content"):
        return str(message["content"])
    return ""
