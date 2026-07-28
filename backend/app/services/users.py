"""UserService — account CRUD, permission assignment, and last-admin safety.

User volume is tiny (single-digit to low tens of accounts on a NAS), so this
service queries the table inline rather than delegating to a repository.
"""

from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password
from app.models.user import User
from app.permissions import PERMISSION_MODULES

# Fields callers may change via update(); anything else is silently ignored.
_UPDATABLE_FIELDS = {
    "display_name",
    "is_active",
    "is_admin",
    "permissions",
    "nsfw_visible",
    "upload_quota_bytes",
    "upload_used_bytes",
}


class UserService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list(self) -> list[User]:
        result = await self.db.execute(select(User).order_by(User.id))
        return list(result.scalars().all())

    async def get(self, user_id: int) -> User:
        user = await self.db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        return user

    async def create(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
        is_admin: bool = False,
        permissions: list[str] | None = None,
    ) -> User:
        permissions = list(permissions) if permissions else []
        self._validate_permissions(permissions)

        existing = await self.db.execute(select(User).where(User.username == username))
        if existing.scalars().first() is not None:
            raise ValueError("username taken")

        user = User(
            username=username,
            password_hash=hash_password(password),
            display_name=display_name,
            is_admin=is_admin,
            permissions=permissions,
            must_change_password=True,
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def update(self, user_id: int, **fields) -> User:
        user = await self.get(user_id)

        if "permissions" in fields:
            self._validate_permissions(fields["permissions"])

        demoting = fields.get("is_admin") is False
        disabling = fields.get("is_active") is False
        if user.is_admin and user.is_active and (demoting or disabling):
            await self._ensure_not_last_admin(user.id)

        for key, value in fields.items():
            if key in _UPDATABLE_FIELDS:
                setattr(user, key, value)

        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def delete(self, user_id: int) -> None:
        user = await self.get(user_id)
        if user.is_admin and user.is_active:
            await self._ensure_not_last_admin(user.id)
        await self.db.delete(user)
        await self.db.commit()

    async def reset_password(self, user_id: int) -> str:
        """Reset the user's password to a random value, returned once in plaintext."""
        user = await self.get(user_id)
        plaintext = secrets.token_urlsafe(9)
        user.password_hash = hash_password(plaintext)
        user.must_change_password = True
        await self.db.commit()
        return plaintext

    @staticmethod
    def _validate_permissions(permissions: list[str]) -> None:
        invalid = set(permissions) - set(PERMISSION_MODULES)
        if invalid:
            raise ValueError(f"invalid permission module(s): {', '.join(sorted(invalid))}")

    async def _ensure_not_last_admin(self, excluding_user_id: int) -> None:
        """Raise if excluding this user would leave zero other active admins."""
        result = await self.db.execute(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.is_active.is_(True), User.id != excluding_user_id)
        )
        remaining = result.scalar_one()
        if remaining == 0:
            raise ValueError("last admin")
