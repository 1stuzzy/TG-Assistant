"""
Живой диалог без биографии: эмоция + один уместный вопрос.
Личность и факты о себе берутся только из промта персонажа.
"""
from __future__ import annotations

import json
import logging
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR, settings
from app.services.character_store import is_female
from app.services.world_context import clock as world_clock, wants_news, wants_time, wants_weather

logger = logging.getLogger(__name__)

DEFAULT_SUFFIX = (
    "Живой диалог: коротко, как в личке, с реакцией. Можно хаха/ну/мм. "
    "Сначала ответь на сказанное. За раз максимум один вопрос, своими словами, не анкетой. "
    "Про себя — только из личности в этом промте, коротко и последовательно, "
    "без списка фактов и без новой биографии. Это один чат с одним человеком. Не ассистент. "
    "Эмоции по разговору: тяжело — мягко; шутка — можно ржать; коротко написал — не души. "
    "Иногда достаточно реакции без вопроса."
)

_QUESTION = re.compile(r"[?？]|^(?i)\s*(а\s+)?(ты|как|что|чем|кто|где|когда|какой|какая|какие|зачем|почему)\b")
_GREET_ONLY = re.compile(r"(?i)^\s*(привет|приветик|хай|ку|здаров|здравствуй(те)?|йо)\s*[.!?…)]*\s*$")
_BOT_ASK = re.compile(r"(?i)(ты бот|ты ии|нейросет|странно обща|как бот)")
_HOW_ARE_YOU = re.compile(
    r"(?i)("
    r"(?<![а-яё])ты как(?!\s*-?\s*то\b)(?!\s+странн)"
    r"|как ты(?!\s+странн)(?!\s+себя)"
    r"|как дела|как делишки|\bделишки\b|"
    r"чё как|че как|как самочувствие|как жизнь|\bкак оно\b"
    r")"
)
_GET_ACQ = re.compile(
    r"(?i)\b(знакомимся|познаком(имся|иться)|давай знаком|будем знаком)"
)
_CITY_ASK = re.compile(
    r"(?i)(от\s*куда|откуда|из какого города|какой город|ты (сама |сам )?откуда|ты (сама |сам )?от\s*куда)"
)
_ABOUT_SELF = re.compile(
    r"(?i)(расскажи о себе|о себе расскаж|ты кто такая|ты кто такой|\bкто ты\b|что ты за)"
)
_NAME_ASK = re.compile(
    r"(?i)(как( тебя)? зовут|тво[её] имя|ты кто по имени)"
)
_DAIVINCHIK = re.compile(
    r"(?i)("
    r"дайвинчик|давинчик|дай\s*винчик|винчик|"
    r"леонардо\s*дай\s*винчик|леонардо\s*да\s*винчик|"
    r"daivinchik|davinchik|"
    r"\bдв\b|\bлео\b"
    r")"
)


_PACK_MARKERS = ("human_rules.json", "topics.json", "emotions.json", "dialogues.jsonl", "slang.json")
_FALLBACK_SHOTS = (("привет", "привет)"), ("ты бот?", "лол с чего"))


def _has_pack(path: Path) -> bool:
    return any((path / name).is_file() for name in _PACK_MARKERS)


def _load_json(path: Path, fallback):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Не прочитан %s: %s", path.name, exc)
    return fallback


def _pack_dir() -> Path:
    data_root = Path(settings.accounts_file).resolve().parent
    candidates = [
        Path(getattr(settings, "conversation_pack_dir", "") or ""),
        data_root / "rules",
        BASE_DIR / "data" / "rules",
        BASE_DIR / "models" / "Model" / "rules",
        BASE_DIR / "models" / "Model",
    ]
    seen: set[Path] = set()
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        if _has_pack(path):
            return path
    return (data_root / "rules").resolve()


