from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.auth import require_docs_admin
from app.models.user import User


_ASYNCAPI_PATH = Path(__file__).with_name("contracts") / "asyncapi.yaml"
_DOCS_ASSET_ROOT = "/api-docs"


def create_api_docs_router(app: FastAPI) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/api/openapi.json")
    async def openapi_json(_: User = Depends(require_docs_admin)):
        return JSONResponse(app.openapi(), headers={"Cache-Control": "no-store"})

    @router.get("/api/asyncapi.yaml")
    async def asyncapi_yaml(_: User = Depends(require_docs_admin)):
        return FileResponse(_ASYNCAPI_PATH, media_type="application/yaml", headers={"Cache-Control": "no-store"})

    @router.get("/api/docs")
    async def swagger_ui(_: User = Depends(require_docs_admin)):
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title="auto-gallery LAN API",
            swagger_js_url=f"{_DOCS_ASSET_ROOT}/swagger-ui-bundle.js",
            swagger_css_url=f"{_DOCS_ASSET_ROOT}/swagger-ui.css",
            swagger_favicon_url="/favicon.ico",
            swagger_ui_parameters={
                "deepLinking": True,
                "displayRequestDuration": True,
                "persistAuthorization": False,
                "tryItOutEnabled": True,
            },
        )

    @router.get("/api/redoc")
    async def redoc(_: User = Depends(require_docs_admin)):
        return get_redoc_html(
            openapi_url="/api/openapi.json",
            title="auto-gallery LAN API reference",
            redoc_js_url=f"{_DOCS_ASSET_ROOT}/redoc.standalone.js",
            with_google_fonts=False,
        )

    @router.get("/docs")
    async def legacy_docs():
        return RedirectResponse("/api/docs", status_code=308)

    @router.get("/redoc")
    async def legacy_redoc():
        return RedirectResponse("/api/redoc", status_code=308)

    @router.get("/openapi.json")
    async def legacy_openapi():
        return RedirectResponse("/api/openapi.json", status_code=308)

    return router
