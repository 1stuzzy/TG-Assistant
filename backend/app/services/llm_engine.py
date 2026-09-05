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
    nudge_hint as director_nudge_hint,
    prompt_compact,
    turn_hint as director_hint,
)
from app.services.world_context import (
    snapshot as world_snapshot,
    clock as world_clock,
    spoken_today,
    wants_day,
    wants_now,
)
from app.services.model_trace import model_trace

logger = logging.getLogger(__name__)

_STOP = ["</s>", "<|im_end|>", "<|eot_id|>", "<|endoftext|>"]
_SAMPLER_DROP = (
    ("dry_multiplier", "dry_base", "dry_allowed_length"),
    ("xtc_probability", "xtc_threshold"),
    ("min_p",),
    ("repeat_penalty",),
)


def _sampler_extras() -> dict:
    extra: dict = {}
    if settings.llm_min_p > 0:
        extra["min_p"] = settings.llm_min_p
    if settings.llm_repeat_penalty and abs(settings.llm_repeat_penalty - 1.0) > 0.001:
        extra["repeat_penalty"] = settings.llm_repeat_penalty
    if settings.llm_dry_multiplier > 0:
        extra["dry_multiplier"] = settings.llm_dry_multiplier
        extra["dry_base"] = settings.llm_dry_base
        extra["dry_allowed_length"] = settings.llm_dry_allowed_length
    if settings.llm_xtc_probability > 0:
        extra["xtc_probability"] = settings.llm_xtc_probability
        extra["xtc_threshold"] = settings.llm_xtc_threshold
    return extra


def _token_budget() -> int:
    return max(64, min(512, int(settings.llm_max_tokens or 160)))


def _chat_payload() -> dict:
    return {
        "max_tokens": _token_budget(),
        "temperature": settings.llm_temperature,
        "top_p": settings.llm_top_p,
        **_sampler_extras(),
    }


DEFAULT_PERSONA = (
    "Ты живой человек в Telegram. Пиши как в личке: свободно, по ситуации. Не бот и не ассистент."
)

