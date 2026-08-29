"""
Память одного Telegram-чата: разбор всей доступной переписки,
без смешивания людей и без чужой биографии агента.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_NAME = re.compile(
    r"(?i)(?:меня зовут|зови меня|я\s+)\s*([А-ЯЁA-Z][а-яёa-zA-Z\-']{1,24})\b"
)
_AGE = re.compile(r"(?i)мне\s+(\d{1,2})\s*(?:лет|год(?:а|ов)?)\b")
_CITY = re.compile(
    r"(?i)(?:живу|я из|родом из|переехал(?:а)?(?:\s+в)?)\s+(?:в\s+|из\s+)?([А-ЯЁA-Z][а-яёa-zA-Z\-]{2,24})"
)
_WORK = re.compile(
    r"(?i)(?:работаю(?:\s+(?:как|в))?|я\s+(?:по\s+работе)?)\s+(.{3,48}?)(?:[.!?,\n]|$)"
)
_SELF = re.compile(
    r"(?i)\bя\s+(?:не\s+)?(?:живу|работаю|учусь|люблю|хочу|устал(?:а)?|один|одна|был(?:а)?|из|мне)\b.{0,70}"
)
_NOISE = re.compile(r"(?i)^\s*(\[.*?\]|привет|хай|ку|ок|лол|хаха|мм|ща|да|нет|ну)\s*$")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _user_texts(history: list[dict]) -> list[str]:
    out: list[str] = []
    for item in history or []:
        if item.get("role") != "user":
            continue
        text = _clean(item.get("content") or "")
        if text:
            out.append(text)
    return out


def _facts_from_text(text: str) -> list[str]:
    found: list[str] = []
    age = _AGE.search(text)
    if age:
        found.append(f"{age.group(1)} лет")
    city = _CITY.search(text)
    if city:
        found.append(f"из {city.group(1)}")
    name = _NAME.search(text)
    if name and name.group(1).lower() not in {"не", "на", "по", "за", "это", "тут"}:
        found.append(f"зовётся {name.group(1)}")
    work = _WORK.search(text)
    if work:
        job = _clean(work.group(1)).rstrip(" .,!")
        if 3 <= len(job) <= 48 and "http" not in job.lower():
            found.append(f"работа: {job}")
    for hit in _SELF.finditer(text):
        snippet = _clean(hit.group(0)).rstrip(" .,!")
        if 8 <= len(snippet) <= 80:
            found.append(snippet)
    return found


def _topic_labels(history: list[dict]) -> list[str]:
    try:
        from app.services.conversation_director import _pack
        topics = _pack().get("topics") or []
    except Exception:
        topics = []
    blob = " ".join(_user_texts(history)).lower()
    labels: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        label = str(topic.get("label") or "").strip()
        if not label or label in seen:
            continue
        keys = [str(k).lower() for k in (topic.get("keywords") or []) if k]
        if keys and any(k in blob for k in keys):
            seen.add(label)
            labels.append(label)
    return labels


def _recent_points(texts: list[str], n: int = 4) -> list[str]:
    points: list[str] = []
    for raw in reversed(texts):
        if _NOISE.match(raw) or len(raw) < 10:
            continue
        points.append(raw[:90].rstrip(" ,;"))
        if len(points) >= n:
            break
    points.reverse()
    return points


def analyze_dialogue(history: list[dict], peer: Optional[str] = None, limit: int = 520) -> str:
    """Короткий разбор всего диалога: факты собеседника, темы, недавнее."""
    texts = _user_texts(history)
    turns = len(history or [])
    facts: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for fact in _facts_from_text(text):
            key = fact.lower()
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
            if len(facts) >= 10:
                break
        if len(facts) >= 10:
            break
    topics = _topic_labels(history)
    recent = _recent_points(texts)
    bits: list[str] = []
    who = (peer or "").strip()
    if who:
        bits.append(f"Собеседник: {who}.")
    bits.append(f"В этом чате уже {turns} реплик.")
    if facts:
        bits.append("Он говорил о себе: " + "; ".join(facts[:8]) + ".")
    if topics:
        bits.append("Уже поднимали: " + ", ".join(topics[:10]) + ".")
    if recent:
        bits.append("Недавно: «" + "»; «".join(recent) + "».")
    bits.append("Опирайся на это. Не переспрашивай известное. Не путай с другими чатами.")
    text = " ".join(bits)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


class ChatMemory:
    """Копит факты по (аккаунт, чат), чтобы не забыть при коротком окне модели."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._load()

    def remember(
        self,
        account_id: str,
        chat_id: int,
        history: list[dict],
        peer: Optional[str] = None,
    ) -> str:
        notes = analyze_dialogue(history, peer)
        key = f"{account_id}:{int(chat_id)}"
        fresh_facts: list[str] = []
        seen: set[str] = set()
        for fact in _facts_from_text(" ".join(_user_texts(history))):
            low = fact.lower()
            if low not in seen:
                seen.add(low)
                fresh_facts.append(fact)
        with self._lock:
            prev = self._data.get(key) or {}
            facts: list[str] = []
            for fact in list(prev.get("facts") or []) + fresh_facts:
                low = fact.lower()
                if low in {f.lower() for f in facts}:
                    continue
                facts.append(fact)
            facts = facts[-16:]
            topics = list(
                dict.fromkeys(list(prev.get("topics") or []) + _topic_labels(history))
            )[-16:]
            self._data[key] = {
                "peer": (peer or prev.get("peer") or "").strip(),
                "facts": facts,
                "topics": topics,
                "notes": notes,
            }
            self._save_unlocked()
        missing = [f for f in facts if f.lower() not in notes.lower()]
        if missing:
            notes = notes.replace(
                "Опирайся на это.",
                "Ранее ещё: " + "; ".join(missing[:6]) + ". Опирайся на это.",
            )
        return notes[:560]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, json.JSONDecodeError):
            logger.warning("Не прочитана память чатов %s", self._path)

    def _save_unlocked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("Не удалось сохранить память чатов", exc_info=True)