def _load_shots(path: Path, limit: int = 20) -> list[tuple[str, str]]:
    shots: list[tuple[str, str]] = []
    if not path.is_file():
        return shots
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Не прочитан %s: %s", path.name, exc)
        return shots
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        user = assistant = ""
        for item in row.get("messages") or []:
            role = item.get("role")
            content = (item.get("content") or "").strip()
            if not content:
                continue
            if role == "user" and not user:
                user = content
            elif role == "assistant" and user:
                assistant = content
                break
        if user and assistant:
            shots.append((user, assistant))
        if len(shots) >= limit:
            break
    return shots


@lru_cache(maxsize=1)
def _pack() -> dict:
    root = _pack_dir()
    rules = _load_json(root / "human_rules.json", {})
    emotions = _load_json(root / "emotions.json", {})
    topics_file = _load_json(root / "topics.json", {})
    slang = _load_json(root / "slang.json", {})
    sit_file = _load_json(root / "situations.json", {})
    shots = _load_shots(root / "dialogues.jsonl")
    compiled = []
    for item in emotions.get("rules") or []:
        try:
            compiled.append((item["id"], re.compile(item["pattern"])))
        except (KeyError, re.error):
            continue
    situations = []
    for item in sit_file.get("rules") or []:
        try:
            situations.append((re.compile(item["pattern"]), (item.get("hint") or "").strip()))
        except (KeyError, re.error, TypeError):
            continue
    always = [str(x).strip() for x in (rules.get("always") or []) if str(x).strip()]
    never = [str(x).strip() for x in (rules.get("never") or []) if str(x).strip()]
    suffix_bits = [(rules.get("system_suffix") or DEFAULT_SUFFIX).strip()]
    if always:
        suffix_bits.append("Важно: " + "; ".join(always) + ".")
    if never:
        suffix_bits.append("Нельзя: " + "; ".join(never) + ".")
    glossary = (slang.get("glossary") or "").strip()
    if glossary:
        suffix_bits.append(glossary)
    logger.info(
        "Пакет диалога: %s (rules=%s topics=%s emotions=%s shots=%s slang=%s sit=%s)",
        root,
        bool(rules),
        len(topics_file.get("topics") or []),
        len(compiled),
        len(shots),
        bool(glossary),
        len(situations),
    )
    return {
        "dir": str(root),
        "suffix": " ".join(suffix_bits).strip(),
        "states": emotions.get("states") or {},
        "emotion_rules": compiled,
        "topics": list(topics_file.get("topics") or []),
        "stages": topics_file.get("stages") or {"ice": [0, 4], "warm": [5, 12], "close": [13, 999]},
        "shots": shots,
        "situations": situations,
    }


def human_suffix() -> str:
    return _pack()["suffix"]


def dialogue_shots() -> tuple[tuple[str, str], ...]:
    shots = _pack().get("shots") or []
    return tuple(shots) if shots else _FALLBACK_SHOTS


def _last_user(history: list[dict]) -> str:
    for item in reversed(history or []):
        if item.get("role") == "user":
            return (item.get("content") or "").strip()
    return ""


def _user_turns(history: list[dict]) -> int:
    return sum(1 for item in (history or []) if item.get("role") == "user")


def _blob(history: list[dict]) -> str:
    return " ".join((item.get("content") or "") for item in (history or [])).lower()


def _mood(text: str) -> str:
    pack = _pack()
    raw = text or ""
    if len(raw) <= 8 and not _QUESTION.search(raw):
        return "cool"
    for mood_id, pattern in pack["emotion_rules"]:
        if pattern.search(raw):
            return mood_id
    return "warm" if len(raw) > 40 else "curious"


def _stage(turns: int) -> str:
    stages = _pack()["stages"]
    for name, bounds in stages.items():
        if not isinstance(bounds, list) or len(bounds) < 2:
            continue
        if bounds[0] <= turns <= bounds[1]:
            return name
    if turns <= 4:
        return "ice"
    if turns <= 12:
        return "warm"
    return "close"


