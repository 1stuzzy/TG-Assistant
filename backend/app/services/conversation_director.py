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
from app.services.world_context import (
    clock as world_clock,
    wants_news,
    wants_now,
    wants_show,
    wants_time,
    wants_weather,
    wants_day,
    watching_title,
)

logger = logging.getLogger(__name__)

DEFAULT_SUFFIX = (
    "Живой диалог: коротко, как в личке. Сначала ответь на сказанное, не «ага». "
    "В начале знакомства после ответа один живой вопрос. "
    "Про себя — только из личности в этом промте. Это один чат с одним человеком. Не ассистент. "
    "Эмоции по разговору: тяжело — мягко; шутка — можно ржать; коротко написал — не души."
)

_QUESTION = re.compile(r"[?？]|^(?i)\s*(а\s+)?(ты|как|что|чем|кто|где|когда|какой|какая|какие|зачем|почему)\b")
_GREET_ONLY = re.compile(r"(?i)^\s*(привет|приветик|хай|ку|здаров|здравствуй(те)?|йо)\s*[.!?…)]*\s*$")
_BOT_ASK = re.compile(r"(?i)(ты бот|ты ии|нейросет|странно обща|как бот)")
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
_NAME_BACK = re.compile(
    r"(?i)(а тебя\s*\?*\s*$|а тебя как|а как тебя|тебя как зовут|а тво[её] имя)"
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
_SHOW_ASK = re.compile(
    r"(?i)("
    r"какой сериал|какую сери[юи]|что за сериал|"
    r"какой смотришь|что смотришь|"
    r"какой фильм|что за фильм"
    r")"
)
_DAIVINCHIK = re.compile(
    r"(?i)("
    r"дайвинчик|давинчик|дай\s*винчик|винчик|"
    r"леонардо\s*дай\s*винчик|леонардо\s*да\s*винчик|"
    r"daivinchik|davinchik|"
    r"\bдв\b|\bлео\b"
    r")"
)


_PACK_MARKERS = ("human_rules.json", "topics.json", "emotions.json", "dialogues.jsonl", "slang.json", "sepia.json")
_FALLBACK_SHOTS = (("привет", "приветик, как день?"), ("ты бот?", "нет, с чего такие выводы"))
_LIVE_NOISE = re.compile(
    r"(?i)("
    r"токен|бирж|nft|\bнфт\b|vpn|\bвпн\b|сбп|"
    r"верификац|куратор|заявк|пополн|льгот|"
    r"выве[дс]|вывод|баланс|закинул|замороз|"
    r"сбер|озон|\bбанк\b|оплат|перевод|"
    r"поддержк|продавц|выстав|куп(ил|ить|пи)|продаж|"
    r"скриншот|скрин |/start|"
    r"милана|крылов|"
    r"североурал|талица|севыч|\bекб\b|екатеринбург|сибай|"
    r"кружок|кружоч|поскидывай|фоточ|фотк|"
    r"хирург|сперма|узбек|переводчик|"
    r"пздц какой заботлив|"
    r"\d+\s*(тыс|руб)|700к|\bайди\b|"
    r"50 на 50|сессию заканч|кредит|"
    r"деньг|закуп|аванс|покупаем|\bплюсе\b|"
    r"зарплат|\bзп\b|продава|сумм[уыеа]|помощь твоя|"
    r"расплатил|перевест|повысили|бизнесменш|"
    r"фотографи|глазки|скинешь|инста|\bмоня\b|"
    r"очень красив|бородой|технологии дошли|помог мне"
    r")"
)
_GLUED_DAYS = re.compile(
    r"(?i)(спокойной ночи|сладких снов).{6,}(доброе утро)|"
    r"(доброе утро).{6,}(спокойной ночи)|"
    r"(пойду спать).{6,}(доброе утро)"
)
_SHOT_GREET = re.compile(r"(?i)^[^\wа-яё]{0,6}(привет|приветик|здравств|доброе)")
_LAZY_ASSISTANT = re.compile(
    r"(?i)^("
    r"да|нет|ок|окс|оки|ага|угу|ну|нуу+|онет|"
    r"хаха|хех|ураа?|ждемс|отлично|хорошо|"
    r"поняла\)*|спасибо\)*|спасибки|умничка|"
    r"капец|согласна|странные"
    r")\s*[).!]*$"
)
_SPACE = re.compile(r"\s+")


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


def _load_shots(path: Path, limit: int = 42) -> list[tuple[str, str]]:
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


LIVE_SHOT_LIMIT = 6
CORE_SHOT_LIMIT = 14
TOTAL_SHOT_LIMIT = 18


def pack_dir() -> Path:
    return _pack_dir()


def reload_pack() -> None:
    _pack.cache_clear()


def style_shot_ok(user: str, assistant: str) -> bool:
    user = _SPACE.sub(" ", (user or "").strip())
    assistant = _SPACE.sub(" ", (assistant or "").strip())
    if not (12 <= len(user) <= 90 and 12 <= len(assistant) <= 90):
        return False
    if re.fullmatch(r"[\d.,\s]+", user):
        return False
    blob = f"{user} {assistant}"
    if _LIVE_NOISE.search(blob):
        return False
    if _GLUED_DAYS.search(user) or _GLUED_DAYS.search(assistant):
        return False
    if _SHOT_GREET.match(assistant) and not _SHOT_GREET.match(user):
        return False
    if _LAZY_ASSISTANT.match(assistant):
        return False
    if re.match(r"(?i)^оки\b", assistant):
        return False
    if assistant.count("?") > 1 or user.count("?") > 1:
        return False
    if blob.count("😁") + blob.count("😊") + blob.count("😅") + blob.count("🥰") + blob.count("😘") > 1:
        return False
    if re.search(r"(?i)https?://|t\.me/", blob):
        return False
    return True


def _combined_shots(root: Path) -> list[tuple[str, str]]:
    core = _load_shots(root / "dialogues.jsonl", limit=CORE_SHOT_LIMIT)
    live = [
        pair
        for pair in _load_shots(root / "dialogues.live.jsonl", limit=400)
        if style_shot_ok(pair[0], pair[1])
    ][:LIVE_SHOT_LIMIT]
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for pair in (*core, *live):
        key = (pair[0].casefold(), pair[1].casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(pair)
        if len(out) >= TOTAL_SHOT_LIMIT:
            break
    return out


@lru_cache(maxsize=1)
def _pack() -> dict:
    root = _pack_dir()
    rules = _load_json(root / "human_rules.json", {})
    sepia = _load_json(root / "sepia.json", {})
    emotions = _load_json(root / "emotions.json", {})
    topics_file = _load_json(root / "topics.json", {})
    slang = _load_json(root / "slang.json", {})
    sit_file = _load_json(root / "situations.json", {})
    shots = _combined_shots(root)
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
    never.extend(str(x).strip() for x in (sepia.get("never") or []) if str(x).strip())
    never = list(dict.fromkeys(never))
    suffix_bits = []
    sepia_extra = (sepia.get("system_extra") or "").strip()
    if sepia_extra:
        suffix_bits.append(sepia_extra)
    suffix_bits.append((rules.get("system_suffix") or DEFAULT_SUFFIX).strip())
    if always:
        suffix_bits.append("Важно: " + "; ".join(always) + ".")
    if never:
        suffix_bits.append("Нельзя: " + "; ".join(never) + ".")
    glossary = (slang.get("glossary") or "").strip()
    if glossary:
        suffix_bits.append(glossary)
    logger.info(
        "Пакет диалога: %s (rules=%s sepia=%s topics=%s emotions=%s shots=%s slang=%s sit=%s)",
        root,
        bool(rules),
        bool(sepia_extra or sepia.get("never")),
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


COMPACT_SUFFIX = (
    "Пиши как в личке: живо и свободно, по ситуации. Сначала ответь на сказанное. "
    "Биография и характер из карточки — сама решай тон и длину, не прячься за «мм» и «обычная жизнь». "
    "Можно шутить, быть прямой, чуть дерзкой. "
    "Жёстко: дайвинчик / дв / винчик / лео — только бот знакомств в Telegram. "
    "Это НЕ дайвинг, НЕ плавание, НЕ ныряние, НЕ спорт и НЕ Леонардо да Винчи. "
    "Запрещено писать «дайвинг классный», «сколько плавала», «нырять». "
    "Не здоровайся второй раз. Не ассистент. "
    "Если спросят про себя / работу / хобби — ответь из карточки по-человечески. "
    "Сериал — название из блока сегодня или карточки."
)


def prompt_compact() -> bool:
    return (settings.llm_prompt_mode or "compact").strip().lower() != "full"


def human_suffix() -> str:
    if prompt_compact():
        gloss = ""
        try:
            slang = _load_json(_pack_dir() / "slang.json", {})
            gloss = (slang.get("glossary") or "").strip()
        except Exception:
            gloss = ""
        if gloss:
            return COMPACT_SUFFIX + " " + gloss
        return COMPACT_SUFFIX
    return _pack()["suffix"]


def dialogue_shots() -> tuple[tuple[str, str], ...]:
    n = max(0, int(getattr(settings, "llm_few_shots", 0) or 0))
    if n == 0:
        return ()
    shots = list(_pack().get("shots") or _FALLBACK_SHOTS)
    return tuple(shots[:n])


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
    turns = _user_turns(history)
    stage = _stage(turns)
    if random.random() < (0.12 if stage == "ice" else 0.38):
        return None
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


def nudge_hint(
    history: list[dict],
    city: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    bits = [
        "Он замолчал в уже идущем чате. Напиши коротко сама, как человек в личке.",
        "Не здоровайся. Не «ну что молчишь» и не «алло ты где». Не извиняйся что пропала.",
        "Не отвечай заново на его старую фразу — это не новое сообщение.",
    ]
    last = _last_user(history)
    if last and len(last) >= 8 and not _GREET_ONLY.match(last):
        bits.append("Можно зацепить последнюю тему своими словами, без пересказа.")
    who = (name or "").strip()
    if who:
        bits.append(f"Ты {who} из карточки, не ассистент.")
    try:
        part = world_clock(city).part
    except Exception:
        part = ""
    if part == "ночь":
        bits.append("Сейчас ночь — тише, без бодрого утра.")
    elif part:
        bits.append(f"Сейчас {part}.")
    bits.append("Одна короткая реплика. Вопрос не обязателен.")
    return " ".join(bits)


def turn_hint(
    history: list[dict],
    last_user: Optional[str] = None,
    city: Optional[str] = None,
    name: Optional[str] = None,
    gender: Optional[str] = None,
    persona: Optional[str] = None,
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

    if wants_show(text) or _SHOW_ASK.search(text):
        show = watching_title(city, persona, gender) or ""
        named = f"Назови «{show}»" if show else "Назови сериал из блока «сегодня»"
        bits.append(
            f"Он спросил что смотришь. {named}, одно название. "
            "Не «сериалы вообще» и не уходи в фото/йогу. Потом можно спросить его детектив."
        )
        return " ".join(bits)

    if _TASK_ASK.search(text):
        who = (name or "").strip()
        bits.append(
            "Он просит как у ChatGPT: код, калькулятор, эссе. Ты живой человек из промта"
            + (f", тебя зовут {who}" if who else "")
            + ". Не пиши код и не списки. Коротко отшутись и верни разговор в личку."
        )
        return " ".join(bits)

    if _NAME_ASK.search(text) or _NAME_BACK.search(text):
        who = (name or "").strip() or "имя из промта"
        rod = "женском (рада, пошла)" if is_female(gender) else "мужском"
        bits.append(
            f"Он спросил как тебя зовут. Ответь коротко своим именем: {who}. О себе в {rod} роде. "
            "Не модель, не «умею отвечать на вопросы». Не копируй подсказку."
        )
        return " ".join(bits)

    if _LOOKING_FOR.search(text):
        bits.append(
            "Он спросил кого или зачем ты здесь. Ответь из карточки коротко "
            "(живой человек, познакомиться), и спроси его. Не «ага». Не эссе."
        )
        return " ".join(bits)

    if _ASK_BACK.search(text):
        prev = ""
        for item in reversed(history or []):
            if item.get("role") == "assistant":
                prev = (item.get("content") or "").strip()
                break
        if _CITY_ASK.search(prev) or re.search(r"(?i)откуда", prev):
            where = (city or "").strip() or "город из карточки"
            bits.append(
                f"Он назвал свой город и спросил тебя. Коротко скажи только свой: {where}. "
                "Не переспрашивай «а ты откуда» — он уже сказал. "
                "Не копируй его город как свой, если у тебя другой. Не здоровайся."
            )
            return " ".join(bits)
        blob = " ".join((item.get("content") or "") for item in (history or []))
        if _DAIVINCHIK.search(blob) or _DAIVINCHIK.search(prev):
            bits.append(
                "Он спросил тебя после разговора про дайвинчик (бот знакомств). "
                "Ответь про себя: недавно тут / ещё осваиваюсь. "
                "ЗАПРЕЩЕНО: дайвинг, плавание, ныряние, «сколько плавала». "
                "Это не спорт. Потом один короткий вопрос ему."
            )
            return " ".join(bits)
        bits.append(
            "Он ответил и спросил тебя то же. Сначала ответь про себя из карточки, "
            "не «ага» и не одно «ну». Потом один короткий вопрос ему — прояви интерес. "
            "Не копируй примеры."
        )
        return " ".join(bits)

    if _GREET_ONLY.match(text):
        if greeted:
            bits.append("Уже поздоровались. Не пиши привет снова. Коротко отозвись, без представления.")
        else:
            bits.append("Одно короткое приветствие: приветик. Можно один живой вопрос — как день, как зовут. Не хай, без представления.")
        if part == "ночь":
            bits.append("Сейчас ночь — не пиши доброе утро.")
        return " ".join(bits)

    if _DAIVINCHIK.search(text):
        bits.append(
            "Он про бот знакомств в телеге (дайвинчик/дв/винчик). "
            "Это НЕ дайвинг, не ныряние, не спорт. Не пиши «круто нырять» и не про да винчи. "
            "Если это привет оттуда — «приветик» и спроси давно ли там сидит. "
            "Не пиши «тоже оттуда» и не «хай». Если спросил давно ли ты — недавно. "
            "Не копируй подсказку."
        )
        if greeted:
            bits.append("Не здоровайся повторно.")
        return " ".join(bits)

    if re.search(
        r"(?i)^(что\s+давно|в\s+смысле|о\s*ч[её]м|про\s+что|что\s+ты\s+(имеешь|спросил)|уточни)\s*[?？!.…)]*\s*$",
        text,
    ):
        bits.append(
            "Он не понял твой прошлый вопрос и просит уточнить. "
            "Поясни свой вопрос коротко (например: спрашиваю тебя — давно тут сидишь?), "
            "НЕ отвечай про себя («пару дней как тут»). Не копируй подсказку."
        )
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
            "Он просит рассказать о себе. Ответь живее из карточки: кто ты, чем живёшь "
            "(работа, город, 1–2 детали). Не «обычная жизнь», не список-резюме. Можно 2–3 фразы. "
            "Не говори про обучение нейросети. Если фактов мало — коротко и спроси его."
        )
        return " ".join(bits)

    if wants_day(text):
        bits.append(
            "Он спросил чем была занята / как день. "
            "1–2 бытовых факта из блока «сегодня» своими словами "
            "(магазин, еда, работа — только то, что там есть). Не список. Не другой день. "
            "Потом можно коротко спросить его."
        )
        return " ".join(bits)

    if wants_now(text):
        bits.append(
            "Он спросил чем сейчас занята. Одна фраза из «сейчас» в блоке сегодня. "
            "Не голое «в телефоне сижу», если там кухня, магазин или работа."
        )
        return " ".join(bits)

    if _HOW_ARE_YOU.search(text):
        if greeted:
            bits.append("Уже поздоровались секунду назад. ЗАПРЕЩЕНО писать привет/хай ещё раз.")
        blob = " ".join((item.get("content") or "") for item in (history or []))
        if _DAIVINCHIK.search(blob):
            bits.append(
                "Разговор про дайвинчик (бот знакомств). На «как тебе» ответь коротко и по-человечески "
                "(норм / пока ок), спроси его. Не восхищайся ботом («космос», «огонь»), "
                "не пиши «новенькая», «с удовольствием», не про плавание."
            )
        else:
            bits.append(
                "Он спросил как дела. Коротко, можно одну деталь из блока «сегодня» "
                "или из карточки. Не эссе и не «с удовольствием»."
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
            "Он спросил. Сначала ответь по сути из карточки, не «ага» и не одно «ну»."
        )
        ice = _stage(_user_turns(history)) == "ice"
        if ice or mood not in {"supportive", "tired", "cool"}:
            bits.append("Потом один живой вопрос ему по теме — прояви интерес, не допрос.")
        if greeted:
            bits.append("Не здоровайся повторно.")
        bits.append("Опирайся на память этого чата: не переспрашивай то, что он уже рассказывал.")
        return " ".join(bits)

    topic = _pick_topic(history, text, mood)
    ice = _stage(_user_turns(history)) == "ice"
    shape = random.choice(("ask", "ask", "react") if ice else ("react", "react", "cut", "ask"))
    if shape == "cut":
        bits.append("Одна короткая реакция, можно оборвать. Без вопроса и без вывода «главное».")
    elif shape == "ask" and topic:
        bits.append("Коротко отозвись на сказанное, не резюмируй.")
        bits.append(_ask_hint(topic))
    else:
        bits.append("Короткая реакция на его фразу. Не объясняй смысл.")
        if topic and (ice or random.random() < 0.55):
            bits.append(_ask_hint(topic))
        elif ice:
            bits.append("Потом один короткий вопрос ему — прояви интерес.")
    if greeted:
        bits.append("Не пиши привет — вы уже поздоровались.")
    if part == "ночь":
        bits.append("Сейчас ночь: можно быть спокойнее, не бодрый дневной тон.")
    bits.append("Не повторяй свой прошлый ответ. Не переспрашивай то, что он уже сказал.")
    bits.append("Опирайся на память этого чата. Про себя — только из промта.")
    return " ".join(bits)
