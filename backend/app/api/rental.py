"""Вход, профиль и админка арендаторов."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth import admin_user, current_user
from app.deps import agents, catalog, rental, store, workers
from app.schemas import LoginRequest, TenantPayload
from app.services.rental_store import COOKIE, POWER_PRESETS

router = APIRouter(prefix="/api")


async def _stop_tenant_agents(tenant_id: str) -> None:
    for acc in store.list_by_tenant(tenant_id):
        if agents.is_running(acc.id):
            await agents.stop(acc.id)


def _quota(tenant: dict) -> dict:
    accounts = store.list_by_tenant(tenant["id"])
    running = sum(1 for a in accounts if agents.is_running(a.id))
    return {
        **tenant,
        "accounts_used": len(accounts),
        "agents_running": running,
        "chats_used": rental.chats_used_tenant(tenant["id"]),
        "power_presets": POWER_PRESETS,
    }


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response):
    try:
        result = rental.login(payload.login.strip(), payload.password)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not result:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    user, token = result
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=14 * 24 * 3600,
        path="/",
    )
    return {"token": token, "user": user}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE) or ""
    rental.logout(token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    data = {"user": {k: user[k] for k in ("id", "login", "role", "tenant_id")}}
    if user["role"] == "tenant" and user.get("tenant"):
        data["quota"] = _quota(user["tenant"])
        data["suspended"] = user["tenant"]["status"] != "active"
    return data


@router.get("/admin/tenants")
def admin_list_tenants(_: dict = Depends(admin_user)):
    items = []
    for t in rental.list_tenants():
        items.append(_quota(t))
    return items


@router.post("/admin/tenants")
def admin_create_tenant(payload: TenantPayload, _: dict = Depends(admin_user)):
    try:
        tenant = rental.create_tenant(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _quota(tenant)


@router.patch("/admin/tenants/{tenant_id}")
async def admin_update_tenant(tenant_id: str, payload: TenantPayload, _: dict = Depends(admin_user)):
    data = payload.model_dump(exclude_unset=True)
    try:
        tenant = rental.update_tenant(tenant_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if tenant["status"] != "active":
        rental.drop_tenant_tokens(tenant_id)
        await _stop_tenant_agents(tenant_id)
    return _quota(tenant)


@router.post("/admin/tenants/{tenant_id}/access")
async def admin_set_access(tenant_id: str, payload: dict, _: dict = Depends(admin_user)):
    granted = bool(payload.get("granted", True))
    try:
        tenant = rental.set_access(tenant_id, granted)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not granted:
        rental.drop_tenant_tokens(tenant_id)
        await _stop_tenant_agents(tenant_id)
    return _quota(tenant)


@router.patch("/me/delays")
def update_my_delays(payload: TenantPayload, user: dict = Depends(current_user)):
    if user["role"] != "tenant":
        raise HTTPException(status_code=403, detail="Только арендатор меняет задержки своей панели")
    if user.get("tenant_suspended") or (user.get("tenant") or {}).get("status") != "active":
        raise HTTPException(status_code=403, detail="Панель приостановлена")
    data = payload.model_dump(exclude_unset=True)
    allowed = {k: data[k] for k in ("read_delay_ms", "reply_delay_ms") if k in data}
    if not allowed:
        raise HTTPException(status_code=400, detail="Укажите задержку в миллисекундах")
    try:
        tenant = rental.update_tenant(user["tenant_id"], allowed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _quota(tenant)


@router.get("/admin/ai-options")
def admin_ai_options(_: dict = Depends(admin_user)):
    return {
        "models": catalog.list_local(),
        "workers": workers.list(),
        "power_presets": POWER_PRESETS,
    }
