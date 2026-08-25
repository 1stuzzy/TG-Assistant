"""
HTTP-слой: аккаунты Telegram, каталог GGUF-моделей и запуск ИИ-агента.
"""
import io
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.auth import admin_user, current_user, require_account, accounts_for
from app.deps import agents, catalog, characters, llm, rental, store, telegram, workers
from app.models.account import Account
from app.services.agent_manager import REPLY_FOLDER_TITLE
from app.schemas import (
    AgentStartRequest,
    CharacterPayload,
    ConfirmCodeRequest,
    ConfirmCodeResponse,
    ConfirmPasswordRequest,
    DefaultModelRequest,
    DownloadModelRequest,
    StartLoginRequest,
    StartLoginResponse,
    WorkerPayload,
)
from app.services.log_hub import log_hub

router = APIRouter(prefix="/api")


def _account_out(account: Account) -> dict:
    data = account.to_dict()
    snap = agents.snapshot(account.id)
    data["agent"] = snap
    enabled = bool(account.tenant_id) or bool(snap.get("running"))
    limit = 0
    if account.tenant_id:
        tenant = rental.get_tenant(account.tenant_id)
        if tenant:
            try:
                limit = int(tenant.get("max_chats") or 0)
            except (TypeError, ValueError):
                limit = 0
    limit = snap.get("folder_limit") or limit
    chats = snap.get("folder_chats") or []
    hint = snap.get("folder_hint") or (
        f"После запуска агента в Telegram появится папка «{REPLY_FOLDER_TITLE}». "
        "Перетащите туда личные чаты — бот ответит только им."
        if enabled
        else ""
    )
    data["reply_folder"] = {
        "title": snap.get("folder_title") or REPLY_FOLDER_TITLE,
        "enabled": enabled,
        "limit": limit,
        "chats": chats,
        "hint": hint,
    }
    return data


@router.get("/accounts")
async def list_accounts(user: dict = Depends(current_user)):
    return [_account_out(a) for a in accounts_for(user)]


