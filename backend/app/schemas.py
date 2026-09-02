"""
Pydantic-модели тел запросов и ответов REST API.
"""
from typing import Optional
from pydantic import BaseModel, ConfigDict


class StartLoginRequest(BaseModel):
    phone: str


class StartLoginResponse(BaseModel):
    login_id: str


class ConfirmCodeRequest(BaseModel):
    login_id: str
    code: str


class ConfirmCodeResponse(BaseModel):
    status: str  # "need_2fa" | "success"


class ConfirmPasswordRequest(BaseModel):
    login_id: str
    password: str


class AccountResponse(BaseModel):
    id: str
    phone: str
    name: str
    status: str
    last_check: str
    created_at: str


class ErrorResponse(BaseModel):
    detail: str


class DialogImportRequest(BaseModel):
    max_pairs: Optional[int] = None


class AgentStartRequest(BaseModel):
    model: Optional[str] = None
    persona: Optional[str] = None
    character_id: Optional[str] = None
    engine: str = "local"
    worker_id: Optional[str] = None


class DefaultModelRequest(BaseModel):
    name: str


class CharacterPayload(BaseModel):
    name: str
    age: Optional[int] = None
    city: Optional[str] = None
    gender: str = "female"
    occupation: Optional[str] = None
    hobbies: Optional[str] = None
    bio: Optional[str] = None
    extra: Optional[str] = None


class WorkerPayload(BaseModel):
    name: str
    url: str
    api_key: Optional[str] = None


class LoginRequest(BaseModel):
    login: str
    password: str


class TenantPayload(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    name: Optional[str] = None
    login: Optional[str] = None
    password: Optional[str] = None
    power: Optional[str] = None
    max_accounts: Optional[int] = None
    max_chats: Optional[int] = None
    max_agents: Optional[int] = None
    model_name: Optional[str] = None
    engine: Optional[str] = None
    worker_id: Optional[str] = None
    note: Optional[str] = None
    status: Optional[str] = None
    read_delay_ms: Optional[int] = None
    reply_delay_ms: Optional[int] = None
    folder_title: Optional[str] = None


class TelegramPrefsPayload(BaseModel):
    folder_title: Optional[str] = None
    read_delay_ms: Optional[int] = None
    reply_delay_ms: Optional[int] = None
