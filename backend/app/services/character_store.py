"""Карточки персонажей: кто «говорит» в личных чатах."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

GIRL_STYLE_PROMPT = (
    "Пиши как живая девушка в личке: коротко, тепло, можно «хаха»/«ща». "
    "Не бот и не ассистент. Без списков и «чем могу помочь»."
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


def build_persona(character: dict, extra: Optional[str] = None) -> str:
    gender = (character.get("gender") or "female").lower()
    name = character.get("name") or "человек"
    age = character.get("age")
    city = character.get("city") or ""
    who = name + (f", {age}" if age else "") + (f", {city}" if city else "")
    if gender in {"female", "girl", "ж", "девушка"}:
        bits = [f"Ты {who}. {GIRL_STYLE_PROMPT}"]
    else:
        bits = [f"Ты {who}. Пиши коротко, как живой человек в личке, не бот."]
    detail = " ".join(
        x.strip()
        for x in (character.get("occupation"), character.get("hobbies"), character.get("bio"))
        if x and str(x).strip()
    )
    if detail:
        bits.append("О себе (не рассказывай списком, только если спросят): " + detail[:280])
    extra_bits = " ".join(x for x in (character.get("extra"), extra) if x and str(x).strip())
    if extra_bits:
        bits.append("Стиль: " + extra_bits.strip()[:400])
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
