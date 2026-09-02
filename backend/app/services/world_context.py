"""
Живой контекст: локальные часы персонажа, погода и новости.
Модель не дообучается — в промт подмешивается снимок «сейчас».
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NEWS_ASK = re.compile(
    r"(?i)("
    r"\bновост|"
    r"что в мире|что в стране|"
    r"какие новости|что по новостям|"
    r"следишь за новост|смотришь новости"
    r")"
)
_WEATHER_ASK = re.compile(
    r"(?i)\b(погод|на улице холодно|на улице тепло|сколько градус|дождь идёт|дождь идет)\b"
)
_TIME_ASK = re.compile(
    r"(?i)("
    r"который час|сколько времени|который сейчас час|"
    r"сколько сейчас времени|какое сейчас время|"
    r"час у тебя"
    r")"
)

_CITY_TZ = {
    "москва": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "питер": "Europe/Moscow",
    "спб": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "петербург": "Europe/Moscow",
    "казань": "Europe/Moscow",
    "нижний новгород": "Europe/Moscow",
    "воронеж": "Europe/Moscow",
    "ростов": "Europe/Moscow",
    "краснодар": "Europe/Moscow",
    "сочи": "Europe/Moscow",
    "волгоград": "Europe/Moscow",
    "ярославль": "Europe/Moscow",
    "тула": "Europe/Moscow",
    "калининград": "Europe/Kaliningrad",
    "самара": "Europe/Samara",
    "тольятти": "Europe/Samara",
    "ижевск": "Europe/Samara",
    "уфа": "Asia/Yekaterinburg",
    "екатеринбург": "Asia/Yekaterinburg",
    "екб": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg",
    "пермь": "Asia/Yekaterinburg",
    "оренбург": "Asia/Yekaterinburg",
    "тюмень": "Asia/Yekaterinburg",
    "магнитогорск": "Asia/Yekaterinburg",
    "омск": "Asia/Omsk",
    "новосибирск": "Asia/Novosibirsk",
    "томск": "Asia/Tomsk",
    "барнаул": "Asia/Barnaul",
    "кемерово": "Asia/Novokuznetsk",
    "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk",
    "улан-удэ": "Asia/Irkutsk",
    "якутск": "Asia/Yakutsk",
    "хабаровск": "Asia/Vladivostok",
    "владивосток": "Asia/Vladivostok",
    "минск": "Europe/Minsk",
    "киев": "Europe/Kyiv",
    "київ": "Europe/Kyiv",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
}

_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_TZ_OFFSETS = {
    "Europe/Kaliningrad": 2,
    "Europe/Minsk": 3,
    "Europe/Moscow": 3,
    "Europe/Kyiv": 2,
    "Europe/Samara": 4,
    "Asia/Yekaterinburg": 5,
    "Asia/Omsk": 6,
    "Asia/Novosibirsk": 7,
    "Asia/Tomsk": 7,
    "Asia/Barnaul": 7,
    "Asia/Novokuznetsk": 7,
    "Asia/Krasnoyarsk": 7,
    "Asia/Irkutsk": 8,
    "Asia/Yakutsk": 9,
    "Asia/Vladivostok": 10,
    "Asia/Almaty": 5,
    "Asia/Tashkent": 5,
}

_NEWS_FEEDS = (
    "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru",
    "https://ria.ru/export/rss2/index.xml",
    "https://lenta.ru/rss/news",
)

_lock = threading.Lock()
_news_cache: list[str] = []
_news_at = 0.0
_weather_cache: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class Clock:
    zone: str
    city: str
    now: datetime
    weekday: str
    date_line: str
    time_line: str
    part: str
    part_hint: str


def wants_news(text: str) -> bool:
    return bool(_NEWS_ASK.search(text or ""))


def wants_weather(text: str) -> bool:
    return bool(_WEATHER_ASK.search(text or ""))


def wants_time(text: str) -> bool:
    return bool(_TIME_ASK.search(text or ""))


def _norm_city(raw: str) -> str:
    text = (raw or "").strip().lower().replace("ё", "е")
    text = re.sub(r"^г\.?\s*", "", text)
    return re.sub(r"\s+", " ", text)


def _zone_name(city: Optional[str]) -> tuple[str, str]:
    raw = (city or "").strip()
    default_zone = (settings.world_timezone or "Europe/Moscow").strip() or "Europe/Moscow"
    if not raw:
        return default_zone, ""
    low = _norm_city(raw)
    mapped = {_norm_city(name): zone for name, zone in _CITY_TZ.items()}
    if low in mapped:
        return mapped[low], raw
    for name, zone in mapped.items():
        if name and (name in low or low in name):
            return zone, raw
    logger.warning("Город персонажа «%s» нет в карте поясов — запасной %s", raw, default_zone)
    return default_zone, raw


def _aware_now(zone: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(zone))
    except Exception:
        hours = _TZ_OFFSETS.get(zone, 3)
        return datetime.now(timezone(timedelta(hours=hours)))


def _part_of_day(hour: int) -> tuple[str, str]:
    if hour < 5:
        return "ночь", "глубокая ночь — не пиши доброе утро и не рассказывай про рабочий день"
    if hour < 8:
        return "раннее утро", "раннее утро — можно быть сонной/сонным, без бодрого «доброе утро» если не спросили"
    if hour < 12:
        return "утро", "утро"
    if hour < 17:
        return "день", "день"
    if hour < 22:
        return "вечер", "вечер"
    return "ночь", "уже ночь — не пиши доброе утро, не выдумывай офис"


def clock(city: Optional[str] = None) -> Clock:
    zone, city_label = _zone_name(city)
    now = _aware_now(zone)
    part, part_hint = _part_of_day(now.hour)
    return Clock(
        zone=zone,
        city=city_label,
        now=now,
        weekday=_WEEKDAYS[now.weekday()],
        date_line=f"{now.day} {_MONTHS[now.month - 1]}",
        time_line=f"{now.hour}:{now.minute:02d}",
        part=part,
        part_hint=part_hint,
    )


def _http_get(url: str, timeout: float = 4.0) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "TG-Assistant/1.0"}) as client:
        res = client.get(url)
        res.raise_for_status()
        return res.text


def _clean_headline(title: str) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    text = re.sub(r"\s+[-—|]\s+[^-—|]{2,40}$", "", text).strip()
    return text[:90]


def _fetch_news() -> list[str]:
    if not settings.world_news:
        return []
    titles: list[str] = []
    seen: set[str] = set()
    for url in _NEWS_FEEDS:
        try:
            xml = _http_get(url)
            root = ET.fromstring(xml)
        except Exception as exc:
            logger.debug("Новости %s недоступны: %s", url, exc)
            continue
        for item in root.iter("item"):
            title = _clean_headline((item.findtext("title") or ""))
            key = title.lower()
            if len(title) < 12 or key in seen:
                continue
            seen.add(key)
            titles.append(title)
            if len(titles) >= 4:
                return titles
    return titles


def news_headlines() -> list[str]:
    global _news_cache, _news_at
    ttl = max(120, settings.world_news_ttl)
    with _lock:
        if _news_cache and (time.time() - _news_at) < ttl:
            return list(_news_cache)
    try:
        titles = _fetch_news()
    except Exception as exc:
        logger.info("Лента новостей недоступна: %s", exc)
        titles = []
    with _lock:
        if titles:
            _news_cache = titles
            _news_at = time.time()
            return list(titles)
        return list(_news_cache)


def weather_line(city: Optional[str]) -> str:
    if not settings.world_weather:
        return ""
    place = (city or "").strip() or "Moscow"
    key = place.lower()
    now = time.time()
    with _lock:
        cached = _weather_cache.get(key)
        if cached and now - cached[0] < 1800:
            return cached[1]
    try:
        raw = _http_get(
            f"https://wttr.in/{quote(place)}?format=%C+%t&lang=ru",
            timeout=4.0,
        ).strip()
    except Exception as exc:
        logger.debug("Погода недоступна: %s", exc)
        return cached[1] if cached else ""
    text = re.sub(r"\s+", " ", raw).strip()[:48]
    if not text or "unknown" in text.lower():
        return cached[1] if cached else ""
    with _lock:
        _weather_cache[key] = (now, text)
    return text


_JOB_IN_PERSONA = re.compile(
    r"Работаешь:\s*(.+?)(?:\s+Ещё в жизни:|\s+Ты и есть|\s+Как пишешь:|$)",
    re.S,
)
_HOB_IN_PERSONA = re.compile(
    r"Ещё в жизни:\s*(.+?)(?:\s+Ты и есть|\s+Как пишешь:|$)",
    re.S,
)
_DAY_ASK = re.compile(
    r"(?i)("
    r"чем\s+(ты\s+)?(сегодня\s+)?(занимал|занята|занят)|"
    r"что\s+(ты\s+)?(сегодня\s+)?(делал|делала)|"
    r"как\s+(у тебя\s+)?(прошёл|прошел)\s+день|"
    r"как\s+(у тебя\s+)?день|"
    r"какой\s+день|"
    r"чем\s+сегодня"
    r")"
)
_NOW_ASK = re.compile(
    r"(?i)("
    r"чем\s+(ты\s+)?(сейчас|щас|ща)?\s*(занимаешь|занята|занят)|"
    r"что\s+(ты\s+)?(сейчас|щас|ща)\s+делаешь|"
    r"а\s+что\s+ты\s+сейчас|"
    r"а\s+ты\s+(сейчас|щас)\s+чем|"
    r"чем\s+занимаешься"
    r")"
)


def wants_day(text: str) -> bool:
    return bool(_DAY_ASK.search(text or ""))


def wants_now(text: str) -> bool:
    return bool(_NOW_ASK.search(text or ""))


def _life_from_persona(persona: Optional[str]) -> tuple[str, str]:
    text = persona or ""
    job = hob = ""
    found = _JOB_IN_PERSONA.search(text)
    if found:
        job = re.sub(r"\s+", " ", found.group(1)).strip()[:120]
    found = _HOB_IN_PERSONA.search(text)
    if found:
        hob = re.sub(r"\s+", " ", found.group(1)).strip()[:140]
    return job, hob


def _girl_voice(gender: Optional[str], persona: Optional[str]) -> bool:
    g = (gender or "").strip().lower()
    if g in {"male", "boy", "м", "парень", "муж", "m"}:
        return False
    if g in {"female", "girl", "ж", "девушка", "жен", "f"}:
        return True
    return "ты парень" not in (persona or "").lower()


def _job_kind(occupation: str) -> str:
    text = (occupation or "").lower()
    if any(key in text for key in ("дизайн", "удалён", "удален", "графич")):
        return "remote"
    if "фото" in text:
        return "photo"
    if "бармен" in text or "бар" in text:
        return "bar"
    if "кофе" in text:
        return "cafe"
    if "цвет" in text:
        return "flowers"
    return "misc"


def _pick(rng: random.Random, options: list[str]) -> str:
    clean = [item for item in options if item]
    return rng.choice(clean) if clean else "дома"


def today_beats(
    city: Optional[str] = None,
    occupation: str = "",
    hobbies: str = "",
    gender: Optional[str] = None,
    persona: Optional[str] = None,
) -> tuple[str, str]:
    """Один стабильный быт на календарный день: (уже было, сейчас)."""
    clk = clock(city)
    girl = _girl_voice(gender, persona)
    if not occupation or not hobbies:
        job, hob = _life_from_persona(persona)
        occupation = occupation or job
        hobbies = hobbies or hob
    rng = random.Random(f"{clk.date_line}|{clk.city}|{occupation[:48]}|{hobbies[:48]}")
    hour = clk.now.hour
    weekend = clk.weekday in {"суббота", "воскресенье"}
    kind = _job_kind(occupation)
    hob = (hobbies or "").lower()
    cat = "кот" in hob or "муся" in hob or "барсик" in hob
    cook = any(key in hob for key in ("готов", "паст", "рамён", "рамен"))
    series = "сериал" in hob
    yoga = "йога" in hob
    swim = "бассейн" in hob or "басейн" in hob
    coffee = "кофе" in hob
    weather = weather_line(clk.city or city).lower()
    wet = any(word in weather for word in ("дожд", "ливень", "снег"))

    went = "ходила" if girl else "ходил"
    sat = "сидела" if girl else "сидел"
    got = "съездила" if girl else "съездил"

    work_past = {
        "remote": [f"утром в макетах {sat}", "заказчику правки кидала" if girl else "заказчику правки кидал"],
        "photo": ["утром съёмка была", "дома фото чистила" if girl else "дома фото чистил"],
        "bar": ["днём ещё дома была" if girl else "днём ещё дома был"],
        "cafe": ["смена в кофейне была", f"в кофейне {sat}"],
        "flowers": ["в цветочном выручала" if girl else "в цветочном выручал"],
        "misc": [f"по работе {sat}", "дела были"],
    }[kind]
    work_now = {
        "remote": ["ещё в файлах копаюсь", "глаз уже мылит от экрана", "ноут открыт, доделываю"],
        "photo": ["дома фото разгребаю", "в телефоне кадры смотрю"],
        "bar": ["дома ещё, смена позже", "собираюсь на смену"],
        "cafe": ["с смены только", "ещё чуть на смене"],
        "flowers": ["с цветочного только", "ещё там крутилась" if girl else "ещё там крутился"],
        "misc": ["ещё по делам", "ща дома уже"],
    }[kind]
    shop = [
        f"в магазин {went}, продуктов набрала" if girl else f"в магазин {went}, продуктов набрал",
        f"за хлебом {got} и молоком",
        "в магазин сгоняла" if girl else "в магазин сгонял",
    ]
    cook_now = ["сейчас готовить буду", "ща на кухне, ужин", "кастрюля уже стоит"]
    home_now = [
        "дома, в телефоне",
        "с котом валяюсь" if cat else "дома лежу",
        ("сериал включила" if girl else "сериал включил") if series else "дома",
    ]
    morning_now = [
        "кофе пью",
        "ещё не раскачалась" if girl else "ещё не раскачался",
        "с бассейна только" if swim else "",
        "кофе дома пью" if coffee or wet else "с кофе гуляю" if coffee else "",
        "на йоге была" if yoga and girl else "",
    ]
    if hour < 8:
        past = _pick(rng, ["плохо спалось", "только встала" if girl else "только встал"])
        now = _pick(rng, morning_now)
    elif hour < 12:
        past = _pick(rng, morning_now + (["на йоге была"] if yoga and girl else []))
        now = _pick(rng, work_now if not weekend else morning_now + home_now)
    elif hour < 17:
        past = _pick(rng, work_past if not weekend else shop)
        now = _pick(rng, home_now if wet else (work_now if not weekend else shop[:1] + home_now))
    elif hour < 22:
        past = _pick(rng, shop if rng.random() < 0.65 else work_past)
        now = _pick(rng, cook_now if (cook or rng.random() < 0.7) else home_now)
    else:
        past = _pick(rng, shop + (["дома ужинала" if girl else "дома ужинал"]))
        now = _pick(rng, [
            "уже валяюсь с чаем",
            "сериал досматриваю" if series else "в телефоне ещё",
            "с котом на диване" if cat else "дома тихо",
        ])
    return past, now


def today_block(
    city: Optional[str] = None,
    persona: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    past, now = today_beats(city, gender=gender, persona=persona)
    return (
        f"Сегодня у тебя: {past}. Сейчас: {now}. "
        "Если спросят про день или чем занята — своими словами из этого, не список и не другой день."
    )


def spoken_today(
    city: Optional[str] = None,
    persona: Optional[str] = None,
    gender: Optional[str] = None,
    kind: str = "day",
) -> str:
    past, now = today_beats(city, gender=gender, persona=persona)
    if kind == "now":
        return now
    if past.lower() in now.lower() or now.lower() in past.lower():
        return now
    return f"{past}, {now}"


def snapshot(
    city: Optional[str],
    last_user: str = "",
    persona: Optional[str] = None,
    gender: Optional[str] = None,
) -> str:
    clk = clock(city)
    where = clk.city or "твой город"
    bits = [
        (
            f"Сейчас: {clk.weekday}, {clk.part}, {where}. {clk.part_hint.capitalize()}. "
            "Не называй дату и точные часы, если не спросили."
        ),
        today_block(city, persona, gender),
    ]
    if wants_news(last_user):
        heads = news_headlines()
        if heads:
            bits.append("Свежие заголовки (одно своими словами, не списком): " + "; ".join(heads[:3]) + ".")
        else:
            bits.append("Новости сейчас не подгрузились — не выдумывай сводку.")
    if wants_weather(last_user):
        w = weather_line(clk.city or city)
        if w:
            bits.append(f"Погода у тебя: {w}. Одна короткая реплика, не сводка.")
    return " ".join(bits)


def _warm_news() -> None:
    try:
        news_headlines()
    except Exception:
        logger.debug("Прогрев ленты новостей не удался", exc_info=True)


if settings.world_news:
    threading.Thread(target=_warm_news, daemon=True, name="world-news").start()