@router.post("/accounts/login/start", response_model=StartLoginResponse)
async def login_start(payload: StartLoginRequest, user: dict = Depends(current_user)):
    if user["role"] == "tenant":
        tenant = user.get("tenant") or {}
        if tenant.get("status") != "active":
            raise HTTPException(status_code=403, detail="Панель приостановлена")
        used = len(store.list_by_tenant(user["tenant_id"]))
        if used >= int(tenant.get("max_accounts") or 0):
            raise HTTPException(
                status_code=400,
                detail=f"Лимит аккаунтов исчерпан ({tenant.get('max_accounts')})",
            )
    login_id = str(uuid.uuid4())
    try:
        await telegram.start_login(
            login_id, payload.phone.strip(), user.get("tenant_id") or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка Telegram API: {exc}")
    return StartLoginResponse(login_id=login_id)


@router.post("/accounts/login/code", response_model=ConfirmCodeResponse)
async def login_code(payload: ConfirmCodeRequest, user: dict = Depends(current_user)):
    try:
        status = await telegram.confirm_code(payload.login_id, payload.code.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ConfirmCodeResponse(status=status)


@router.post("/accounts/login/password")
async def login_password(payload: ConfirmPasswordRequest, user: dict = Depends(current_user)):
    try:
        await telegram.confirm_password(payload.login_id, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success"}


@router.post("/accounts/{account_id}/check")
async def check_account(account_id: str, user: dict = Depends(current_user)):
    require_account(account_id, user)
    if agents.is_running(account_id):
        account = store.get(account_id)
        return _account_out(account)
    try:
        account = await telegram.check_status(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _account_out(account)


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, user: dict = Depends(current_user)):
    require_account(account_id, user)
    if agents.is_running(account_id):
        await agents.stop(account_id)
    try:
        await telegram.delete_account(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.post("/accounts/{account_id}/agent/start")
async def start_agent(account_id: str, payload: AgentStartRequest, user: dict = Depends(current_user)):
    require_account(account_id, user)
    if user["role"] == "tenant" and (user.get("tenant") or {}).get("status") != "active":
        raise HTTPException(status_code=403, detail="Панель приостановлена")
    try:
        return await agents.start(
            account_id,
            model_name=(payload.model or "").strip() or None,
            persona=payload.persona,
            character_id=payload.character_id,
            engine=payload.engine or "local",
            worker_id=payload.worker_id,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/accounts/{account_id}/agent/stop")
async def stop_agent(account_id: str, user: dict = Depends(current_user)):
    require_account(account_id, user)
    return await agents.stop(account_id)


@router.get("/accounts/{account_id}/agent")
async def agent_status(account_id: str, user: dict = Depends(current_user)):
    require_account(account_id, user)
    return agents.snapshot(account_id)


@router.get("/models")
async def list_models(user: dict = Depends(current_user)):
    items = catalog.list_local()
    if user["role"] == "admin":
        return items
    allowed = ((user.get("tenant") or {}).get("model_name") or "").strip()
    if not allowed:
        return items
    return [m for m in items if m.get("name") == allowed]


@router.get("/models/recommended")
async def list_recommended(_: dict = Depends(admin_user)):
    return catalog.recommended()


@router.post("/models/default")
async def set_default_model(payload: DefaultModelRequest, _: dict = Depends(admin_user)):
    try:
        name = catalog.set_default(payload.name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"default_model": name}


@router.delete("/models/default")
async def clear_default_model(_: dict = Depends(admin_user)):
    catalog.clear_default()
    return {"default_model": None}


@router.delete("/models/{name}")
async def delete_model(name: str, _: dict = Depends(admin_user)):
    try:
        catalog.delete(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.post("/models/upload")
async def upload_model(file: UploadFile = File(...), _: dict = Depends(admin_user)):
    filename = Path(file.filename or "").name
    try:
        saved = catalog.save_upload(filename, file.file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await file.close()
    return saved


@router.post("/models/download")
async def download_model(payload: DownloadModelRequest, _: dict = Depends(admin_user)):
    try:
        return catalog.start_download(payload.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/models/download/status")
async def download_status(_: dict = Depends(admin_user)):
    return catalog.download_status()


@router.get("/system")
async def system_info(user: dict = Depends(current_user)):
    visible = accounts_for(user)
    models = catalog.list_local()
    running = sum(1 for a in visible if agents.is_running(a.id))
    return {
        "app_version": "1.1.0",
        "api_version": "v1",
        "models": len(models),
        "loaded_model": llm.loaded_name,
        "running_agents": running,
        "models_dir": str(catalog.models_dir),
    }


@router.get("/characters")
async def list_characters(user: dict = Depends(current_user)):
    items = characters.list()
    if user["role"] == "admin":
        return items
    tid = user.get("tenant_id") or ""
    return [c for c in items if not c.get("tenant_id") or c.get("tenant_id") == tid]


@router.post("/characters")
async def create_character(payload: CharacterPayload, user: dict = Depends(current_user)):
    data = payload.model_dump()
    if user["role"] == "tenant":
        data["tenant_id"] = user.get("tenant_id") or ""
    try:
        return characters.create(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/characters/{character_id}")
async def update_character(character_id: str, payload: CharacterPayload, user: dict = Depends(current_user)):
    current = characters.get(character_id)
    if not current:
        raise HTTPException(status_code=404, detail="Персонаж не найден")
    if user["role"] != "admin" and current.get("tenant_id") and current.get("tenant_id") != user.get("tenant_id"):
        raise HTTPException(status_code=404, detail="Персонаж не найден")
    data = payload.model_dump()
    if user["role"] == "tenant":
        data["tenant_id"] = current.get("tenant_id") or user.get("tenant_id") or ""
    try:
        return characters.update(character_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/characters/{character_id}")
async def delete_character(character_id: str, user: dict = Depends(current_user)):
    current = characters.get(character_id)
    if not current:
        raise HTTPException(status_code=404, detail="Персонаж не найден")
    if user["role"] != "admin" and current.get("tenant_id") and current.get("tenant_id") != user.get("tenant_id"):
        raise HTTPException(status_code=404, detail="Персонаж не найден")
    try:
        characters.delete(character_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.get("/workers")
async def list_workers(user: dict = Depends(current_user)):
    items = workers.list()
    if user["role"] == "admin":
        return items
    wid = ((user.get("tenant") or {}).get("worker_id") or "").strip()
    if not wid:
        return items
    return [w for w in items if w.get("id") == wid]


@router.get("/workers/bundle")
async def worker_bundle(_: dict = Depends(admin_user)):
    """ZIP с мини-сервером модели — весь проект качать не нужно."""
    root = Path(__file__).resolve().parents[3] / "remote-worker"
    names = ("start.bat", "open-firewall.bat", "fetch-runtime.ps1", "run-server.ps1")
    missing = [n for n in names if not (root / n).exists()]
    if missing:
        raise HTTPException(status_code=500, detail="Пакет воркера не собран")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            zf.write(root / name, f"tg-worker/{name}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=tg-worker.zip"},
    )


@router.post("/workers")
async def create_worker(payload: WorkerPayload, _: dict = Depends(admin_user)):
    try:
        return workers.create(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/workers/{worker_id}")
async def delete_worker(worker_id: str, _: dict = Depends(admin_user)):
    try:
        workers.delete(worker_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


@router.post("/workers/{worker_id}/ping")
async def ping_worker(worker_id: str, _: dict = Depends(admin_user)):
    try:
        return await workers.ping(worker_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/logs")
async def list_logs(_: dict = Depends(admin_user)):
    return log_hub.history()


@router.websocket("/logs/ws")
async def logs_ws(ws: WebSocket):
    user = rental.user_from_request(ws)
    if not user or user.get("role") != "admin":
        await ws.close(code=4401)
        return
    await ws.accept()
    for item in log_hub.history():
        await ws.send_json(item)
    queue = log_hub.subscribe()
    try:
        while True:
            item = await queue.get()
            await ws.send_json(item)
    except WebSocketDisconnect:
        pass
    finally:
        log_hub.unsubscribe(queue)
