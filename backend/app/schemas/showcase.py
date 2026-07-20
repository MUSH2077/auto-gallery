from pydantic import BaseModel


class ShowcaseItem(BaseModel):
    work_id: str
    title: str | None = None
    creator_name: str | None = None
    source: str | None = None
    thumb_url: str
    preview_url: str
    width: int | None = None
    height: int | None = None


class ShowcaseSampleResponse(BaseModel):
    items: list[ShowcaseItem]
