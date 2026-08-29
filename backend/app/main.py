from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.rental import router as rental_router
from app.api.routes import router
from app.config import settings
from app.deps import agents
from app.services.log_hub import attach_log_hub, log_hub

attach_log_hub()
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_hub.set_loop(asyncio.get_running_loop())
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    logging.getLogger("app").info("Backend запущен, модели: %s", settings.models_dir)
    yield
    await agents.stop_all()


app = FastAPI(
    title="TG-Assistant",
    description="Управление аккаунтами и ИИ-агентами Telegram",
    version="1.4.5",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rental_router)
app.include_router(router)


@app.get("/health")
async def health():
    return {"ok": True}


if FRONTEND_DIR.exists():
    app.mount("/css", StaticFiles(directory=FRONTEND_DIR / "css"), name="css")
    app.mount("/js", StaticFiles(directory=FRONTEND_DIR / "js"), name="js")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/login")
    @app.get("/login.html")
    async def login_page():
        return FileResponse(FRONTEND_DIR / "login.html")
