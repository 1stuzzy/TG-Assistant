"""Авторизация: текущий пользователь и доступ к аккаунтам."""
from fastapi import Depends, HTTPException, Request

from app.deps import rental, store

MAINTENANCE_DETAIL = "Технические работы. Добавление и запуск временно недоступны."


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None) or rental.user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Войдите в панель")
    if user["role"] == "tenant":
        tenant = rental.get_tenant(user["tenant_id"])
        if not tenant or tenant["status"] == "revoked":
            raise HTTPException(status_code=403, detail="Доступ к панели отозван")
        if tenant["status"] == "suspended":
            user = {**user, "tenant_suspended": True}
        user = {**user, "tenant": tenant}
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Только администратор")
    return user


def require_tenant_writable(user: dict) -> None:
    if user.get("role") != "tenant":
        return
    if rental.is_maintenance():
        raise HTTPException(status_code=503, detail=MAINTENANCE_DETAIL)


def require_account(account_id: str, user: dict):
    account = store.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    if user["role"] != "admin" and account.tenant_id != user.get("tenant_id"):
        raise HTTPException(status_code=404, detail="Аккаунт не найден")
    return account


def accounts_for(user: dict):
    if user["role"] == "admin":
        return store.list()
    return store.list_by_tenant(user.get("tenant_id") or "")
