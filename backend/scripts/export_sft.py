"""
Собрать датасет для LoRA из dialogues.jsonl и живых чатов.

GGUF не хранит промт. Чтобы стиль и примеры были «в модели»,
нужно дообучить базовые веса (QLoRA), слить адаптер и снова получить GGUF.

Запуск из папки backend:
  python scripts/export_sft.py

Получится ../data/sft/chat.jsonl — формат messages (ChatML / ShareGPT).
Карточку персонажа в веса не кладём: одна модель обслуживает разных людей.
В обучение идёт стиль лички; имя и город по-прежнему приходят коротким system.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.services.conversation_director import COMPACT_SUFFIX, pack_dir  # noqa: E402

STYLE_SYSTEM = (
    "Ты живой человек в личке Telegram. " + COMPACT_SUFFIX
)
OUT = ROOT / "data" / "sft" / "chat.jsonl"


def _pairs(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
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
                rows.append((user, assistant))
                user = assistant = ""
    return rows


def main() -> None:
    root = pack_dir()
    pairs = _pairs(root / "dialogues.jsonl") + _pairs(root / "dialogues.live.jsonl")
    seen: set[tuple[str, str]] = set()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT.open("w", encoding="utf-8") as fh:
        for user, assistant in pairs:
            key = (user.casefold(), assistant.casefold())
            if key in seen or not user or not assistant:
                continue
            seen.add(key)
            rec = {
                "messages": [
                    {"role": "system", "content": STYLE_SYSTEM},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                ]
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"Wrote {n} pairs to {OUT}")
    print(
        "Next (GPU, not from GGUF): QLoRA on the same-family HuggingFace base, "
        "merge, then llama.cpp convert-hf-to-gguf. Keep name/city in a short system prompt."
    )


if __name__ == "__main__":
    main()
