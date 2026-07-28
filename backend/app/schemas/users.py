from datetime import datetime

from pydantic import BaseModel


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    is_admin: bool
    is_active: bool
    permissions: list[str]
    nsfw_visible: bool
    upload_quota_bytes: int | None = None
    upload_used_bytes: int
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    is_admin: bool = False
    permissions: list[str] = []


class UserUpdate(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    is_admin: bool | None = None
    permissions: list[str] | None = None
    nsfw_visible: bool | None = None
    upload_quota_bytes: int | None = None
    upload_used_bytes: int | None = None


class ResetPasswordOut(BaseModel):
    password: str


class MeOut(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    is_admin: bool
    is_active: bool
    permissions: list[str]
    modules: dict[str, str]
    preferences: dict
    nsfw_visible: bool
    upload_quota_bytes: int | None = None
    upload_used_bytes: int
    must_change_password: bool

    model_config = {"from_attributes": True}


class PreferencesIn(BaseModel):
    preferences: dict


class PreferencesOut(BaseModel):
    preferences: dict
