"""Users CRUD API — admin-only account management."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdminUser
from app.database import get_db
from app.models.user import User
from app.schemas.users import ResetPasswordOut, UserCreate, UserOut, UserUpdate
from app.services.users import UserService

router = APIRouter(dependencies=[])


def _raise_from_value_error(exc: ValueError, *, not_found_status: int = 404) -> None:
    msg = str(exc)
    if "not found" in msg:
        raise HTTPException(status_code=not_found_status, detail=msg) from exc
    if "taken" in msg:
        raise HTTPException(status_code=409, detail=msg) from exc
    raise HTTPException(status_code=400, detail=msg) from exc


@router.get("", response_model=list[UserOut])
async def list_users(admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)):
    return await UserService(db).list()


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    data: UserCreate, admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)
):
    try:
        return await UserService(db).create(
            username=data.username,
            password=data.password,
            display_name=data.display_name,
            is_admin=data.is_admin,
            permissions=data.permissions,
        )
    except ValueError as exc:
        _raise_from_value_error(exc)


@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)):
    try:
        return await UserService(db).get(user_id)
    except ValueError as exc:
        _raise_from_value_error(exc)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, data: UserUpdate, admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)
):
    fields = data.model_dump(exclude_unset=True)
    try:
        return await UserService(db).update(user_id, **fields)
    except ValueError as exc:
        _raise_from_value_error(exc)


@router.delete("/{user_id}")
async def delete_user(user_id: int, admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    try:
        await UserService(db).delete(user_id)
    except ValueError as exc:
        _raise_from_value_error(exc)
    return {"status": "ok"}


@router.post("/{user_id}/reset-password", response_model=ResetPasswordOut)
async def reset_password(
    user_id: int, admin: User = RequireAdminUser, db: AsyncSession = Depends(get_db)
):
    try:
        password = await UserService(db).reset_password(user_id)
    except ValueError as exc:
        _raise_from_value_error(exc)
    return ResetPasswordOut(password=password)
