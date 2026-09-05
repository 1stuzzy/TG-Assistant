"""
Долгая память одного Telegram-чата.

Факты и дайджест копятся в JSON и переживают рестарт/дни.
В промт уходит сжатая записка; свежий хвост диалога — отдельно из Telegram.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_NAME = re.compile(
    r"(?i)(?:"
    r"меня\s+зовут|зови\s+меня|мо[её]\s+имя|"
    r"я\s*[-—:]\s*"
    r")\s*([А-ЯЁA-Z][а-яёa-zA-Z\-']{2,24})\b"
)
_NAME_SOFT = re.compile(
    r"(?i)^(?:привет[,!]?\s+)?я\s+([А-ЯЁA-Z][а-яёa-zA-Z\-']{2,24})\b"
)
_AGE = re.compile(r"(?i)(?:мне|мне\s+уже)\s+(\d{1,2})\s*(?:лет|год(?:а|ов)?)\b")
_CITY = re.compile(
    r"(?i)(?:"
    r"живу\s+(?:в\s+)?|"
    r"я\s+из\s+|"
    r"родом\s+из\s+|"
    r"переехал(?:а)?(?:\s+в)?\s+(?:в\s+|из\s+)?|"
    r"(?:^|[,.]\s*)(?:я\s+)?(?:из|с)\s+"
    r")([А-ЯЁA-Zа-яё][А-ЯЁA-Zа-яё\-]{2,24})"
)
_WORK = re.compile(
    r"(?i)(?:работаю(?:\s+(?:как|в|на))?|я\s+(?:по\s+работе)?)\s+(.{3,48}?)(?:[.!?,\n]|$)"
)
_SELF = re.compile(
    r"(?i)\bя\s+(?:не\s+)?(?:"
    r"живу|работаю|учусь|люблю|хочу|устал(?:а)?|один|одна|"
    r"был(?:а)?|из|мне|инженер|программист|студент|водитель|врач"
    r")\b.{0,70}"
)
_NOISE = re.compile(
    r"(?i)^\s*("
    r"\[.*?\]|привет|приветик|хай|ку|ок|окей|лол|хаха|мм+|ща|"
    r"да|нет|ну|ага|угу|пон|ясно|вс[её]|норм|хорошо"
    r")\s*[)!.…]*\s*$"
)
_NAME_STOP = {
    "не", "на", "по", "за", "это", "тут", "там", "как", "какой", "какая", "какие",
    "что", "кто", "где", "когда", "же", "уже", "ещё", "еще", "просто", "тоже",
    "сейчас", "сегодня", "вчера", "завтра", "меня", "тебя", "его", "ее", "её",
    "из", "со", "для", "или", "если", "здесь", "вообще", "типа", "короче",
    "связывалась", "проверяю", "написала", "написал", "думал", "думала",
    "привет", "приветик", "хай",
}
_CITY_STOP = {
    "дома", "там", "тут", "здесь", "работы", "работы", "отпуска", "театра",
    "магазина", "интернета", "телефона", "тебя", "него", "нее", "неё",
}
_WORK_STOP = re.compile(
    r"(?i)^(как|в|на|по|не|да|нет|тут|там|сейчас|уже|просто)\b|http"
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_texts(history: list[dict]) -> list[str]:
    out: list[str] = []
    for item in history or []:
        if item.get("role") != "user":
            continue
        text = _clean(item.get("content") or "")
        if text:
            out.append(text)
    return out


def _pair_digest_lines(history: list[dict], skip_tail: int = 8, limit: int = 10) -> list[str]:
    """Сжатые моменты из более старой части диалога."""
    items = [
        {"role": m.get("role"), "content": _clean(m.get("content") or "")}
        for m in (history or [])
        if m.get("role") in {"user", "assistant"} and _clean(m.get("content") or "")
    ]
    if len(items) <= skip_tail:
        return []
    older = items[:-skip_tail]
    lines: list[str] = []
    i = 0
    while i < len(older) and len(lines) < limit:
        cur = older[i]
        text = cur["content"]
        if cur["role"] == "user" and not _NOISE.match(text) and len(text) >= 12:
            snippet = text[:70].rstrip(" ,;")
            nxt = older[i + 1]["content"][:50] if i + 1 < len(older) and older[i + 1]["role"] == "assistant" else ""
            if nxt and not _NOISE.match(nxt):
                lines.append(f"он: «{snippet}» → «{nxt.rstrip(' ,;')}»")
            else:
                lines.append(f"он: «{snippet}»")
        i += 1
    return lines[-limit:]


def _is_good_name(token: str) -> bool:
    low = (token or "").strip().lower().replace("ё", "е")
    if len(low) < 3 or low in _NAME_STOP:
        return False
    if not re.match(r"^[А-ЯЁA-Z]", token or ""):
        return False
    if re.search(r"(?i)(л[аи]сь|аю|ает|ите|ешь|ишь)$", low):
        return False
    return True


def _facts_from_text(text: str) -> list[str]:
    found: list[str] = []
    age = _AGE.search(text)
    if age:
        years = int(age.group(1))
        if 14 <= years <= 80:
            found.append(f"{years} лет")
    for city in _CITY.finditer(text):
        place = city.group(1).strip(" .,!")
        if place.lower().replace("ё", "е") in _CITY_STOP:
            continue
        if len(place) >= 3:
            found.append(f"из {place}")
            break
    name = _NAME.search(text) or _NAME_SOFT.search(text)
    if name and _is_good_name(name.group(1)):
        found.append(f"зовётся {name.group(1)}")
    work = _WORK.search(text)
    if work:
        job = _clean(work.group(1)).rstrip(" .,!")
        if 3 <= len(job) <= 48 and not _WORK_STOP.search(job):
            found.append(f"работа: {job}")
    for hit in _SELF.finditer(text):
        snippet = _clean(hit.group(0)).rstrip(" .,!")
        if 10 <= len(snippet) <= 80 and not _NOISE.match(snippet):
            low = snippet.lower()
            if any(x in low for x in ("зовут", "как дела", "привет")):
                continue
            found.append(snippet)
    return found


def _junk_fact(fact: str) -> bool:
    low = (fact or "").lower().replace("ё", "е")
    if not low or len(low) < 4:
        return True
    if low.startswith("зовется "):
        return not _is_good_name(fact.split(" ", 1)[-1])
    if low.startswith("из "):
        place = fact[3:].strip()
        return place.lower().replace("ё", "е") in _CITY_STOP or len(place) < 3
    if low.startswith("работа:"):
        return bool(_WORK_STOP.search(fact[7:].strip()))
    return False


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


def _recent_points(texts: list[str], n: int = 5) -> list[str]:
    points: list[str] = []
    for raw in reversed(texts):
        if _NOISE.match(raw) or len(raw) < 10:
            continue
        points.append(raw[:100].rstrip(" ,;"))
        if len(points) >= n:
            break
    points.reverse()
    return points


def _highlights_from_history(history: list[dict], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for text in _user_texts(history):
        if _NOISE.match(text) or len(text) < 18:
            continue
        if len(text) > 160:
            text = text[:157].rsplit(" ", 1)[0] + "…"
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out[-limit:]


def analyze_dialogue(
    history: list[dict],
    peer: Optional[str] = None,
    *,
    stored_facts: Optional[list[str]] = None,
    stored_topics: Optional[list[str]] = None,
    digest: Optional[list[str]] = None,
    highlights: Optional[list[str]] = None,
    limit: int = 720,
) -> str:
    """Короткая записка в system prompt: факты + дайджест + недавнее."""
    texts = _user_texts(history)
    turns = len(history or [])
    facts: list[str] = []
    seen: set[str] = set()
    for fact in list(stored_facts or []):
        if _junk_fact(fact):
            continue
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(fact)
    for text in texts:
        for fact in _facts_from_text(text):
            if _junk_fact(fact):
                continue
            key = fact.lower()
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    topics = list(dict.fromkeys(list(stored_topics or []) + _topic_labels(history)))
    recent = _recent_points(texts)
    bits: list[str] = []
    who = (peer or "").strip()
    if who:
        bits.append(f"Собеседник: {who}.")
    bits.append(f"В этом чате уже {turns} реплик в хвосте.")
    if facts:
        bits.append("Он говорил о себе: " + "; ".join(facts[:12]) + ".")
    if topics:
        bits.append("Уже поднимали: " + ", ".join(topics[:12]) + ".")
    if digest:
        bits.append("Из прошлых дней: " + " | ".join(digest[:8]) + ".")
    elif highlights:
        bits.append("Ранее он писал: «" + "»; «".join(highlights[-4:]) + "».")
    if recent:
        bits.append("Недавно: «" + "»; «".join(recent) + "».")
    bits.append(
        "Это долгая память чата. Опирайся на неё через дни. "
        "Не переспрашивай известное. Не путай с другими чатами."
    )
    text = " ".join(bits)
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


class ChatMemory:
    """Копит факты/дайджест по (аккаунт, чат) без TTL — переживает недели."""

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
        key = f"{account_id}:{int(chat_id)}"
        fresh_facts: list[str] = []
        seen_fresh: set[str] = set()
        for text in _user_texts(history):
            for fact in _facts_from_text(text):
                if _junk_fact(fact):
                    continue
                low = fact.lower()
                if low in seen_fresh:
                    continue
                seen_fresh.add(low)
                fresh_facts.append(fact)

        fresh_digest = _pair_digest_lines(history, skip_tail=10, limit=8)
        fresh_highlights = _highlights_from_history(history, limit=12)

        with self._lock:
            prev = self._data.get(key) or {}
            facts: list[str] = []
            for fact in list(prev.get("facts") or []) + fresh_facts:
                if _junk_fact(fact):
                    continue
                low = fact.lower()
                if low in {f.lower() for f in facts}:
                    continue
                # одно имя / один возраст / один город — последнее побеждает
                low_fact = fact.lower().replace("ё", "е")
                if low_fact.startswith("зовется "):
                    facts = [f for f in facts if not f.lower().replace("ё", "е").startswith("зовется ")]
                elif low_fact.startswith("из "):
                    facts = [f for f in facts if not f.lower().replace("ё", "е").startswith("из ")]
                elif low_fact.endswith(" лет"):
                    facts = [f for f in facts if not f.lower().endswith(" лет")]
                facts.append(fact)
            facts = facts[-24:]

            topics = list(
                dict.fromkeys(list(prev.get("topics") or []) + _topic_labels(history))
            )[-20:]

            digest: list[str] = []
            for line in list(prev.get("digest") or []) + fresh_digest:
                low = line.lower()
                if low in {d.lower() for d in digest}:
                    continue
                digest.append(line)
            digest = digest[-20:]

            highlights: list[str] = []
            for line in list(prev.get("highlights") or []) + fresh_highlights:
                low = line.lower()
                if low in {h.lower() for h in highlights}:
                    continue
                highlights.append(line)
            highlights = highlights[-24:]

            notes = analyze_dialogue(
                history,
                peer or prev.get("peer"),
                stored_facts=facts,
                stored_topics=topics,
                digest=digest,
                highlights=highlights,
            )
            self._data[key] = {
                "peer": (peer or prev.get("peer") or "").strip(),
                "facts": facts,
                "topics": topics,
                "digest": digest,
                "highlights": highlights,
                "notes": notes,
                "updated_at": _now_iso(),
                "turns_seen": max(int(prev.get("turns_seen") or 0), len(history or [])),
            }
            self._save_unlocked()

        return notes[:780]

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # подчистить старый шум при загрузке
                cleaned: dict[str, dict] = {}
                for key, row in raw.items():
                    if not isinstance(row, dict):
                        continue
                    facts = [f for f in (row.get("facts") or []) if isinstance(f, str) and not _junk_fact(f)]
                    cleaned[key] = {
                        **row,
                        "facts": facts[-24:],
                        "digest": list(row.get("digest") or [])[-20:],
                        "highlights": list(row.get("highlights") or [])[-24:],
                    }
                self._data = cleaned
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
