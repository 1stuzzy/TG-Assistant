"""
Каталог локальных GGUF-моделей: список файлов в data/models,
рекомендации для CPU и скачивание с Hugging Face.
"""
from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.config import settings


def _size_label(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


@dataclass
class RecommendedModel:
    id: str
    name: str
    filename: str
    repo_id: str
    kind: str
    size_label: str
    ram_label: str
    vram_label: str
    speed: str
    quality: str
    description: str


RECOMMENDED: list[RecommendedModel] = [
    RecommendedModel(
        id="qwen25-15b",
        name="Qwen 2.5 1.5B",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        kind="cpu",
        size_label="1.0 ГБ",
        ram_label="3 ГБ RAM",
        vram_label="—",
        speed="мгновенно",
        quality="тест пайплайна",
        description="Самый быстрый прогон на этом ПК. Русский держит, ответы короткие.",
    ),
    RecommendedModel(
        id="qwen25-3b",
        name="Qwen 2.5 3B",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        repo_id="Qwen/Qwen2.5-3B-Instruct-GGUF",
        kind="cpu",
        size_label="1.9 ГБ",
        ram_label="5 ГБ RAM",
        vram_label="—",
        speed="быстро",
        quality="живой диалог",
        description="Оптимум для CPU: скорость и русский без видеокарты.",
    ),
    RecommendedModel(
        id="llama32-3b",
        name="Llama 3.2 3B",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
        kind="cpu",
        size_label="2.0 ГБ",
        ram_label="5 ГБ RAM",
        vram_label="—",
        speed="быстро",
        quality="другой стиль",
        description="Сравнение с Qwen на том же железе. Английский сильнее, русский слабее.",
    ),
    RecommendedModel(
        id="qwen25-7b-q5",
        name="Qwen 2.5 7B Q5",
        filename="qwen2.5-7b-instruct-q5_k_m.gguf",
        repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
        kind="gpu",
        size_label="5.4 ГБ",
        ram_label="8 ГБ RAM",
        vram_label="8 ГБ VRAM",
        speed="быстро на GPU",
        quality="чистое 7B",
        description="На игровом ПК целиком в VRAM. Лучше 7B Q4, почти без просадки скорости.",
    ),
    RecommendedModel(
        id="qwen25-14b",
        name="Qwen 2.5 14B",
        filename="qwen2.5-14b-instruct-q4_k_m.gguf",
        repo_id="Qwen/Qwen2.5-14B-Instruct-GGUF",
        kind="gpu",
        size_label="8.4 ГБ",
        ram_label="12 ГБ RAM",
        vram_label="10–12 ГБ VRAM",
        speed="средне",
        quality="заметно умнее",
        description="Имеет смысл при видеокарте от 12 ГБ. На игровом ПК — как model.gguf.",
    ),
    RecommendedModel(
        id="qwen25-7b",
        name="Qwen 2.5 7B Q4",
        filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
        kind="hybrid",
        size_label="4.7 ГБ",
        ram_label="8 ГБ RAM",
        vram_label="6 ГБ VRAM + RAM",
        speed="GPU, иначе медленно",
        quality="рабочий диалог",
        description="Слои на видеокарте, хвост на CPU. Тот же файл идёт и чисто на процессоре.",
    ),
    RecommendedModel(
        id="llama31-8b",
        name="Llama 3.1 8B",
        filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        kind="hybrid",
        size_label="4.9 ГБ",
        ram_label="10 ГБ RAM",
        vram_label="8 ГБ VRAM + RAM",
        speed="средне",
        quality="другой характер",
        description="Оффлоад на 8 ГБ карте. Для сравнения качества с Qwen 7B.",
    ),
]


@dataclass
class DownloadState:
    status: str = "idle"
    model_id: Optional[str] = None
    progress: float = 0.0
    message: str = ""


class ModelCatalog:
    def __init__(self, models_dir: Optional[Path] = None):
        self.models_dir = Path(models_dir or settings.models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._download = DownloadState()
        self._lock = threading.Lock()

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

    def recommended(self) -> list[dict]:
        local = {item["name"] for item in self.list_local()}
        return [
            {
                "id": m.id,
                "name": m.name,
                "filename": m.filename,
                "repo_id": m.repo_id,
                "kind": m.kind,
                "hf_url": f"https://huggingface.co/{m.repo_id}",
                "file_url": f"https://huggingface.co/{m.repo_id}/blob/main/{m.filename}",
                "size_label": m.size_label,
                "ram_label": m.ram_label,
                "vram_label": m.vram_label,
                "speed": m.speed,
                "quality": m.quality,
                "description": m.description,
                "downloaded": m.filename in local,
            }
            for m in RECOMMENDED
        ]

    def download_status(self) -> dict:
        with self._lock:
            d = self._download
            return {
                "status": d.status,
                "model_id": d.model_id,
                "progress": d.progress,
                "message": d.message,
            }

    def start_download(self, model_id: str, on_done: Optional[Callable] = None) -> dict:
        rec = next((m for m in RECOMMENDED if m.id == model_id), None)
        if not rec:
            raise ValueError("Неизвестная модель")
        dest = self.models_dir / rec.filename
        if dest.exists():
            if not self.get_default():
                self.set_default(rec.filename)
            return {"status": "done", "model_id": model_id, "progress": 100, "message": "Уже скачана"}

        with self._lock:
            if self._download.status == "downloading":
                raise ValueError("Уже идёт скачивание другой модели")
            self._download = DownloadState(
                status="downloading",
                model_id=model_id,
                progress=0,
                message="Подключение к Hugging Face…",
            )

        thread = threading.Thread(
            target=self._download_worker,
            args=(rec, on_done),
            daemon=True,
        )
        thread.start()
        return self.download_status()

    def _download_worker(self, rec: RecommendedModel, on_done: Optional[Callable]) -> None:
        try:
            from huggingface_hub import hf_hub_download

            def _progress(progress: float, message: str) -> None:
                with self._lock:
                    self._download.progress = progress
                    self._download.message = message

            _progress(2, "Скачивание GGUF… это может занять несколько минут")
            path = hf_hub_download(
                repo_id=rec.repo_id,
                filename=rec.filename,
                local_dir=str(self.models_dir),
            )
            src = Path(path)
            dest = self.models_dir / rec.filename
            if src.resolve() != dest.resolve() and src.exists():
                shutil.copy2(src, dest)
            if not dest.exists():
                raise FileNotFoundError("Файл не появился после скачивания")
            if not self.get_default():
                self.set_default(rec.filename)
            with self._lock:
                self._download.status = "done"
                self._download.progress = 100
                self._download.message = f"Готово: {rec.filename}"
            if on_done:
                on_done()
        except Exception as exc:
            with self._lock:
                self._download.status = "error"
                self._download.message = str(exc)

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

