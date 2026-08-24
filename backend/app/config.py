"""
Конфигурация приложения. Значения читаются из переменных окружения
(см. .env.example) через python-dotenv.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # корень репозитория


def _resolve_data_dir(raw: str | None, fallback: Path) -> Path:
    """Относительный путь из .env считаем от CWD, иначе берём папку проекта."""
    if not raw:
        return fallback.resolve()
    path = Path(raw)
    if not path.is_absolute():
        cwd_path = (Path.cwd() / path).resolve()
        if cwd_path.exists():
            return cwd_path
        return fallback.resolve()
    return path.resolve()


class Settings:
    """Единая точка доступа к настройкам приложения."""

    def __init__(self) -> None:
        self.api_id = int(os.getenv("TG_API_ID", "0"))
        self.api_hash = os.getenv("TG_API_HASH", "")

        data_root = BASE_DIR / "data"
        self.sessions_dir: Path = Path(
            os.getenv("SESSIONS_DIR", str(data_root / "sessions"))
        ).resolve()
        self.accounts_file: Path = Path(
            os.getenv("ACCOUNTS_FILE", str(data_root / "accounts.json"))
        ).resolve()
        self.models_dir: Path = _resolve_data_dir(
            os.getenv("MODELS_DIR"),
            data_root / "models",
        )
        self.models_config_file: Path = self.models_dir / "config.json"
        store_root = self.accounts_file.parent
        self.characters_file: Path = Path(
            os.getenv("CHARACTERS_FILE", str(store_root / "characters.json"))
        ).resolve()
        self.workers_file: Path = Path(
            os.getenv("WORKERS_FILE", str(store_root / "workers.json"))
        ).resolve()
        self.rental_db: Path = Path(
            os.getenv("RENTAL_DB", str(store_root / "rental.db"))
        ).resolve()
        self.admin_login: str = os.getenv("ADMIN_LOGIN", "admin")
        self.admin_password: str = os.getenv("ADMIN_PASSWORD", "admin")
        self.llm_n_ctx: int = int(os.getenv("LLM_N_CTX", "2048"))
        self.llm_n_threads: int = int(os.getenv("LLM_N_THREADS", "0"))
        self.llm_n_gpu_layers: int = int(os.getenv("LLM_N_GPU_LAYERS", "0"))
        self.llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "64"))

        cors_raw = os.getenv(
            "CORS_ORIGINS",
            "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500",
        )
        self.cors_origins: list[str] = [o.strip() for o in cors_raw.split(",") if o.strip()]

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if not self.api_id or not self.api_hash:
            raise RuntimeError(
                "TG_API_ID / TG_API_HASH не заданы. "
                "Получите их на https://my.telegram.org и укажите в файле .env"
            )


settings = Settings()
