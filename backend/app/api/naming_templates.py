from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

router = APIRouter()

_templates = []

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
async def list_templates():
    return _templates

@router.post("")
async def create_template(data: NamingTemplateCreate):
    t = {"id": str(len(_templates) + 1), **data.model_dump()}
    _templates.append(t)
    return t

@router.put("/{template_id}")
async def update_template(template_id: str, data: NamingTemplateUpdate):
    for t in _templates:
        if t["id"] == template_id:
            for k, v in data.model_dump(exclude_none=True).items():
                t[k] = v
            return t
    raise HTTPException(status_code=404, detail="Template not found")

@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: str):
    global _templates
    _templates = [t for t in _templates if t["id"] != template_id]

@router.post("/{template_id}/preview")
async def preview_template(template_id: str, data: dict = {}):
    for t in _templates:
        if t["id"] == template_id:
            return {"template": t["template"], "preview": t["template"].replace("{id}", "12345").replace("{title}", "Sample Title").replace("{num}", "1").replace("{extension}", "jpg").replace("{date}", "2024-01-01").replace("{user[id]}", "67890").replace("{user[name]}", "artist_name")}
    raise HTTPException(status_code=404, detail="Template not found")

@router.post("/{template_id}/set-default")
async def set_default(template_id: str):
    for t in _templates:
        t["is_default"] = (t["id"] == template_id)
    return {"status": "ok"}
