"""Каталог локальных GGUF-моделей в data/models."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from app.config import settings


def _size_label(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


class ModelCatalog:
    def __init__(self, models_dir: Optional[Path] = None):
        self.models_dir = Path(models_dir or settings.models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def _gguf_files(self) -> list[Path]:
        if not self.models_dir.exists():
            return []
        files = [
            path
            for path in self.models_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".gguf"
        ]
        return sorted(files, key=lambda p: p.name.lower())

    def list_local(self) -> list[dict]:
        default_name = self.get_default()
        items: list[dict] = []
        for path in self._gguf_files():
            size = path.stat().st_size
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": size,
                    "size_label": _size_label(size),
                    "is_default": path.name == default_name,
                }
            )
        return items

    def resolve(self, name: str) -> Path:
        safe = Path(name).name
        path = self.models_dir / safe
        if not path.exists() or path.suffix.lower() != ".gguf":
            raise FileNotFoundError(f"Модель не найдена: {safe}")
        return path

    def get_default(self) -> Optional[str]:
        cfg = self._read_cfg()
        name = cfg.get("default_model")
        if name and (self.models_dir / name).exists():
            return name
        files = self._gguf_files()
        return files[0].name if files else None

    def set_default(self, name: str) -> str:
        path = self.resolve(name)
        cfg = self._read_cfg()
        cfg["default_model"] = path.name
        self._write_cfg(cfg)
        return path.name

    def clear_default(self) -> None:
        cfg = self._read_cfg()
        cfg["default_model"] = None
        self._write_cfg(cfg)

    def delete(self, name: str) -> None:
        path = self.resolve(name)
        path.unlink(missing_ok=True)
        cfg = self._read_cfg()
        if cfg.get("default_model") == path.name:
            cfg["default_model"] = None
            self._write_cfg(cfg)

    def save_upload(self, filename: str, src) -> dict:
        safe = Path(filename).name
        if not safe.lower().endswith(".gguf"):
            raise ValueError("Нужен файл .gguf")
        dest = self.models_dir / safe
        with open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        if not self.get_default():
            self.set_default(safe)
        size = dest.stat().st_size
        return {
            "name": dest.name,
            "path": str(dest),
            "size_bytes": size,
            "size_label": _size_label(size),
            "is_default": dest.name == self.get_default(),
        }

    def _read_cfg(self) -> dict:
        path = settings.models_config_file
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_cfg(self, cfg: dict) -> None:
        settings.models_config_file.parent.mkdir(parents=True, exist_ok=True)
        settings.models_config_file.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