def _covered(history: list[dict]) -> set[str]:
    blob = _blob(history)
    found: set[str] = set()
    for topic in _pack()["topics"]:
        keys = topic.get("keywords") or []
        if any(str(key).lower() in blob for key in keys if key):
            found.add(topic["id"])
    return found


def _match_asked(text: str) -> Optional[dict]:
    low = (text or "").lower()
    if not _QUESTION.search(text or ""):
        return None
    for topic in _pack()["topics"]:
        if any(str(key).lower() in low for key in (topic.get("keywords") or []) if key):
            return topic
    return None


def _related(text: str, covered: set[str]) -> Optional[dict]:
    low = (text or "").lower()
    for topic in _pack()["topics"]:
        if topic["id"] in covered:
            continue
        if any(str(key).lower() in low for key in (topic.get("keywords") or []) if key):
            return topic
    return None


def _pick_topic(history: list[dict], last: str, mood: str) -> Optional[dict]:
    if mood in {"supportive", "tired", "cool", "annoyed"}:
        return None
    if random.random() < 0.38:
        return None
    turns = _user_turns(history)
    stage = _stage(turns)
    covered = _covered(history)
    related = _related(last, covered)
    if related and (not related.get("heavy") or stage == "close"):
        return related
    if related and related.get("heavy") and stage != "close":
        related = None
    pool = [
        t
        for t in _pack()["topics"]
        if t["id"] not in covered
        and t.get("stage") == stage
        and not (t.get("heavy") and stage != "close")
    ]
    if not pool:
        pool = [
            t
            for t in _pack()["topics"]
            if t["id"] not in covered and not t.get("heavy")
        ]
    return random.choice(pool) if pool else None


def _ask_hint(topic: dict) -> str:
    variants = [v for v in (topic.get("ask") or []) if v]
    sample = random.choice(variants) if variants else topic.get("label") or ""
    label = topic.get("label") or "это"
    return (
        f"Можно одним живым вопросом затронуть «{label}». "
        f"Своими словами, в духе: {sample} — не копируй дословно и не как анкета."
    )


def _already_greeted(history: list[dict]) -> bool:
    for item in reversed(history or []):
        if item.get("role") != "assistant":
            continue
        content = (item.get("content") or "").strip()
        if _GREET_ONLY.match(content):
            return True
        if len(content) <= 24 and re.search(r"(?i)\b(привет|хай|ку|здаров|йо)\b", content):
            return True
        return False
    return False


