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
from app.services.character_store import is_female, is_male, voice_from_persona
from app.services.conversation_director import (
    dialogue_shots,
    human_suffix,
    turn_hint as director_hint,
)
from app.services.world_context import snapshot as world_snapshot, clock as world_clock

logger = logging.getLogger(__name__)

DEFAULT_PERSONA = (
    "Ты живой человек в Telegram. Пиши коротко, как в личке. Не бот и не ассистент."
)

_HOW_ARE_YOU = re.compile(
    r"(?i)("
    r"(?<![а-яё])ты как(?!\s*-?\s*то\b)(?!\s+странн)"
    r"|как ты(?!\s+странн)(?!\s+себя)"
    r"|как дела|как делишки|\bделишки\b|"
    r"чё как|че как|как самочувствие|как жизнь|\bкак оно\b"
    r")"
)
_BOT_ASK = re.compile(
    r"(?i)(ты бот|ты ии|\bии\b|нейросет|странно обща|как бот|ты не жив)"
)
_GET_ACQ = re.compile(
    r"(?i)\b(знакомимся|познаком(имся|иться)|давай знаком|будем знаком)"
)
_CITY_ASK = re.compile(
    r"(?i)("
    r"от\s*куда|откуда|"
    r"из какого города|какой город|"
    r"ты (сама |сам )?откуда|"
    r"ты (сама |сам )?от\s*куда"
    r")"
)
_ASK_BACK = re.compile(
    r"(?i)(а ты как|как дела\s*\??\s*$|а ты\s*\??\s*$|как ты\s*\??\s*$)"
)
_GREET_ONLY = re.compile(r"(?i)^\s*(привет|приветик|хай|ку|здаров|здравствуй(те)?|йо)\s*[.!?…)]*\s*$")
_GREET_PREFIX = re.compile(
    r"(?i)^\s*(привет|приветик|хай|ку|здаров|здравствуй(те)?|йо)\s*[,.!?…:) )]+\s*"
)
_GREET_WORD = re.compile(r"(?i)\b(привет|приветик|хай|ку|здаров|здравствуй|йо)\b")
_BOTTY = re.compile(
    r"(?i)("
    r"относительно нормально|день выдался|насыщенн|"
    r"сразу начинать|свои штучки|просыпаться|"
    r"как дела у тебя|или ты просто|"
    r"рад(а)? слышать|чем могу|"
    r"не как на допросе|в процессе развития|"
    r"процесс(е)? обучен|я (ещё |еще )?учусь|"
    r"интересными людьми|настоящий романтик|"
    r"анкеты и мэтчи|мэтчи оттуда|"
    r"по-дружески|стараюсь отвечать|познакомиться\s*[-—]\s*это|"
    r"😊|🙂|😉|🤓|🌹|😜|💫|😄|😃|😁|"
    r"рад(а)? познакоми"
    r")"
)

_HOW_ARE_YOU_REPLIES = (
    "да норм)",
    "норм, ты как?",
    "тоже ок",
    "да ничего)",
)

HUMAN_FALLBACKS = (
    "хаха",
    "ну ты чего",
    "мм",
    "неа",
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
    r"не имею конкретного (имени|названия) модели|"
    r"процесс(е)? развития|"
    r"в процессе обучен|"
    r"я (ещё |еще )?обуча|"
    r"языков\w+\s+модел|"
    r"\bgoogle\b|\bгугл|"
    r"\bgemma\b|\bgemini\b|"
    r"\bchatgpt\b|\bopenai\b|"
    r"я\s+(—|-)?\s*(большая\s+)?языков|"
    r"модель от\s+"
    r")"
)

