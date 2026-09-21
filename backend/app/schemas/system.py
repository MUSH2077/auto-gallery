from pydantic import BaseModel


class ServiceStatus(BaseModel):
    postgres: str = "unknown"
    redis: str = "unknown"
    meilisearch: str = "unknown"

    model_config = {"from_attributes": True}


class HealthResponse(BaseModel):
    status: str
    version: str
    build_revision: str
    services: ServiceStatus

    model_config = {"from_attributes": True}