def turn_hint(
    history: list[dict],
    last_user: Optional[str] = None,
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    text = (last_user if last_user is not None else _last_user(history)).strip()
    mood = _mood(text)
    bits = []
    greeted = _already_greeted(history)
    try:
        clk = world_clock(city)
        part = clk.part
    except Exception:
        clk = None
        part = ""

    if _BOT_ASK.search(text):
        bits.append("Отшутись коротко, не читай лекцию и не признавайся что бот.")
        bits.append("Про себя — только из промта.")
        return " ".join(bits)

    if wants_time(text):
        if clk:
            bits.append(
                f"Он спросил про время. Сейчас {clk.time_line}, {clk.part}. "
                "Скажи примерно своими словами (типа «часа два ночи»), не как часы с минутами."
            )
        else:
            bits.append("Он спросил про время. Ответь примерно, коротко.")
        return " ".join(bits)

    if wants_news(text):
        bits.append(
            "Он про новости. Одно свежее из блока «сейчас» своими словами, не дайджестом и не списком."
        )
        return " ".join(bits)

    if wants_weather(text):
        bits.append("Он про погоду. Одна короткая реплика из блока «сейчас», не сводка.")
        return " ".join(bits)

    if _NAME_ASK.search(text):
        who = (name or "").strip() or "имя из промта"
        rod = "женском (рада, пошла)" if is_female(gender) else "мужском"
        bits.append(
            f"Он спросил как зовут. Ответь коротко: {who}. О себе в {rod} роде. "
            "Не модель, не Google, не Gemma. Не копируй подсказку."
        )
        return " ".join(bits)

    if _GREET_ONLY.match(text):
        if greeted:
            bits.append("Уже поздоровались. Не пиши привет снова. Коротко отозвись, без представления.")
        else:
            bits.append("Одно короткое приветствие, без точки в конце как у бота, без «как дела», без представления.")
        if part == "ночь":
            bits.append("Сейчас ночь — не пиши доброе утро.")
        return " ".join(bits)

    if _DAIVINCHIK.search(text):
        bits.append(
            "Он про бот знакомств в телеге (дв/винчик). "
            "Если это привет оттуда — коротко: тоже оттуда. "
            "О себе в роде из промта: девушка — не «рад». "
            "Не копируй эту подсказку. Не пиши «давай» просто так. Не про художника."
        )
        if greeted:
            bits.append("Не здоровайся повторно.")
        return " ".join(bits)

    if _GET_ACQ.search(text):
        bits.append(
            "Он предлагает познакомиться. Коротко согласись («давай» / «ну давай»), "
            "можно спросить откуда или как зовут. Не пиши «познакомиться — это норм»."
        )
        return " ".join(bits)

    if _CITY_ASK.search(text):
        where = (city or "").strip() or "город из промта"
        bits.append(
            f"Он спросил откуда ты. Коротко назови город: {where}. Потом спроси его. Не «оттуда»."
        )
        return " ".join(bits)

    if _ABOUT_SELF.search(text):
        bits.append(
            "Он просит рассказать о себе. Одна-две короткие фразы из карточки в промте. "
            "Не говори про обучение, развитие и нейросеть. Если в промте мало фактов — коротко и спроси его."
        )
        return " ".join(bits)

    if _HOW_ARE_YOU.search(text):
        if greeted:
            bits.append("Уже поздоровались секунду назад. ЗАПРЕЩЕНО писать привет/хай ещё раз.")
        bits.append(
            "Он спросил как дела. Одна короткая фраза: «да норм» / «тоже ок». "
            "Не выдумывай утро, работу, кошмары и распорядок, если этого нет в промте. "
            "Максимум один короткий вопрос. Без абзаца, без 😊, без двух вопросов."
        )
        if part == "ночь":
            bits.append("Сейчас ночь — не рассказывай про рабочий день, не называй часы.")
        elif part:
            bits.append(f"Сейчас {part} — учитывай, не называй часы.")
        return " ".join(bits)

    for sit_re, sit_hint in _pack().get("situations") or []:
        if sit_re.search(text):
            bits.append(sit_hint)
            bits.append("Не копируй подсказку.")
            return " ".join(bits)

    asked = _match_asked(text)
    if asked or _QUESTION.search(text):
        bits.append(
            "Собеседник спросил тебя. Ответь коротко из личности в промте, "
            "не выдумывай другую биографию и не перечисляй факты списком."
        )
        if mood not in {"supportive", "tired", "cool"} and random.random() < 0.55:
            bits.append("Потом можно один встречный вопрос по теме, своими словами.")
        if greeted:
            bits.append("Не здоровайся повторно.")
        bits.append("Опирайся на память этого чата: не переспрашивай то, что он уже рассказывал.")
        return " ".join(bits)

    topic = _pick_topic(history, text, mood)
    bits.append("Сначала живая реакция на его фразу, 1–2 коротких предложения.")
    if greeted:
        bits.append("Не пиши привет — вы уже поздоровались.")
    if part == "ночь":
        bits.append("Сейчас ночь: можно быть спокойнее, не бодрый дневной тон.")
    if topic:
        bits.append(_ask_hint(topic))
    else:
        bits.append("Вопрос не обязателен — можно просто отозваться.")
    bits.append("Не повторяй свой прошлый ответ. Не переспрашивай то, что он уже сказал.")
    bits.append("Опирайся на память этого чата. Про себя — только из промта.")
    return " ".join(bits)