_ABOUT_SELF = re.compile(
    r"(?i)(расскажи о себе|о себе расскаж|ты кто такая|ты кто такой|кто ты\b|что ты за)"
)
_NAME_ASK = re.compile(
    r"(?i)(как( тебя)? зовут|тво[её] имя|ты кто по имени|имя\s*\??\s*$)"
)
_DV_HELLO = re.compile(
    r"(?i)((привет|приветик|хай|ку).{0,50}(дайвинчик|винчик|\bдв\b)|с дайвинчика|из дв|из винчика|я с дайвинчик|я с винчика)"
)
_HINT_LEAK = re.compile(
    r"(?i)("
    r"не как на допросе|из промта|не леонардо|"
    r"анкеты/?мэтчи|мэтчи оттуда|подсказк|"
    r"запрещено писать|1–2 коротких|"
    r"свежие заголовки|не называй дату|живи в этом моменте|"
    r"не дайджестом"
    r")"
)
_PAINTER_LEAK = re.compile(
    r"(?i)(да\s*винчи|mona lisa|мона лиза|джоконд|ренессанс|художник леонардо|картин)"
)
_DAIVINCHIK_TALK = re.compile(
    r"(?i)(дайвинчик|давинчик|винчик|дай\s*винчик|леонардо\s*дай\s*винчик|\bдв\b|\bлео\b|daivinchik)"
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
_MASC_SELF = re.compile(
    r"(?i)("
    r"\bя рад\b|\bрад познакоми|"
    r"\bя готов\b|\bя должен\b|"
    r"\bя пош[её]л\b|\bя заш[её]л\b|"
    r"\bя устал\b|\bпознакомился|"
    r"\bя парень\b|\bя мужчина\b"
    r")"
)
_FEM_SELF = re.compile(
    r"(?i)("
    r"\bя рада\b|\bрада познакоми|"
    r"\bя готова\b|\bя должна\b|"
    r"\bя пошла\b|\bя зашла\b|"
    r"\bя устала\b|\bпознакомилась|"
    r"\bя девушка\b|\bя женщина\b"
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
        if (
            _BOT_NOISE.search(chunk)
            or _AI_LEAK.search(chunk)
            or _BIO_DUMP.search(chunk)
            or _HINT_LEAK.search(chunk)
        ):
            continue
        kept.append(chunk)
    text = " ".join(kept).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    if len(parts) > 2:
        text = " ".join(parts[:2]).strip()
    if len(text) > 140:
        text = text[:140].rsplit(" ", 1)[0].rstrip(",;:") + "…"
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


def _turn_hint(
    history: list[dict],
    last_user: str,
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    try:
        return director_hint(history, last_user, city=city, name=name, gender=gender)
    except Exception:
        logger.debug("Не удалось собрать подсказку диалога", exc_info=True)
    text = (last_user or "").strip()
    if re.search(r"(?i)\b(ты бот|ты ии|нейросет)\b", text):
        return "Отшутись, не признавайся что бот."
    if _NAME_ASK.search(text) and name:
        return f"Он спросил имя. Ответь коротко: {name}."
    if _HOW_ARE_YOU.search(text):
        return "Он спросил как у тебя. Ответь про себя из промта. Вопрос в ответ не обязателен."
    if _GREET_ONLY.match(text):
        return "Коротко поприветствуй, без представления."
    if re.search(r"(?i)знаком", text):
        return "Коротко согласись познакомиться, без биографии."
    return "Ответь по сути его фразы, 1–2 коротких предложения. Не повторяй свой прошлый ответ."


def _only_question(text: str) -> bool:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if p.strip()]
    if not parts:
        return True
    return all(p.endswith("?") for p in parts)


def _already_greeted(history: list[dict]) -> bool:
    for item in reversed(history or []):
        if item.get("role") != "assistant":
            continue
        content = (item.get("content") or "").strip()
        if _GREET_ONLY.match(content):
            return True
        if len(content) <= 24 and _GREET_WORD.search(content):
            return True
        return False
    return False


def _question_count(text: str) -> int:
    return (text or "").count("?") + (text or "").count("？")


def _how_are_you_replies(city: Optional[str] = None) -> tuple[str, ...]:
    try:
        part = world_clock(city).part
    except Exception:
        part = ""
    if part == "ночь":
        return ("да ничего, ещё не сплю)", "норм, ты чего не спишь?", "тоже ок")
    if part in {"раннее утро", "утро"}:
        return ("да норм)", "норм, ты как?", "тоже ок")
    if part == "вечер":
        return ("да норм)", "норм, ты как?", "тоже ок")
    return _HOW_ARE_YOU_REPLIES


def _name_reply(name: Optional[str], gender: Optional[str]) -> str:
    who = (name or "").strip() or "секрет"
    if is_female(gender):
        return f"меня {who}"
    if is_male(gender):
        return f"я {who}"
    return who


def _dv_hello_reply() -> str:
    return random.choice(("приветик, давно вообще там сидишь?", "приветик", "привет, как давно в дв уже?"))


def _bot_reply() -> str:
    return random.choice(("нет", "неа", "окак, нет)"))


def _get_acq_reply() -> str:
    return random.choice(("давай, ты откуда?", "ну давай)", "ага, давай"))


def _city_mentioned(city: Optional[str], reply: str) -> bool:
    raw = (city or "").strip()
    if not raw:
        return True
    stem = re.sub(r"[аяьеюиыо]$", "", raw.lower().replace("ё", "е"))
    if len(stem) < 2:
        stem = raw.lower()
    return stem in (reply or "").lower().replace("ё", "е")


def _city_reply(city: Optional[str]) -> str:
    where = (city or "").strip()
    if where:
        return f"из {where}, а ты?"
    return "а ты откуда?"


def _gender_broken(reply: str, gender: Optional[str]) -> bool:
    if is_female(gender) and _MASC_SELF.search(reply or ""):
        return True
    if is_male(gender) and _FEM_SELF.search(reply or ""):
        return True
    return False


def fallback_reply(
    last_user: str,
    name: Optional[str] = None,
    gender: Optional[str] = None,
    city: Optional[str] = None,
) -> str:
    text = last_user or ""
    if _BOT_ASK.search(text):
        return _bot_reply()
    if _GET_ACQ.search(text):
        return _get_acq_reply()
    if _CITY_ASK.search(text):
        return _city_reply(city)
    if _NAME_ASK.search(text):
        return _name_reply(name, gender)
    if _DV_HELLO.search(text):
        return _dv_hello_reply()
    if _HOW_ARE_YOU.search(text):
        return random.choice(_how_are_you_replies(city))
    if _GREET_ONLY.match(text):
        return random.choice(("привет", "приветик"))
    if _ABOUT_SELF.search(text):
        if is_female(gender) and name:
            return f"я {name}, обычная жизнь. а ты?"
        if name:
            return f"я {name}. а ты чем занят?"
        return "ну что сказать, обычная жизнь. а ты?"
    return random.choice(("мммм", "ну", "аыхвахыв", "ага"))


def _fix_reply(
    text: str,
    last_user: str,
    history: list[dict],
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    reply = (text or "").strip()
    prev_assistant = next(
        (m.get("content") or "" for m in reversed(history or []) if m.get("role") == "assistant"),
        "",
    )
    if _too_alike(reply, prev_assistant):
        reply = ""
    greeted = _already_greeted(history)
    if greeted and reply:
        reply = _GREET_PREFIX.sub("", reply).strip()
        reply = re.sub(r"(?i)^да,\s*", "", reply).strip()
    if last_user and _GREET_ONLY.match(last_user):
        if not reply or len(reply) > 40 or _BIO_DUMP.search(reply) or _BOTTY.search(reply) or _gender_broken(reply, gender):
            return random.choice(("приветик", "привет", "привет)"))
        if _GREET_WORD.search(reply) and _question_count(reply):
            return random.choice(("приветик", "привет", "привет)"))
        if re.fullmatch(r"(?i)привет[.!]?$", reply or ""):
            return "привет)"
        return reply
    if last_user and _NAME_ASK.search(last_user):
        bad = (
            not reply
            or _AI_LEAK.search(reply)
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or len(reply) > 50
            or (bool(name) and name.lower() not in reply.lower())
        )
        if bad:
            return _name_reply(name, gender)
        return reply
    if last_user and _BOT_ASK.search(last_user):
        if (
            not reply
            or _AI_LEAK.search(reply)
            or _BOTTY.search(reply)
            or _BOT_NOISE.search(reply)
            or _HOW_ARE_YOU.search(reply)
            or _gender_broken(reply, gender)
            or len(reply) > 50
            or re.search(r"(?i)(не сплю|как дела|оттуда|дружеск)", reply)
        ):
            return _bot_reply()
        return reply
    if last_user and _GET_ACQ.search(last_user):
        if (
            not reply
            or _BOTTY.search(reply)
            or _BIO_DUMP.search(reply)
            or _HINT_LEAK.search(reply)
            or re.search(r"(?i)(это норм|познакомиться|дружеск)", reply)
            or len(reply) > 40
        ):
            return _get_acq_reply()
        return reply
    if last_user and _CITY_ASK.search(last_user):
        where = (city or "").strip()
        bad = (
            not reply
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _AI_LEAK.search(reply)
            or re.search(r"(?i)(оттуда|дружеск|стараюсь)", reply)
            or len(reply) > 50
            or (where and not _city_mentioned(where, reply))
        )
        if bad:
            return _city_reply(city)
        return reply
    if last_user and _HOW_ARE_YOU.search(last_user):
        too_long = len(reply) > 70
        too_many_q = _question_count(reply) > 1
        botty = bool(_BOTTY.search(reply) or _BIO_DUMP.search(reply))
        no_self = _only_question(reply) and _ASK_BACK.search(reply or "")
        greet_again = bool(_GREET_WORD.search(reply or "")) and not _GREET_WORD.search(last_user)
        if (not reply) or too_long or too_many_q or botty or no_self or greet_again or _gender_broken(reply, gender):
            return random.choice(_how_are_you_replies(city))
    if last_user and _DV_HELLO.search(last_user):
        if (
            not reply
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _AI_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or re.search(r"(?i)давай|познакоми", reply)
            or len(reply) > 40
        ):
            return _dv_hello_reply()
    if last_user and _ABOUT_SELF.search(last_user):
        if (
            not reply
            or _AI_LEAK.search(reply)
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or len(reply) > 120
        ):
            return fallback_reply(last_user, name, gender, city)
    if last_user and _DAIVINCHIK_TALK.search(last_user) and _PAINTER_LEAK.search(reply or ""):
        if re.search(r"(?i)что это|что такое|это про", last_user):
            return "бот в тг для знакомств, не да винчи)"
        return "ну да, винчик, знакомства"
    if not reply or _AI_LEAK.search(reply or "") or _gender_broken(reply or "", gender):
        return fallback_reply(last_user, name, gender, city)
    if reply and (_HINT_LEAK.search(reply) or _BOTTY.search(reply)):
        if last_user and _HOW_ARE_YOU.search(last_user):
            return random.choice(_how_are_you_replies(city))
        return fallback_reply(last_user, name, gender, city)
    if (
        reply
        and last_user
        and not re.search(r"(?i)(который час|сколько времени|какое число|какой сегодня)", last_user)
        and re.search(
            r"(?i)(сейчас (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)|"
            r"\d{1,2}:\d{2}|utc\+?\d)",
            reply,
        )
    ):
        reply = re.sub(
            r"(?i)\s*(сейчас )?(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)[^.]{0,40}",
            "",
            reply,
        ).strip()
        reply = re.sub(r"\b\d{1,2}:\d{2}\b", "", reply).strip(" ,.-")
        if not reply:
            return fallback_reply(last_user, name, gender, city)
    return reply


def _isolation_line(peer: Optional[str]) -> str:
    who = (peer or "").strip()
    if who:
        return (
            f"Сейчас ты пишешь только с {who}. Это отдельный человек и отдельный чат: "
            "не переноси факты из других переписок и не путай людей. "
            "Кто ты — только из промта выше, без новой биографии."
        )
    return (
        "Это один чат с одним человеком. Другие переписки тебе неизвестны. "
        "Кто ты — только из промта выше, без новой биографии."
    )


def _system_prompt(
    persona: Optional[str],
    peer: Optional[str] = None,
    memory: Optional[str] = None,
    world: Optional[str] = None,
) -> str:
    persona_text = (persona or DEFAULT_PERSONA).strip()
    memory_text = (memory or "").strip()
    world_text = (world or "").strip()
    suffix = (
        human_suffix()
        + " "
        + _isolation_line(peer)
        + " Отвечай только текстом сообщения. Смотри историю и память этого чата: не переспрашивай уже сказанное."
    )
    persona_max = 860
    memory_max = 480
    world_max = 280
    system_max = 1900
    suffix_min = 180
    persona_text = persona_text[:persona_max].rstrip()
    if world_text:
        world_block = world_text[:world_max]
    else:
        world_block = ""
    if memory_text:
        memory_block = "Память этого чата: " + memory_text[:memory_max]
    else:
        memory_block = ""
    used = len(persona_text)
    if world_block:
        used += 1 + len(world_block)
    if memory_block:
        used += 1 + len(memory_block)
    room = system_max - used - 1
    if room < suffix_min:
        overflow = suffix_min - room
        persona_text = persona_text[: max(400, len(persona_text) - overflow)].rstrip()
        used = len(persona_text)
        if world_block:
            used += 1 + len(world_block)
        if memory_block:
            used += 1 + len(memory_block)
        room = system_max - used - 1
    parts = [persona_text]
    if world_block:
        parts.append(world_block)
    if memory_block:
        parts.append(memory_block)
    parts.append(suffix[: max(0, room)].rstrip())
    return " ".join(p for p in parts if p).strip()


def _recent_history(history: list[dict], max_turns: int = 12, char_budget: int = 1400) -> list[dict]:
    recent: list[dict] = []
    used = 0
    for item in reversed(history or []):
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        content = content[:400]
        n = len(content)
        if recent and used + n > char_budget:
            break
        recent.append({"role": role, "content": content})
        used += n
        if len(recent) >= max_turns:
            break
    recent.reverse()
    return recent


def _resolve_voice(
    persona: Optional[str],
    name: Optional[str],
    gender: Optional[str],
    city: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    parsed_name, parsed_gender, parsed_city = voice_from_persona(persona)
    return name or parsed_name or None, gender or parsed_gender, city or parsed_city or None


def _messages(
    history: list[dict],
    persona: Optional[str],
    peer: Optional[str] = None,
    memory: Optional[str] = None,
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
) -> list[dict]:
    name, gender, city = _resolve_voice(persona, name, gender, city)
    last_user = next(
        (item.get("content") or "" for item in reversed(history or []) if item.get("role") == "user"),
        "",
    )
    try:
        world = world_snapshot(city, last_user)
    except Exception:
        logger.debug("Живой контекст недоступен", exc_info=True)
        world = ""
    messages: list[dict] = [{"role": "system", "content": _system_prompt(persona, peer, memory, world)}]
    for user, assistant in dialogue_shots():
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    last_user = ""
    for item in _recent_history(history):
        messages.append({"role": item["role"], "content": item["content"]})
        if item["role"] == "user":
            last_user = item["content"]
    if last_user:
        hint = _turn_hint(history, last_user, city, name, gender)
        for i in range(len(messages) - 1, 0, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] = messages[i]["content"] + "\n\n[" + hint + "]"
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
        peer: Optional[str] = None,
        memory: Optional[str] = None,
        city: Optional[str] = None,
        name: Optional[str] = None,
        gender: Optional[str] = None,
    ) -> str:
        async with self._gen_lock:
            if remote_url:
                return await self._generate_remote(
                    history,
                    persona,
                    remote_url,
                    remote_key or "",
                    on_partial,
                    peer,
                    memory,
                    city,
                    name,
                    gender,
                )
            if self._llm is None:
                raise RuntimeError("Локальная модель не загружена")
            return await asyncio.to_thread(
                self._generate_sync,
                history,
                persona,
                on_partial,
                peer,
                memory,
                city,
                name,
                gender,
            )

    def _reset_context(self) -> None:
        llm = self._llm
        if llm is None:
            return
        for name in ("reset", "reset_chat"):
            fn = getattr(llm, name, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    logger.debug("Сброс контекста модели (%s) не удался", name, exc_info=True)

    def _complete(self, messages: list[dict], on_partial: Optional[Callable[[str], None]]) -> str:
        self._reset_context()
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
        peer: Optional[str] = None,
        memory: Optional[str] = None,
        city: Optional[str] = None,
        name: Optional[str] = None,
        gender: Optional[str] = None,
    ) -> str:
        name, gender, city = _resolve_voice(persona, name, gender, city)
        messages = _messages(history, persona, peer, memory, city, name, gender)
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        logger.info(
            "Промт: peer=%s сообщений=%s символов=%s persona=%s name=%s gender=%s city=%s",
            (peer or "—")[:40],
            len(messages),
            prompt_chars,
            len(persona or ""),
            name or "—",
            gender or "—",
            (city or "—")[:40],
        )
        t0 = time.perf_counter()
        try:
            text = self._complete(messages, on_partial)
        except Exception as exc:
            logger.exception("Генерация упала: %s", exc)
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "привет")
            mini = [
                {
                    "role": "system",
                    "content": _system_prompt(persona, peer, memory),
                },
                {"role": "user", "content": last_user[:400]},
            ]
            try:
                text = self._complete(mini, on_partial)
            except Exception as exc2:
                logger.exception("Повторная генерация тоже упала: %s", exc2)
                last = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
                return fallback_reply(last, name, gender, city)
        cleaned = _cleanup(text)
        last_user = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
        cleaned = _fix_reply(cleaned, last_user, history, city, name, gender)
        elapsed = time.perf_counter() - t0
        logger.info("Локальная генерация %.1f сек, символов: %s", elapsed, len(cleaned))
        return cleaned or fallback_reply(last_user, name, gender, city)

    async def _generate_remote(
        self,
        history: list[dict],
        persona: Optional[str],
        url: str,
        api_key: str,
        on_partial: Optional[Callable[[str], None]] = None,
        peer: Optional[str] = None,
        memory: Optional[str] = None,
        city: Optional[str] = None,
        name: Optional[str] = None,
        gender: Optional[str] = None,
    ) -> str:
        name, gender, city = _resolve_voice(persona, name, gender, city)
        messages = await asyncio.to_thread(
            _messages, history, persona, peer, memory, city, name, gender
        )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": min(64, settings.llm_max_tokens or 64),
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
        cleaned = _fix_reply(cleaned, last_user, history, city, name, gender)
        elapsed = time.perf_counter() - t0
        logger.info("Удалённая генерация %.1f сек, символов: %s", elapsed, len(cleaned))
        return cleaned or fallback_reply(last_user, name, gender, city)

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
