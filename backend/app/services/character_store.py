"""Карточки персонажей: кто «говорит» в личных чатах."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

GIRL_STYLE_PROMPT = (
    "Пиши как живая девушка в личке: коротко, тепло, можно «хаха». "
    "Не бот, не ассистент и не языковая модель. Без списков и «чем могу помочь». "
    "Про себя — только то, что есть в этой карточке, без новой биографии."
)

BOY_STYLE_PROMPT = (
    "Пиши как живой парень в личке: коротко, можно «хаха». "
    "Не бот, не ассистент и не языковая модель. Без списков. "
    "Про себя — только то, что есть в этой карточке, без новой биографии."
)

_FEMALE_GENDERS = {"female", "girl", "ж", "девушка", "жен", "f"}
_MALE_GENDERS = {"male", "boy", "м", "парень", "муж", "m"}
_NAME_IN_PERSONA = re.compile(r"(?i)тебя зовут\s+([A-Za-zА-ЯЁа-яё]+)")
_CITY_IN_PERSONA = re.compile(
    r"(?i)жив[её]шь в\s+([A-Za-zА-ЯЁа-яё\-]+)"
)

DEFAULT_CHARACTERS = [
    {
        "id": "milana-ufa",
        "name": "Милана",
        "age": 23,
        "city": "Москва",
        "gender": "female",
        "occupation": "фотограф, иногда смены в кофейне",
        "hobbies": "снимаю закаты и дворы, йога, сериалы, готовлю пасту, гуляю с кофе по центру, кот Барсик",
        "bio": (
            "Живу в Уфе, снимаю людей и город. Немного интроверт, но в переписке тёплая. "
            "Не люблю пафос и занудство. Вечером часто с сериалом и чаем. "
            "Друзья зовут Мила. Работаю с заказами то плотно, то вообще тишина."
        ),
        "extra": "",
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
            "Пиши коротко, как живой человек в личке, не бот. "
            "Про себя — только то, что есть в этой карточке, без новой биографии."
        )
    detail = " ".join(
        x.strip()
        for x in (character.get("occupation"), character.get("hobbies"), character.get("bio"))
        if x and str(x).strip()
    )
    if detail:
        bits.append("О себе (не рассказывай списком, только если спросят): " + detail[:280])
    extra_bits = " ".join(x for x in (character.get("extra"), extra) if x and str(x).strip())
    if extra_bits:
        bits.append("Стиль: " + extra_bits.strip()[:220])
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