_HOW_ARE_YOU = re.compile(
    r"(?i)("
    r"(?<![а-яё])ты как(?!\s*-?\s*то\b)(?!\s+странн)"
    r"|как ты(?!\s+странн)(?!\s+себя)"
    r"|как тебе\b|и как тебе|ну как тебе|"
    r"как дела|как делишки|\bделишки\b|"
    r"чё как|че как|как самочувствие|как жизнь|\bкак оно\b|"
    r"и как\s*\??\s*$|ну как\s*\??\s*$"
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
_LOOKING_FOR = re.compile(
    r"(?i)("
    r"кого ищешь|что ищешь|кого тут|"
    r"зачем (ты )?(здесь|тут)|"
    r"что (ты )?(тут|здесь) (ищешь|делаешь|забыл)|"
    r"кого хочешь найти|для чего здесь"
    r")"
)
_LAZY = re.compile(r"(?i)^(ага|угу|ну|мм+|ок|окей|ясно|пон|лол|хаха|аых)\s*[)!.…]*\s*$")
_ALREADY_SAID = re.compile(
    r"(?i)("
    r"я же (написал|сказала?|ответил)|"
    r"уже (написал|сказала?|ответил|писал)|"
    r"я (уже )?сказал|"
    r"ты не читаешь|прочитай (выше|ещё раз)"
    r")"
)
_NICE_MEET = re.compile(
    r"(?i)^(?:"
    r"(очень )?приятно( познакомиться)?|"
    r"взаимно|"
    r"рад(а)? познакомиться"
    r")\s*[)!.…]*\s*$"
)
_ASK_CITY_AGAIN = re.compile(
    r"(?i)("
    r"а ты откуда|ты откуда|а ты из какого|"
    r"сам откуда|сама откуда|а ты из\b"
    r")"
)
_SELF_CITY_CLAIM = re.compile(
    r"(?i)(?:^|[.!?]\s*|,\s*)(?:я\s+)?(?:тоже\s+)?(?:из|с)\s+([а-яёa-z\-]{3,})"
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
    r"умею отвечать|создавать тексты|в разных стилях|"
    r"не как на допросе|в процессе развития|"
    r"процесс(е)? обучен|я (ещё |еще )?учусь|"
    r"интересными людьми|настоящий романтик|"
    r"анкеты и мэтчи|мэтчи оттуда|"
    r"по-дружески|стараюсь отвечать|познакомиться\s*[-—]\s*это|"
    r"с удовольствием|"
    r"😊|🙂|😉|🤓|🌹|😜|💫|😄|😃|😁|"
    r"рад(а)? познакоми"
    r")"
)
_DV_HYPE = re.compile(
    r"(?i)("
    r"дайвинчик.{0,40}(космос|огонь|бомба|круто|кайф|вау|классн)|"
    r"(космос|огонь|бомба).{0,20}дайвинчик|"
    r"зарыл(ась|ся) в чат|"
    r"новеньк(ая|ий).{0,30}(тут|здесь|чат)|"
    r"возвращаюсь в строй"
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
_NAME_BACK = re.compile(
    r"(?i)(а тебя\s*\?*\s*$|а тебя как|а как тебя|тебя как зовут|а тво[её] имя)"
)
_TASK_ASK = re.compile(
    r"(?i)("
    r"напиши(те)? (мне )?(код|скрипт|программ|калькулятор|бот|сайт|функци|парсер)|"
    r"калькулятор|"
    r"на python|на питоне|на javascript|"
    r"```|"
    r"реши (задач|пример|уравнен)|"
    r"сделай домашк|"
    r"сочини (эссе|сочинен|стих)|"
    r"переведи (на англий|текст)"
    r")"
)
_CODE_DUMP = re.compile(
    r"(?i)(```|def\s+\w+\s*\(|import\s+\w+|while\s+true|print\s*\(|function\s+\w+|class\s+\w+|<!doctype)"
)
_ASSISTANT_BIO = re.compile(
    r"(?i)("
    r"умею отвечать|"
    r"создавать тексты|"
    r"в разных стилях|"
    r"я (ваш |твой )?ассистент|"
    r"я помощник|"
    r"генерир\w+ текст|"
    r"чем могу помочь|"
    r"задавай вопросы|"
    r"языков\w+\s+модель"
    r")"
)
_DV_HELLO = re.compile(
    r"(?i)("
    r"(привет|приветик|хай|ку).{0,80}(дайвинчик|давинчик|винчик|\bдв\b)|"
    r"с дайвинчика|из дайвинчика|с винчика|из винчика|"
    r"я с дайвинчик|я с винчика|я с дв\b|"
    r"из дв\b"
    r")"
)
_DIVING_LEAK = re.compile(
    r"(?i)("
    r"дайвинг(?!чик)|diving|scuba|подводн|акваланг|"
    r"ныря|нырн|плавал[аи]?|плавать|плавани|"
    r"в\s+море\s+ныр|бассейн.{0,20}ныр"
    r")"
)
_HINT_LEAK = re.compile(
    r"(?i)("
    r"не как на допросе|из промта|не леонардо|"
    r"анкеты/?мэтчи|мэтчи оттуда|подсказк|"
    r"запрещено писать|1–2 коротких|"
    r"свежие заголовки|не называй дату|живи в этом моменте|"
    r"не дайджестом|"
    r"не chatgpt|код и калькулятор не пишешь|ты человек в личке"
    r")"
)
_SEPIA_LEAD = re.compile(
    r"(?i)^(о+[,!.]?\s+|"
    r"понимаю[,.]?\s+|я понимаю[,.]?\s+|"
    r"звучит так(?:[,]?\s+будто)?[,.]?\s+|"
    r"это важно[,.]?\s+|честно говоря[,.]?\s+|"
    r"кстати[,.]?\s+|в любом случае[,.]?\s+|"
    r"главное(?:[,]?\s+что)?[,.]?\s+"
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
    text = text.replace("👋", "").strip()
    text = re.sub(r"[🤔😊🙂😉🤓🌹😜💫😄😃😁😅🙌]+", "", text)
    # Модели иногда вставляют деванагари/combining (съездँла)
    text = re.sub(r"[\u0900-\u097F\u0300-\u036F]", "", text)
    text = _SEPIA_LEAD.sub("", text, count=1).strip()
    if _CODE_DUMP.search(text):
        return ""
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
            or _ASSISTANT_BIO.search(chunk)
            or _BIO_DUMP.search(chunk)
            or _HINT_LEAK.search(chunk)
        ):
            continue
        kept.append(chunk)
    text = " ".join(kept).strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    if len(parts) > 3:
        text = " ".join(parts[:3]).strip()
    if len(text) > 280:
        text = text[:280].rsplit(" ", 1)[0].rstrip(",;:") + "…"
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
    persona: Optional[str] = None,
) -> str:
    try:
        return director_hint(history, last_user, city=city, name=name, gender=gender, persona=persona)
    except Exception:
        logger.debug("Не удалось собрать подсказку диалога", exc_info=True)
    text = (last_user or "").strip()
    if re.search(r"(?i)\b(ты бот|ты ии|нейросет)\b", text):
        return "Отшутись, не признавайся что бот."
    if _TASK_ASK.search(text):
        return "Он просит код или задачу как у ChatGPT. Не пиши код. Коротко отшутись."
    if (_NAME_ASK.search(text) or _NAME_BACK.search(text)) and name:
        return f"Он спросил имя. Ответь коротко: {name}."
    if wants_day(text):
        return "Он спросил про день. Ответь бытом из блока сегодня, не «ага»."
    if wants_now(text):
        return "Он спросил чем сейчас. Одна фраза из «сейчас» в блоке сегодня."
    if _HOW_ARE_YOU.search(text):
        return "Он спросил как у тебя. Коротко, можно одну деталь из блока сегодня."
    if _GREET_ONLY.match(text):
        return "Коротко поприветствуй, без представления."
    if re.search(r"(?i)знаком", text):
        return "Коротко согласись познакомиться, без биографии."
    return "Ответь по сути его фразы из своей жизни и характера. Не повторяй свой прошлый ответ."


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
        return ("да ничего, ещё не сплю. ты как?", "норм, ты чего не спишь?", "тихо, устала чуть")
    if part in {"раннее утро", "утро"}:
        return ("да норм, только раскачиваюсь. ты как?", "всё хорошо. как ты?", "норм, ты как?")
    if part == "вечер":
        return ("да ничего, работала. ты как?", "чуть устала, день был. у тебя как?", "всё норм. как день?")
    return ("да ничего, ты как?", "всё хорошо, работала. а у тебя?", "норм, чуть устала. ты как?")


def _task_refuse_reply() -> str:
    return random.choice((
        "лол я не по этому)",
        "неа, давай лучше как обычно",
        "всм код? да ну",
        "я ж не программист)",
    ))


def _name_reply(name: Optional[str], gender: Optional[str]) -> str:
    who = (name or "").strip() or "секрет"
    if is_female(gender):
        return f"меня {who}"
    if is_male(gender):
        return f"я {who}"
    return who


def _identity_pin(name: Optional[str], gender: Optional[str]) -> str:
    who = (name or "").strip()
    if is_female(gender):
        body = f"Тебя зовут {who}." if who else "Ты девушка из промта."
        return body + " Ты человек в личке, не ChatGPT. Код и калькулятор не пишешь — отшутись."
    if is_male(gender):
        body = f"Тебя зовут {who}." if who else "Ты парень из промта."
        return body + " Ты человек в личке, не ChatGPT. Код и калькулятор не пишешь — отшутись."
    if who:
        return f"Тебя зовут {who}. Ты человек в личке, не ChatGPT. Код не пишешь."
    return "Ты человек в личке, не ChatGPT. Код и калькулятор не пишешь."


def _dv_hello_reply() -> str:
    return random.choice((
        "приветик, давно тут?",
        "приветик) давно на винчике?",
        "ну приветик, как давно сидишь?",
    ))


def _bot_reply() -> str:
    return random.choice((
        "нет, с чего такие выводы",
        "нет) с чего вдруг",
        "неа, просто пишу как пишу",
    ))


def _get_acq_reply() -> str:
    return random.choice(("давай, ты откуда?", "ну давай)", "давай, как зовут?"))


def _city_stem(raw: str) -> str:
    text = (raw or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z\-]", "", text)
    stem = re.sub(r"(ска|цка|а|я|е|и|ы|у|ю|ой|ей|ом|ем)$", "", text)
    return stem if len(stem) >= 3 else text


def _cities_same(a: Optional[str], b: Optional[str]) -> bool:
    sa, sb = _city_stem(a or ""), _city_stem(b or "")
    if not sa or not sb:
        return False
    return sa == sb or sa.startswith(sb) or sb.startswith(sa)


def _extract_user_city(text: str) -> str:
    """Город, который собеседник назвал про себя (с/из …)."""
    m = re.search(
        r"(?i)(?:^|[.!?]\s*)(?:я\s+)?(?:из|с)\s+([а-яёa-z\-]{3,})",
        text or "",
    )
    if m:
        return m.group(1).strip(" .,!?")
    return ""


def _user_answered_city(text: str) -> bool:
    t = text or ""
    if re.search(r"(?i)(?:^|[.!?]\s*|,\s*)(?:я\s+)?(?:из|с)\s+[а-яёa-z\-]{3,}", t):
        return True
    return bool(re.search(r"(?i)^(с|из)\s+[а-яёa-z\-]{3,}", t.strip()))


def _city_mentioned(city: Optional[str], reply: str) -> bool:
    raw = (city or "").strip()
    if not raw:
        return True
    low = (reply or "").lower().replace("ё", "е")
    stem = _city_stem(raw)
    return bool(stem) and stem in low


def _asks_city_again(reply: str) -> bool:
    return bool(_ASK_CITY_AGAIN.search(reply or ""))


def _foreign_self_city(reply: str, city: Optional[str], last_user: str = "") -> bool:
    """Своим назвала чужой город (часто эхо собеседника)."""
    our = (city or "").strip()
    if not our:
        return False
    claimed = _SELF_CITY_CLAIM.findall(reply or "")
    if not claimed:
        return False
    user_city = _extract_user_city(last_user)
    for item in claimed:
        tok = (item or "").strip()
        if not tok or tok.lower() in {"тоже", "дома", "там", "тут"}:
            continue
        if _cities_same(tok, our):
            continue
        if user_city and _cities_same(tok, user_city):
            return True
        return True
    return False


def _city_from_form(city: Optional[str]) -> str:
    """Грубая форма «из X» без морфологии."""
    w = (city or "").strip()
    if not w:
        return ""
    low = w.lower().replace("ё", "е")
    if low.endswith("а") and not low.endswith(("ска", "цка")):
        return w[:-1] + ("ы" if not w[-2:].lower() in {"га", "ка", "ха", "жа", "ша", "ща", "ча"} else "и")
    if low.endswith("я"):
        return w[:-1] + "и"
    if low.endswith(("ск", "цк")):
        return w + "а"
    return w


def _city_reply(city: Optional[str]) -> str:
    where = (city or "").strip()
    if where:
        return f"из {_city_from_form(where)}, а ты?"
    return "а ты откуда?"


def _city_reply_back(city: Optional[str], last_user: str = "") -> str:
    where = (city or "").strip()
    form = _city_from_form(where)
    user_city = _extract_user_city(last_user)
    same = bool(where and user_city and _cities_same(where, user_city))
    if where:
        if same:
            return random.choice((
                f"тоже {where})",
                f"ого, тоже {where}",
                f"я тоже из {form}",
            ))
        return random.choice((
            f"я из {form})",
            f"из {form}",
            f"я из {form}",
        ))
    return "я тоже оттуда вроде)"


def _already_said_reply() -> str:
    return random.choice((
        "ой да, сорри)",
        "точно, вижу)",
        "ага, прочитала уже)",
        "да, ты писал)",
    ))


def _nice_meet_reply() -> str:
    return random.choice((
        "взаимно)",
        "мне тоже)",
        "приятно)",
        "и мне)",
    ))


_NOT_A_NAME = {
    "тоже", "тут", "дома", "просто", "сейчас", "там", "ещё", "еще",
    "недавно", "нормально", "жива", "живой", "из", "москва", "москвы",
}

_DUMMY_NAMES = ("лена", "катя")


def _foreign_self_name(reply: str, name: Optional[str]) -> bool:
    who = (name or "").strip().lower()
    text = reply or ""
    claimed = re.search(
        r"(?i)(?:меня зовут|меня\s+|я\s*[-—:]\s+)([а-яёa-z]{3,})",
        text,
    )
    if claimed:
        got = claimed.group(1).lower()
        if got not in _NOT_A_NAME and who and got != who:
            return True
    if who:
        for dummy in _DUMMY_NAMES:
            if dummy != who and re.search(rf"(?i)\b{dummy}\b", text):
                return True
        if re.match(rf"(?i)^{re.escape(name.strip())}\s*,", text):
            return True
    return False


def _gender_broken(reply: str, gender: Optional[str]) -> bool:
    if is_female(gender) and _MASC_SELF.search(reply or ""):
        return True
    if is_male(gender) and _FEM_SELF.search(reply or ""):
        return True
    return False


def _ask_back_reply(
    history: Optional[list],
    name: Optional[str],
    gender: Optional[str],
    city: Optional[str],
    last_user: str = "",
) -> str:
    prev = next(
        (m.get("content") or "" for m in reversed(history or []) if m.get("role") == "assistant"),
        "",
    )
    if _DAIVINCHIK_TALK.search(prev) or re.search(r"(?i)давно", prev):
        return random.choice((
            "недавно ещё, только осваиваюсь",
            "тоже недавно. тебе как тут?",
            "ну недавно, ещё смотрю как оно",
        ))
    if _HOW_ARE_YOU.search(prev):
        return random.choice(_how_are_you_replies(city))
    if _NAME_ASK.search(prev) or "зовут" in prev.lower():
        return _name_reply(name, gender)
    if _CITY_ASK.search(prev) or re.search(r"(?i)откуда", prev) or _user_answered_city(last_user):
        return _city_reply_back(city, last_user)
    return random.choice((
        "ну да, а у тебя?",
        "похож день) а ты чем?",
        "ага, понятно. ты сам как?",
    ))


def _looking_for_reply(gender: Optional[str]) -> str:
    if is_female(gender):
        return random.choice((
            "просто живого человека, не игры. а ты?",
            "познакомиться нормально. ты сам кого ищешь?",
            "без цирка, просто человека. а ты тут за чем?",
        ))
    return random.choice((
        "просто пообщаться нормально. а ты?",
        "человека, не анкету. ты сам зачем?",
    ))


_CLARIFY_ASK = re.compile(
    r"(?i)^(в отпуске|серь[её]зно|правда|это шутка|ты дома|не работаешь)\s*\??\s*$"
)
_META_CLARIFY = re.compile(
    r"(?i)^(?:"
    r"что\s+давно|"
    r"давно\s+что|"
    r"в\s+смысле|"
    r"о\s*ч[её]м|"
    r"про\s+что|"
    r"что\s+ты\s+(имеешь|имела)\s+в\s+виду|"
    r"что\s+ты\s+спросил[аи]?|"
    r"что\s+спрашиваешь|"
    r"уточни|"
    r"поясни"
    r")\s*[?？!.…)]*\s*$"
)
_SELF_HERE_DAYS = re.compile(
    r"(?i)("
    r"(я\s+)?(уже\s+)?(пару|несколько|два|три)?\s*(дня|дней|недел).{0,20}(тут|здесь)|"
    r"(тут|здесь).{0,20}(пару|несколько)\s*(дня|дней)|"
    r"давно\s+здесь\s+не\s+был|"
    r"как\s+тут\.?\s*а\s+ты"
    r")"
)
_DATING_CANNED = re.compile(
    r"(?i)("
    r"ты сам как тут оказался|"
    r"ты чаще сам пишешь первым|"
    r"да так, познакомиться\. а ты\?"
    r")"
)


def _clarify_reply(prev: str) -> str:
    if re.search(r"(?i)отпуск", prev or ""):
        return random.choice((
            "не совсем, просто день полегче)",
            "ну типа выходной сегодня",
            "ага, без офиса сегодня",
        ))
    if re.search(r"(?i)работ", prev or ""):
        return random.choice(("работаю да", "ну да, на работе", "ага"))
    return random.choice(("ну да)", "типа того", "ага"))


def _meta_clarify_reply(prev: str, history: Optional[list] = None) -> str:
    """Уточняет свой прошлый вопрос, а не отвечает на него за себя."""
    prev_l = (prev or "").lower()
    dv = _history_mentions_dv(history, prev)
    if re.search(r"(?i)давно", prev_l):
        if dv or re.search(r"(?i)(винчик|тут|здесь|сидишь)", prev_l):
            return random.choice((
                "ну тебя спрашиваю — давно тут сидишь?",
                "как давно на винчике)",
                "спрашиваю тебя, давно уже тут?",
            ))
        return random.choice((
            "ну тебя — давно тут?",
            "спрашиваю тебя)",
        ))
    if re.search(r"(?i)откуда", prev_l):
        return "ну тебя — откуда ты)"
    if re.search(r"(?i)зовут|имя", prev_l):
        return "как тебя зовут спрашиваю)"
    return random.choice((
        "ну тебя спрашиваю)",
        "уточняю — тебе вопрос)",
    ))


def _user_asked(text: str) -> bool:
    t = text or ""
    return bool(
        "?" in t
        or "？" in t
        or _ASK_BACK.search(t)
        or _LOOKING_FOR.search(t)
        or _NAME_ASK.search(t)
        or _NAME_BACK.search(t)
        or _HOW_ARE_YOU.search(t)
        or _ABOUT_SELF.search(t)
        or _CITY_ASK.search(t)
    )


def fallback_reply(
    last_user: str,
    name: Optional[str] = None,
    gender: Optional[str] = None,
    city: Optional[str] = None,
    history: Optional[list] = None,
    persona: Optional[str] = None,
) -> str:
    text = last_user or ""
    prev = next(
        (m.get("content") or "" for m in reversed(history or []) if m.get("role") == "assistant"),
        "",
    )
    if _BOT_ASK.search(text):
        return _bot_reply()
    if _TASK_ASK.search(text):
        return _task_refuse_reply()
    if _LOOKING_FOR.search(text):
        return _looking_for_reply(gender)
    if _ASK_BACK.search(text):
        return _ask_back_reply(history, name, gender, city, text)
    if _GET_ACQ.search(text):
        return _get_acq_reply()
    if _CITY_ASK.search(text):
        return _city_reply(city)
    if _NAME_ASK.search(text) or _NAME_BACK.search(text):
        return _name_reply(name, gender)
    if _DV_HELLO.search(text):
        return _dv_hello_reply()
    if _ALREADY_SAID.search(text):
        return _already_said_reply()
    if _NICE_MEET.search(text):
        return _nice_meet_reply()
    if _META_CLARIFY.search(text):
        return _meta_clarify_reply(prev, history)
    # уточнение к своей прошлой фразе — не сваливаться в «дайвинчик»
    if _CLARIFY_ASK.search(text):
        return _clarify_reply(prev)
    if wants_day(text):
        return spoken_today(city, persona=persona, gender=gender, kind="day")
    if wants_now(text):
        return spoken_today(city, persona=persona, gender=gender, kind="now")
    if _HOW_ARE_YOU.search(text):
        return random.choice(_how_are_you_replies(city))
    if _GREET_ONLY.match(text):
        return random.choice(("приветик)", "приветик, как день?"))
    if _ABOUT_SELF.search(text):
        where = (city or "").strip()
        if is_female(gender) and name:
            if where:
                return random.choice((
                    f"я {name}, из {where.lower()}. а ты?",
                    f"ну я {name}, работаю и живу своей жизнью. а тебе что интересно?",
                    f"{name} из {where.lower()}, а ты откуда?",
                ))
            return random.choice((
                f"я {name}. а тебе что интересно узнать?",
                f"ну я {name}, работаю и живу своей жизнью. а ты?",
            ))
        if name:
            return f"я {name}. а ты чем занят?"
        return "ну что сказать — живу, работаю. а ты?"
    if _user_asked(text):
        # Не подсовывать реплики про дайвинчик, если речь не про него
        if _history_mentions_dv(history, text):
            return random.choice((
                "ну недавно, ты сам как тут оказался?",
                "да так, познакомиться. а ты?",
                "пока просто болтаю. тебе как тут?",
            ))
        return random.choice((
            "ну да)",
            "типа того",
            "ага, а тебе?",
            "ну вроде",
        ))
    return random.choice(("мм", "ну ты чего", "хаха"))


def _history_mentions_dv(history: Optional[list], last_user: str = "") -> bool:
    if _DAIVINCHIK_TALK.search(last_user or "") or _DV_HELLO.search(last_user or ""):
        return True
    for item in history or []:
        if _DAIVINCHIK_TALK.search(item.get("content") or "") or _DV_HELLO.search(item.get("content") or ""):
            return True
    return False


def _dv_topic_leak(reply: str) -> bool:
    return bool(
        _DIVING_LEAK.search(reply or "")
        or _PAINTER_LEAK.search(reply or "")
        or _DV_HYPE.search(reply or "")
    )


def _dv_leak_fix(last_user: str, history: Optional[list]) -> str:
    if re.search(r"(?i)что это|что такое|это про", last_user or ""):
        return "бот в тг для знакомств, не дайвинг)"
    if _DV_HELLO.search(last_user or ""):
        return _dv_hello_reply()
    if _ASK_BACK.search(last_user or ""):
        return random.choice((
            "недавно ещё, только осваиваюсь",
            "тоже недавно. тебе как тут?",
            "ну недавно, ещё смотрю как оно",
        ))
    return "ну да, винчик — знакомства в тг, не спорт)"


def _is_test_mode(account_id: str = "", chat_id: str = "") -> bool:
    return (account_id or "") == "playground" or (chat_id or "") == "test"


def _log_confused(
    reason: str,
    *,
    last_user: str = "",
    raw: str = "",
    account_id: str = "",
    chat_id: str = "",
) -> None:
    logger.warning(
        "TEST confuse [%s] user=%r raw=%r",
        reason,
        (last_user or "")[:160],
        (raw or "")[:400],
    )
    try:
        model_trace.event(
            "error",
            account_id=account_id or "playground",
            chat_id=chat_id or "test",
            peer="тест",
            detail=f"confused:{reason}\nQ: {(last_user or '')[:240]}\nraw: {(raw or '')[:500]}",
        )
    except Exception:
        pass


def fallback_nudge(city: Optional[str] = None) -> str:
    try:
        part = world_clock(city).part
    except Exception:
        part = ""
    if part == "ночь":
        return random.choice(("не спишь ещё?", "ты куда)", "я ещё тут"))
    return random.choice((
        "ты куда)",
        "чё как там",
        "я тут",
        "ну ты это",
        "ещё живой?",
    ))


def _fix_reply(
    text: str,
    last_user: str,
    history: list[dict],
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
    persona: Optional[str] = None,
    *,
    nudge: bool = False,
    loose: bool = False,
    account_id: str = "",
    chat_id: str = "",
    raw_before: str = "",
) -> str:
    reply = (text or "").strip()
    reply = re.sub(r"(?i)\bхай\b", "приветик", reply)
    reply = re.sub(r"(?i)\s*,?\s*тоже оттуда\.?\s*", " ", reply)
    reply = re.sub(r"\s+", " ", reply).strip(" ,")
    prev_assistant = next(
        (m.get("content") or "" for m in reversed(history or []) if m.get("role") == "assistant"),
        "",
    )
    if _too_alike(reply, prev_assistant):
        reply = ""
    if _foreign_self_name(reply, name):
        reply = ""
    greeted = _already_greeted(history)
    if (greeted or nudge) and reply:
        reply = _GREET_PREFIX.sub("", reply).strip()
        reply = re.sub(r"(?i)\b(привет|приветик|хай)\b[!.]*", "", reply).strip(" ,")
        reply = re.sub(r"(?i)^да,\s*", "", reply).strip()
        if name and re.match(rf"(?i)^{re.escape(name.strip())}\s*,", reply):
            reply = ""

    def _swap(reason: str, canned: str) -> str:
        if loose:
            keep = (reply or raw_before or "").strip()
            _log_confused(
                reason,
                last_user=last_user,
                raw=keep or raw_before,
                account_id=account_id,
                chat_id=chat_id,
            )
            if keep and not _LAZY.match(keep) and not _dv_topic_leak(keep):
                return keep
            return f"[тест] не поняла ({reason}) — смотри консоль"
        return canned

    # Дайвинчик ≠ дайвинг: ловим утечку даже если слово было раньше в чате
    if reply and _dv_topic_leak(reply) and _history_mentions_dv(history, last_user):
        return _swap("dv_diving_leak", _dv_leak_fix(last_user, history))

    if nudge:
        if (
            not reply
            or _BOTTY.search(reply)
            or _AI_LEAK.search(reply)
            or _ASSISTANT_BIO.search(reply)
            or _HINT_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or len(reply) > 140
        ):
            return _swap("nudge", fallback_nudge(city))
        return reply
    if last_user and _DV_HELLO.search(last_user):
        if loose and reply and not (_AI_LEAK.search(reply) or _ASSISTANT_BIO.search(reply)):
            return reply
        return _dv_hello_reply()
    if last_user and _GREET_ONLY.match(last_user):
        if not reply or _BIO_DUMP.search(reply) or _BOTTY.search(reply) or _gender_broken(reply, gender):
            return _swap("greet", random.choice(("приветик)", "приветик, как день?")))
        if len(reply) > 80 and not loose:
            return random.choice(("приветик)", "приветик, как день?"))
        if _GREET_WORD.search(reply) and _question_count(reply) and not loose:
            return random.choice(("приветик)", "приветик, как день?"))
        if re.fullmatch(r"(?i)(привет|хай)[.!]?$", reply or ""):
            return "приветик)"
        return reply
    if last_user and _TASK_ASK.search(last_user):
        if (
            not reply
            or _CODE_DUMP.search(reply)
            or _ASSISTANT_BIO.search(reply)
            or _AI_LEAK.search(reply)
            or _BOTTY.search(reply)
            or (len(reply) > 140 and not loose)
        ):
            return _swap("task", _task_refuse_reply())
        return reply
    if last_user and (_NAME_ASK.search(last_user) or _NAME_BACK.search(last_user)):
        bad = (
            not reply
            or _AI_LEAK.search(reply)
            or _ASSISTANT_BIO.search(reply)
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or (len(reply) > 90 and not loose)
            or (bool(name) and name.lower() not in reply.lower())
        )
        if bad:
            return _swap("name", _name_reply(name, gender))
        return reply
    if last_user and _LOOKING_FOR.search(last_user):
        if (not reply) or _LAZY.match(reply) or _ASSISTANT_BIO.search(reply) or _BOTTY.search(reply) or _gender_broken(reply, gender):
            return _swap("looking", _looking_for_reply(gender))
        return reply
    if last_user and _ASK_BACK.search(last_user):
        prev_was_city = bool(
            _CITY_ASK.search(prev_assistant)
            or re.search(r"(?i)откуда", prev_assistant or "")
            or _user_answered_city(last_user)
        )
        bad = (
            not reply
            or (_LAZY.match(reply) and not loose)
            or _ASSISTANT_BIO.search(reply)
            or _only_question(reply)
            or _gender_broken(reply, gender)
            or _foreign_self_name(reply, name)
            or _foreign_self_city(reply or "", city, last_user)
            or _dv_topic_leak(reply or "")
            or (len(reply) > 140 and not loose)
        )
        if prev_was_city:
            answered = _user_answered_city(last_user)
            bad = bad or (bool(city) and not _city_mentioned(city, reply or ""))
            if answered and _asks_city_again(reply or ""):
                bad = True
            if bad:
                return _swap("ask_back_city", _city_reply_back(city, last_user))
            return reply
        if bad:
            return _swap("ask_back", _ask_back_reply(history, name, gender, city, last_user))
        return reply
    if last_user and _BOT_ASK.search(last_user):
        if (
            not reply
            or _AI_LEAK.search(reply)
            or _BOTTY.search(reply)
            or _BOT_NOISE.search(reply)
            or _gender_broken(reply, gender)
            or (len(reply) > 90 and not loose)
        ):
            return _swap("bot_ask", _bot_reply())
        return reply
    if last_user and _GET_ACQ.search(last_user):
        if (
            not reply
            or _BOTTY.search(reply)
            or _BIO_DUMP.search(reply)
            or _HINT_LEAK.search(reply)
            or _foreign_self_name(reply, name)
            or (len(reply) > 80 and not loose)
        ):
            return _swap("get_acq", _get_acq_reply())
        return reply
    if last_user and _CITY_ASK.search(last_user):
        where = (city or "").strip()
        bad = (
            not reply
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _AI_LEAK.search(reply)
            or _foreign_self_name(reply, name)
            or _foreign_self_city(reply or "", city, last_user)
            or (len(reply) > 90 and not loose)
            or (where and not _city_mentioned(where, reply))
        )
        if bad:
            return _swap("city", _city_reply(city))
        return reply
    if last_user and _ALREADY_SAID.search(last_user):
        if (not reply) or _LAZY.match(reply) or _asks_city_again(reply or "") or _BOTTY.search(reply):
            return _swap("already_said", _already_said_reply())
        return reply
    if last_user and _NICE_MEET.search(last_user):
        if (not reply) or _LAZY.match(reply) or _BOTTY.search(reply) or _DATING_CANNED.search(reply or ""):
            return _swap("nice_meet", _nice_meet_reply())
        return reply
    if last_user and _META_CLARIFY.search(last_user):
        bad = (
            not reply
            or _LAZY.match(reply)
            or _SELF_HERE_DAYS.search(reply or "")
            or _DATING_CANNED.search(reply or "")
            or _ASSISTANT_BIO.search(reply)
            or _BOTTY.search(reply)
            or re.search(r"(?i)(пару дней|я тут|я здесь|уже как тут)", reply or "")
            or (len(reply) > 140 and not loose)
        )
        # На «что давно?» нельзя отвечать про себя — только пояснить свой вопрос
        if bad or not re.search(r"(?i)(тебя|спрашив|как давно|на винчик|тут сид)", reply or ""):
            return _swap("meta_clarify", _meta_clarify_reply(prev_assistant, history))
        return reply
    if last_user and _CLARIFY_ASK.search(last_user):
        bad = (
            not reply
            or _DATING_CANNED.search(reply)
            or _dv_topic_leak(reply or "")
            or _BOTTY.search(reply)
            or _ASSISTANT_BIO.search(reply)
            or _only_question(reply)
            or (len(reply) > 120 and not loose)
        )
        if bad:
            return _swap("clarify", _clarify_reply(prev_assistant))
        return reply
    if last_user and wants_day(last_user):
        too_long = len(reply) > 180 and not loose
        botty = bool(_BOTTY.search(reply) or _BIO_DUMP.search(reply) or (_LAZY.match(reply or "") and not loose))
        if (not reply) or too_long or botty or _only_question(reply) or _gender_broken(reply, gender):
            return _swap("day", spoken_today(city, persona=persona, gender=gender, kind="day"))
        return reply
    if last_user and wants_now(last_user):
        too_long = len(reply) > 160 and not loose
        botty = bool(_BOTTY.search(reply) or _BIO_DUMP.search(reply) or (_LAZY.match(reply or "") and not loose))
        if (not reply) or too_long or botty or _only_question(reply) or _gender_broken(reply, gender):
            return _swap("now", spoken_today(city, persona=persona, gender=gender, kind="now"))
        return reply
    if last_user and _HOW_ARE_YOU.search(last_user):
        too_long = len(reply) > 120 and not loose
        too_many_q = _question_count(reply) > 2
        botty = bool(_BOTTY.search(reply) or _BIO_DUMP.search(reply) or _DV_HYPE.search(reply or ""))
        no_self = _only_question(reply) and _ASK_BACK.search(reply or "")
        greet_again = bool(_GREET_WORD.search(reply or "")) and not _GREET_WORD.search(last_user)
        dv_context = _history_mentions_dv(history, last_user)
        if dv_context and (_dv_topic_leak(reply or "") or too_long or botty):
            return _swap(
                "how_dv",
                random.choice((
                    "пока непонятно, ещё присматриваюсь. а тебе?",
                    "норм, бывает забавно. ты сам зачем тут?",
                    "да пойдёт) а ты как вообще?",
                )),
            )
        if (not reply) or too_long or too_many_q or botty or no_self or greet_again or _gender_broken(reply, gender):
            return _swap("how_are_you", random.choice(_how_are_you_replies(city)))
        return reply
    if last_user and _ABOUT_SELF.search(last_user):
        if (
            not reply
            or _AI_LEAK.search(reply)
            or _ASSISTANT_BIO.search(reply)
            or _BOTTY.search(reply)
            or _HINT_LEAK.search(reply)
            or _gender_broken(reply, gender)
            or (len(reply) > 220 and not loose)
        ):
            return _swap("about_self", fallback_reply(last_user, name, gender, city, history, persona))
        return reply
    if last_user and _DAIVINCHIK_TALK.search(last_user) and _dv_topic_leak(reply or ""):
        return _swap("dv_talk_leak", _dv_leak_fix(last_user, history))
    if reply and _DATING_CANNED.search(reply) and not _history_mentions_dv(history, last_user):
        return _swap(
            "dating_canned",
            fallback_reply(last_user, name, gender, city, history, persona),
        )
    if not reply or _AI_LEAK.search(reply or "") or _gender_broken(reply or "", gender):
        return _swap("empty_or_leak", fallback_reply(last_user, name, gender, city, history, persona))
    if reply and _LAZY.match(reply) and not loose:
        return fallback_reply(last_user, name, gender, city, history, persona)
    if reply and _ASSISTANT_BIO.search(reply):
        return _swap("assistant_bio", fallback_reply(last_user, name, gender, city, history, persona))
    if reply and (_HINT_LEAK.search(reply) or _BOTTY.search(reply) or _DV_HYPE.search(reply or "")):
        if last_user and _HOW_ARE_YOU.search(last_user):
            return _swap("hint_how", random.choice(_how_are_you_replies(city)))
        if _history_mentions_dv(history, last_user) and _dv_topic_leak(reply or ""):
            return _swap("hint_dv", _dv_leak_fix(last_user, history))
        return _swap("hint_botty", fallback_reply(last_user, name, gender, city, history, persona))
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
            return _swap("clock_strip", fallback_reply(last_user, name, gender, city, history, persona))
    return reply


def _isolation_line(peer: Optional[str]) -> str:
    who = (peer or "").strip()
    if prompt_compact():
        if who:
            return f"Пишешь только с {who}. Другие чаты неизвестны."
        return "Один чат, один человек. Другие переписки неизвестны."
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
    name: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    persona_text = (persona or DEFAULT_PERSONA).strip()
    memory_text = (memory or "").strip()
    world_text = (world or "").strip()
    pin = _identity_pin(name, gender)
    suffix = (
        human_suffix()
        + " "
        + _isolation_line(peer)
        + " Отвечай только текстом сообщения. Смотри историю и память этого чата: не переспрашивай уже сказанное."
    )
    compact = prompt_compact()
    persona_max = 2200 if compact else 1800
    memory_max = 720
    world_max = 420
    system_max = 3400 if compact else 3000
    suffix_min = 80 if compact else 160
    pin_len = len(pin) + 1
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
    room = system_max - used - 1 - pin_len
    if room < suffix_min:
        overflow = suffix_min - room
        persona_text = persona_text[: max(900, len(persona_text) - overflow)].rstrip()
        used = len(persona_text)
        if world_block:
            used += 1 + len(world_block)
        if memory_block:
            used += 1 + len(memory_block)
        room = system_max - used - 1 - pin_len
    parts = [persona_text]
    if world_block:
        parts.append(world_block)
    if memory_block:
        parts.append(memory_block)
    parts.append(suffix[: max(0, room)].rstrip())
    parts.append(pin)
    return " ".join(p for p in parts if p).strip()


def _recent_history(history: list[dict], max_turns: int = 18, char_budget: int = 2200) -> list[dict]:
    recent: list[dict] = []
    used = 0
    for item in reversed(history or []):
        role = item.get("role")
        content = (item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        content = content[:450]
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
    nudge: bool = False,
) -> list[dict]:
    name, gender, city = _resolve_voice(persona, name, gender, city)
    last_user = next(
        (item.get("content") or "" for item in reversed(history or []) if item.get("role") == "user"),
        "",
    )
    try:
        world = world_snapshot(city, last_user, persona, gender)
    except Exception:
        logger.debug("Живой контекст недоступен", exc_info=True)
        world = ""
    messages: list[dict] = [{
        "role": "system",
        "content": _system_prompt(persona, peer, memory, world, name, gender),
    }]
    shots = dialogue_shots()
    for user, assistant in shots:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    who = (name or "").strip()
    where = (city or "").strip()
    if shots:
        lock = (
            "Примеры выше — тон лички. Дайвинчик/дв/винчик = бот знакомств в телеге. "
            "Не копируй из примеров имена и города."
        )
        if who:
            lock += f" Тебя зовут {who}."
        if where:
            lock += f" Живёшь в {where}."
        lock += " Не здоровайся второй раз."
        messages.append({"role": "system", "content": lock})
    last_user = ""
    for item in _recent_history(history):
        messages.append({"role": item["role"], "content": item["content"]})
        if item["role"] == "user":
            last_user = item["content"]
    copy_bit = "Не копируй примеры. " if shots else ""
    if nudge:
        try:
            hint = director_nudge_hint(history, city=city, name=name)
        except Exception:
            hint = "Он замолчал. Напиши коротко сама, без привета."
        messages.append({
            "role": "user",
            "content": "[тишина]\n\n[" + copy_bit + hint + "]",
        })
    elif last_user:
        hint = _turn_hint(history, last_user, city, name, gender, persona)
        for i in range(len(messages) - 1, 0, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] = (
                    messages[i]["content"] + "\n\n[" + copy_bit + hint + "]"
                )
                break
    return messages


def _chat_token_estimate(llm, messages: list[dict]) -> int:
    blob = "\n".join((m.get("content") or "") for m in messages)
    extra = 8 * max(1, len(messages))
    if llm is None:
        return max(1, (len(blob) + 1) // 2) + extra
    try:
        return len(llm.tokenize(blob.encode("utf-8"), add_bos=True)) + extra
    except Exception:
        return max(1, (len(blob) + 1) // 2) + extra


def _drop_optional_message(msgs: list[dict]) -> bool:
    """Убирает живые few-shot пары, потом старые реплики истории. System и последний user не трогаем."""
    if len(msgs) < 4:
        return False
    sys_idx = [i for i, m in enumerate(msgs) if m.get("role") == "system"]
    lock = sys_idx[1] if len(sys_idx) >= 2 else -1
    if (
        lock >= 3
        and msgs[lock - 2].get("role") == "user"
        and msgs[lock - 1].get("role") == "assistant"
    ):
        del msgs[lock - 2 : lock]
        return True
    hist_start = lock + 1 if lock >= 0 else 1
    if len(msgs) - hist_start > 1:
        del msgs[hist_start]
        return True
    return False


def _fit_chat(llm, messages: list[dict], reserve: int) -> list[dict]:
    n_ctx = max(512, settings.llm_n_ctx)
    if llm is not None:
        try:
            n_ctx = int(llm.n_ctx())
        except Exception:
            pass
    budget = max(256, n_ctx - max(16, reserve))
    fitted = [dict(m) for m in messages]
    dropped = 0
    while _chat_token_estimate(llm, fitted) > budget and _drop_optional_message(fitted):
        dropped += 1
    if dropped:
        logger.info(
            "Промт урезан под n_ctx=%s: убрано кусков=%s, сообщений=%s, оценка токенов=%s",
            n_ctx,
            dropped,
            len(fitted),
            _chat_token_estimate(llm, fitted),
        )
    return fitted


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
        self._sampler_ok: Optional[tuple] = None

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
        self._sampler_ok = None
        n_threads = settings.llm_n_threads
        cpu = os.cpu_count() or 4
        if n_threads <= 0:
            n_threads = cpu
        n_ctx = max(512, min(int(settings.llm_n_ctx), 8192))
        n_batch = min(512, n_ctx)
        kwargs = dict(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_gpu_layers=settings.llm_n_gpu_layers,
            n_batch=n_batch,
            use_mmap=True,
            use_mlock=False,
            logits_all=False,
            embedding=False,
            chat_format=_chat_format_for(model_path),
            verbose=False,
        )
        try:
            self._llm = Llama(**kwargs, n_threads_batch=n_threads, n_ubatch=n_batch)
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
        model_trace.event(
            "load",
            detail=f"{model_path.name}, n_ctx={n_ctx}, gpu_layers={settings.llm_n_gpu_layers}",
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
        nudge: bool = False,
        account_id: str = "",
        chat_id: str = "",
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
                    nudge,
                    account_id,
                    chat_id,
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
                nudge,
                account_id,
                chat_id,
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
        extras = _sampler_extras()
        if self._sampler_ok is not None:
            extras = {k: extras[k] for k in self._sampler_ok if k in extras}
        max_tokens = _token_budget()
        messages = _fit_chat(self._llm, messages, reserve=max_tokens + 64)

        def _run(extra: dict) -> str:
            kwargs = dict(
                messages=messages,
                max_tokens=max_tokens,
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
                stop=_STOP,
                **extra,
            )
            if on_partial:
                acc = ""
                for chunk in self._llm.create_chat_completion(**kwargs, stream=True):
                    delta = _chunk_delta(chunk)
                    if not delta:
                        continue
                    acc += delta
                    on_partial(acc)
                return acc
            result = self._llm.create_chat_completion(**kwargs)
            return result["choices"][0]["message"].get("content") or ""

        while True:
            try:
                text = _run(extras)
                if self._sampler_ok is None:
                    self._sampler_ok = tuple(extras.keys())
                    logger.info("Сэмплеры: %s", ", ".join(extras) or "базовые")
                return text
            except ValueError as exc:
                if "exceed context" in str(exc).lower() and _drop_optional_message(messages):
                    logger.warning(
                        "Промт не влез в окно (%s) — урезаю, сообщений=%s",
                        exc,
                        len(messages),
                    )
                    continue
                raise
            except TypeError:
                dropped = False
                for group in _SAMPLER_DROP:
                    if any(k in extras for k in group):
                        for key in group:
                            extras.pop(key, None)
                        dropped = True
                        break
                if not dropped:
                    return _run({})
                self._sampler_ok = None

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
        nudge: bool = False,
        account_id: str = "",
        chat_id: str = "",
    ) -> str:
        name, gender, city = _resolve_voice(persona, name, gender, city)
        messages = _messages(history, persona, peer, memory, city, name, gender, nudge)
        messages = _fit_chat(
            self._llm,
            messages,
            reserve=_token_budget() + 64,
        )
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        last_user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
        logger.info(
            "Промт: peer=%s сообщений=%s символов=%s persona=%s name=%s gender=%s city=%s nudge=%s",
            (peer or "—")[:40],
            len(messages),
            prompt_chars,
            len(persona or ""),
            name or "—",
            gender or "—",
            (city or "—")[:40],
            nudge,
        )
        sys = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        tail = []
        for item in messages[-6:]:
            if item.get("role") == "system":
                continue
            role = "юзер" if item.get("role") == "user" else "модель"
            tail.append(f"{role}: {(item.get('content') or '')[:160]}")
        model_trace.event(
            "prompt",
            account_id=account_id,
            chat_id=chat_id,
            peer=peer or "",
            detail=(
                f"{len(messages)} сообщ., {prompt_chars} симв., режим={settings.llm_prompt_mode}, "
                f"few-shot={settings.llm_few_shots}, персонаж={name or '—'}, город={city or '—'}"
                + (f"\nСистема:\n{sys}" if sys else "")
                + ("\n\nХвост:\n" + "\n".join(tail) if tail else "")
            ),
            extra={"messages": len(messages), "chars": prompt_chars, "nudge": nudge},
        )
        model_trace.event(
            "generate",
            account_id=account_id,
            chat_id=chat_id,
            peer=peer or "",
            detail="Модель считает ответ" + (" (тишина)" if nudge else ""),
        )

        def hooked(text: str) -> None:
            model_trace.set_live(account_id or "playground", text, emit=True, peer=peer or "", chat_id=chat_id)
            if on_partial:
                on_partial(text)

        t0 = time.perf_counter()
        try:
            text = self._complete(messages, hooked)
        except Exception as exc:
            logger.exception("Генерация упала: %s", exc)
            model_trace.event(
                "error",
                account_id=account_id,
                chat_id=chat_id,
                peer=peer or "",
                detail=str(exc)[:500],
            )
            last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "привет")
            mini = [
                {
                    "role": "system",
                    "content": _system_prompt(persona, peer, memory, name=name, gender=gender),
                },
                {"role": "user", "content": last_user[:400]},
            ]
            try:
                text = self._complete(mini, hooked)
            except Exception as exc2:
                logger.exception("Повторная генерация тоже упала: %s", exc2)
                model_trace.event(
                    "error",
                    account_id=account_id,
                    chat_id=chat_id,
                    peer=peer or "",
                    detail=str(exc2)[:500],
                )
                last = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
                if _is_test_mode(account_id, chat_id):
                    _log_confused("generate_failed", last_user=last, raw=str(exc2), account_id=account_id, chat_id=chat_id)
                    return f"[тест] генерация упала — смотри консоль: {str(exc2)[:160]}"
                if nudge:
                    fb = fallback_nudge(city)
                    model_trace.event("fallback", account_id=account_id, chat_id=chat_id, peer=peer or "", detail=fb)
                    return fb
                fb = fallback_reply(last, name, gender, city, history, persona)
                model_trace.event("fallback", account_id=account_id, chat_id=chat_id, peer=peer or "", detail=fb)
                return fb
        raw_text = text or ""
        cleaned = _cleanup(raw_text)
        last_user = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
        loose = _is_test_mode(account_id, chat_id)
        cleaned = _fix_reply(
            cleaned,
            last_user,
            history,
            city,
            name,
            gender,
            persona,
            nudge=nudge,
            loose=loose,
            account_id=account_id,
            chat_id=chat_id,
            raw_before=raw_text,
        )
        elapsed = time.perf_counter() - t0
        logger.info("Локальная генерация %.1f сек, символов: %s", elapsed, len(cleaned or ""))
        if cleaned:
            out = cleaned
        elif loose:
            _log_confused("empty_after_filters", last_user=last_user, raw=raw_text, account_id=account_id, chat_id=chat_id)
            out = f"[тест] не поняла вопрос — смотри консоль. raw: {(raw_text or '∅')[:200]}"
        else:
            out = fallback_nudge(city) if nudge else fallback_reply(last_user, name, gender, city, history, persona)
        model_trace.event(
            "reply",
            account_id=account_id,
            chat_id=chat_id,
            peer=peer or "",
            detail=f"{elapsed:.1f} с, {len(out)} симв.\n{out[:500]}",
            extra={"elapsed": round(elapsed, 2), "chars": len(out)},
        )
        model_trace.set_live(account_id or "playground", "")
        return out

    async def generate_raw(
        self,
        history: list[dict],
        *,
        remote_url: str,
        remote_key: str = "",
        max_tokens: int = 512,
        account_id: str = "playground",
        on_partial: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Чат с удалённым llama-server без персонажа и системного промпта."""
        messages = [
            {"role": m.get("role"), "content": (m.get("content") or "").strip()}
            for m in history
            if m.get("role") in {"user", "assistant", "system"} and (m.get("content") or "").strip()
        ]
        if not messages:
            raise ValueError("Пустой диалог")
        headers = {"Content-Type": "application/json"}
        if remote_key:
            headers["Authorization"] = f"Bearer {remote_key}"
        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": max(16, min(int(max_tokens), 2048)),
            "temperature": settings.llm_temperature,
            "top_p": settings.llm_top_p,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        model_trace.event(
            "prompt",
            account_id=account_id,
            chat_id="test",
            peer="тест",
            detail=f"сырой чат, {len(messages)} сообщ., без системного промпта",
            extra={"messages": len(messages), "raw": True, "remote": True},
        )
        model_trace.event("generate", account_id=account_id, chat_id="test", peer="тест", detail="Удалённая модель считает ответ")

        def hooked(text: str) -> None:
            model_trace.set_live(account_id, text, emit=True, peer="тест", chat_id="test")
            if on_partial:
                on_partial(text)

        t0 = time.perf_counter()
        base = _remote_base(remote_url)
        client = await self._http_client()
        async with self._gen_lock:
            text = ""
            last_err = "нет ответа"
            for attempt in range(1, 8):
                try:
                    streamed = await self._remote_stream(client, base, headers, payload, hooked)
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
                        await asyncio.sleep(3)
                        continue
                    res.raise_for_status()
                    data = res.json()
                    text = data["choices"][0]["message"].get("content") or ""
                    break
                except httpx.HTTPStatusError as exc:
                    last_err = _remote_error_text(exc.response)
                    if exc.response is not None and exc.response.status_code == 503 and attempt < 7:
                        await asyncio.sleep(3)
                        continue
                    raise ValueError(last_err) from exc
            else:
                raise ValueError(last_err or "Удалённая модель не ответила")
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S | re.I).strip()
        elapsed = time.perf_counter() - t0
        last_user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
        if not text:
            _log_confused("raw_empty", last_user=last_user, raw="", account_id=account_id, chat_id="test")
            text = f"[тест] пустой ответ модели — смотри консоль. Q: {last_user[:120]}"
        elif _LAZY.match(text):
            _log_confused("raw_lazy", last_user=last_user, raw=text, account_id=account_id, chat_id="test")
        model_trace.event(
            "reply",
            account_id=account_id,
            chat_id="test",
            peer="тест",
            detail=f"{elapsed:.1f} с, {len(text)} симв.\n{text[:500]}",
        )
        model_trace.set_live(account_id, "")
        return text

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
        nudge: bool = False,
        account_id: str = "",
        chat_id: str = "",
    ) -> str:
        name, gender, city = _resolve_voice(persona, name, gender, city)
        messages = await asyncio.to_thread(
            _messages, history, persona, peer, memory, city, name, gender, nudge
        )
        messages = _fit_chat(
            None,
            messages,
            reserve=_token_budget() + 64,
        )
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        model_trace.event(
            "prompt",
            account_id=account_id,
            chat_id=chat_id,
            peer=peer or "",
            detail=f"удалённо, {len(messages)} сообщ., {prompt_chars} симв.",
            extra={"messages": len(messages), "chars": prompt_chars, "remote": True},
        )
        model_trace.event("generate", account_id=account_id, chat_id=chat_id, peer=peer or "", detail="Удалённая модель считает ответ")

        def hooked(text: str) -> None:
            model_trace.set_live(account_id or "playground", text, emit=True, peer=peer or "", chat_id=chat_id)
            if on_partial:
                on_partial(text)

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": "local",
            "messages": messages,
            **_chat_payload(),
        }
        t0 = time.perf_counter()
        base = _remote_base(url)
        logger.info("Запрос к удалённому API %s …", base)
        client = await self._http_client()
        text = ""
        last_err = "нет ответа"
        for attempt in range(1, 13):
            try:
                streamed = await self._remote_stream(client, base, headers, payload, hooked)
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
                or "На удалённом сервере модель ещё не готова. В окне start.bat дождитесь строки «Готово»."
            )
        cleaned = _cleanup(text)
        last_user = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
        loose = _is_test_mode(account_id, chat_id)
        cleaned = _fix_reply(
            cleaned,
            last_user,
            history,
            city,
            name,
            gender,
            persona,
            nudge=nudge,
            loose=loose,
            account_id=account_id,
            chat_id=chat_id,
            raw_before=text or "",
        )
        elapsed = time.perf_counter() - t0
        logger.info("Удалённая генерация %.1f сек, символов: %s", elapsed, len(cleaned))
        if cleaned:
            out = cleaned
        elif loose:
            _log_confused(
                "empty_after_fix",
                last_user=last_user,
                raw=text or "",
                account_id=account_id,
                chat_id=chat_id,
            )
            out = (text or "").strip() or "[модель не поняла — смотри консоль / логи]"
        else:
            out = fallback_nudge(city) if nudge else fallback_reply(last_user, name, gender, city, history, persona)
        model_trace.event(
            "reply",
            account_id=account_id,
            chat_id=chat_id,
            peer=peer or "",
            detail=f"{elapsed:.1f} с, {len(out)} симв.\n{out[:500]}",
        )
        model_trace.set_live(account_id or "playground", "")
        return out

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
            "На удалённом сервере модель ещё не готова. "
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
