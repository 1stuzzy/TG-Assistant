"""
Точка запуска: python run.py
Перед запуском укажите TG_API_ID / TG_API_HASH в файле .env
(см. .env.example).
"""
import logging
import os

import uvicorn

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
    settings.validate()
    # reload убивает ИИ-агента в памяти — включайте только DEV_RELOAD=1
    reload = os.getenv("DEV_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=reload,
        log_level="info",
    )
