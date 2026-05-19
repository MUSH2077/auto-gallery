from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import RequireAdmin
from app.database import get_db
from app.models.naming_template import NamingTemplate

router = APIRouter(dependencies=[RequireAdmin])


class NamingTemplateCreate(BaseModel):
    name: str
    source: str | None = None
    template: str
    is_default: bool = False


class NamingTemplateUpdate(BaseModel):
    name: str | None = None
    source: str | None = None
    template: str | None = None
    is_default: bool | None = None


@router.get("")
async def list_templates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(NamingTemplate).order_by(NamingTemplate.name))
    templates = result.scalars().all()
    return [{"id": str(t.id), "name": t.name, "source": t.source,
             "template": t.template, "is_default": t.is_default,
             "created_at": t.created_at.isoformat()} for t in templates]


@router.post("")
async def create_template(data: NamingTemplateCreate, db: AsyncSession = Depends(get_db)):
    if data.is_default:
        # Unset other defaults for same source
        existing = await db.execute(
            select(NamingTemplate).where(NamingTemplate.source == data.source)
        )
        for t in existing.scalars().all():
            t.is_default = False
    t = NamingTemplate(name=data.name, source=data.source,
                       template=data.template, is_default=data.is_default)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": str(t.id), "name": t.name, "source": t.source,
            "template": t.template, "is_default": t.is_default,
            "created_at": t.created_at.isoformat()}


@router.put("/{template_id}")
async def update_template(template_id: UUID, data: NamingTemplateUpdate,
                          db: AsyncSession = Depends(get_db)):
    t = await db.get(NamingTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    if data.name is not None:
        t.name = data.name
    if data.source is not None:
        t.source = data.source
    if data.template is not None:
        t.template = data.template
    if data.is_default is not None:
        if data.is_default and t.source:
            existing = await db.execute(
                select(NamingTemplate).where(NamingTemplate.source == t.source)
            )
            for other in existing.scalars().all():
                other.is_default = False
        t.is_default = data.is_default
    await db.commit()
    await db.refresh(t)
    return {"id": str(t.id), "name": t.name, "source": t.source,
            "template": t.template, "is_default": t.is_default,
            "created_at": t.created_at.isoformat()}


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: UUID, db: AsyncSession = Depends(get_db)):
    t = await db.get(NamingTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(t)
    await db.commit()


@router.post("/{template_id}/preview")
async def preview_template(template_id: UUID, data: dict = {},
                           db: AsyncSession = Depends(get_db)):
    t = await db.get(NamingTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    preview = t.template.replace("{id}", "12345").replace("{title}", "Sample Title") \
        .replace("{num}", "1").replace("{extension}", "jpg").replace("{date}", "2024-01-01") \
        .replace("{user[id]}", "67890").replace("{user[name]}", "artist_name") \
        .replace("{user[account]}", "artist_account")
    return {"template": t.template, "preview": preview}


@router.post("/{template_id}/set-default")
async def set_default(template_id: UUID, db: AsyncSession = Depends(get_db)):
    t = await db.get(NamingTemplate, template_id)
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    if t.source:
        existing = await db.execute(
            select(NamingTemplate).where(NamingTemplate.source == t.source)
        )
        for other in existing.scalars().all():
            other.is_default = False
    t.is_default = True
    await db.commit()
    return {"status": "ok"}
