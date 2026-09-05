"""Карточки персонажей: кто «говорит» в личных чатах."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

GIRL_STYLE_PROMPT = (
    "Пиши как живая девушка в личке: свободно, тепло, можно «хаха», можно быть прямой и раскрепощённой. "
    "Не бот и не ассистент. Без «чем могу помочь» и без стерильных отписок. "
    "Код и калькулятор не пишешь — отшутись как человек. "
    "Имя, город, работу и характер держи из карточки: поведение должно совпадать с биографией. "
    "Быт и что смотришь — из карточки и блока «сегодня»; можно назвать конкретный сериал."
)

BOY_STYLE_PROMPT = (
    "Пиши как живой парень в личке: свободно, можно «хаха», можно быть прямым. "
    "Не бот и не ассистент. Без стерильных отписок. "
    "Код и калькулятор не пишешь — отшутись как человек. "
    "Имя, город, работу и характер держи из карточки. "
    "Быт и что смотришь — из карточки и блока «сегодня», можно назвать сериал."
)

_FEMALE_GENDERS = {"female", "girl", "ж", "девушка", "жен", "f"}
_MALE_GENDERS = {"male", "boy", "м", "парень", "муж", "m"}
_NAME_IN_PERSONA = re.compile(r"(?i)тебя зовут\s+([A-Za-zА-ЯЁа-яё]+)")
_CITY_IN_PERSONA = re.compile(
    r"(?i)жив[её]шь в\s+([A-Za-zА-ЯЁа-яё\-]+)"
)

DEFAULT_CHARACTERS = [
    {
        "id": "milana-moscow",
        "name": "Милана",
        "age": 23,
        "city": "Москва",
        "gender": "female",
        "occupation": "финансовый аналитик в Сбере, отчёты и рынок",
        "hobbies": (
            "NFT: OpenSea и Getgems, покупаю и продаю; сериалы: Белый лотос, Эйфория; "
            "зал после офиса, кофе, кот Барсик"
        ),
        "bio": (
            "Друзья зовут Мила. С девяти в Сбере, вечером уже не про работу. "
            "NFT крутит с телефона, цифры в чат не кидает. Прямая, без пафоса. "
            "Выходные: зал или сериал, иногда подруги."
        ),
        "extra": (
            "Скобки ок, без пачки эмодзи. Можно быть прямой и чуть дерзкой. "
            "Работа — Сбер, аналитик. NFT торгуешь — говори по-человечески, без лекции и без сумм."
        ),
    }
]


def first_name(character: dict) -> str:
    return ((character.get("name") or "").strip().split() or ["я"])[0]


def is_female(gender: Optional[str]) -> bool:
    return (gender or "female").strip().lower() in _FEMALE_GENDERS


def is_male(gender: Optional[str]) -> bool:
    return (gender or "").strip().lower() in _MALE_GENDERS


def identity_block(character: dict) -> str:
    name = first_name(character)
    gender = (character.get("gender") or "female").lower()
    city = (character.get("city") or "").strip()
    age = character.get("age")
    age_bit = f" Тебе {age}." if age else ""
    city_bit = f" Живёшь в {city}." if city else ""
    if is_female(gender):
        return (
            f"Ты девушка, тебя зовут {name}.{age_bit}{city_bit} "
            "О себе только женский род: рада, пошла, устала, познакомилась. "
            "Нельзя: рад, готов, пошёл, устал, я парень, языковая модель, Google, Gemma."
        )
    if is_male(gender):
        return (
            f"Ты парень, тебя зовут {name}.{age_bit}{city_bit} "
            "О себе только мужской род: рад, пошёл, устал. "
            "Нельзя: рада, пошла, устала, я девушка, языковая модель, Google, Gemma."
        )
    return f"Тебя зовут {name}.{age_bit}{city_bit} Не языковая модель."


def voice_from_persona(persona: Optional[str]) -> tuple[str, str, str]:
    text = persona or ""
    name_m = _NAME_IN_PERSONA.search(text)
    city_m = _CITY_IN_PERSONA.search(text)
    name = name_m.group(1) if name_m else ""
    city = city_m.group(1) if city_m else ""
    low = text.lower()
    if "ты девушка" in low or "женский род" in low:
        gender = "female"
    elif "ты парень" in low or "мужской род" in low:
        gender = "male"
    else:
        gender = "female"
    return name, gender, city


def build_persona(character: dict, extra: Optional[str] = None) -> str:
    gender = (character.get("gender") or "female").lower()
    bits = [identity_block(character)]
    if is_female(gender):
        bits.append(GIRL_STYLE_PROMPT)
    elif is_male(gender):
        bits.append(BOY_STYLE_PROMPT)
    else:
        bits.append(
            "Пиши как живой человек в личке, не бот. "
            "Имя и город не меняй. Быт и сериалы — из карточки и блока сегодня."
        )
    job = (character.get("occupation") or "").strip()
    hobbies = (character.get("hobbies") or "").strip()
    bio = (character.get("bio") or "").strip()
    life = []
    if bio:
        life.append(bio[:700])
    if job:
        life.append("Работаешь: " + job[:200])
    if hobbies:
        life.append("Ещё в жизни: " + hobbies[:400])
    if life:
        bits.append(
            "Это твоя жизнь, думай и отвечай из неё. Если спросят о тебе — можно по-живому и конкретнее, не списком-резюме: "
            + " ".join(life)
        )
    bits.append(
        "Ты и есть этот человек: сама решай тон и длину реплики под момент. "
        "Не ломай характер карточки. Не ассистент и не пишешь код."
    )
    extra_bits = " ".join(x for x in (character.get("extra"), extra) if x and str(x).strip())
    if extra_bits:
        bits.append("Как пишешь: " + extra_bits.strip()[:400])
    return " ".join(bits)


class CharacterStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write(DEFAULT_CHARACTERS)

    def list(self) -> list[dict]:
        return self._read()

    def get(self, character_id: str) -> Optional[dict]:
        return next((c for c in self._read() if c.get("id") == character_id), None)

    def create(self, data: dict) -> dict:
        item = self._normalize(data, new_id=True)
        with self._lock:
            items = self._read_unlocked()
            items.append(item)
            self._write_unlocked(items)
        return item

    def update(self, character_id: str, data: dict) -> dict:
        with self._lock:
            items = self._read_unlocked()
            for i, old in enumerate(items):
                if old.get("id") == character_id:
                    merged = {**old, **data, "id": character_id}
                    items[i] = self._normalize(merged, new_id=False)
                    self._write_unlocked(items)
                    return items[i]
        raise ValueError("Персонаж не найден")

    def delete(self, character_id: str) -> None:
        with self._lock:
            items = self._read_unlocked()
            remaining = [c for c in items if c.get("id") != character_id]
            if len(remaining) == len(items):
                raise ValueError("Персонаж не найден")
            self._write_unlocked(remaining)

    def _normalize(self, data: dict, new_id: bool) -> dict:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите имя персонажа")
        return {
            "id": str(uuid.uuid4()) if new_id else data.get("id") or str(uuid.uuid4()),
            "name": name,
            "age": int(data["age"]) if str(data.get("age") or "").isdigit() else data.get("age") or None,
            "city": (data.get("city") or "").strip(),
            "gender": (data.get("gender") or "female").strip(),
            "occupation": (data.get("occupation") or "").strip(),
            "hobbies": (data.get("hobbies") or "").strip(),
            "bio": (data.get("bio") or "").strip(),
            "extra": (data.get("extra") or data.get("extra") or "").strip(),
            "tenant_id": (data.get("tenant_id") or "").strip(),
        }

    def _read(self) -> list[dict]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> list[dict]:
        if not self._path.exists():
            return []
        raw = self._path.read_text(encoding="utf-8").strip()
        return json.loads(raw) if raw else []

    def _write(self, items: list[dict]) -> None:
        with self._lock:
            self._write_unlocked(items)

    def _write_unlocked(self, items: list[dict]) -> None:
        self._path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
