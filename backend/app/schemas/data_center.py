from typing import Literal

from pydantic import BaseModel


class DataCenterPipelineStats(BaseModel):
    pending_import_works: int
    orphan_pending_artifacts: int
    failed_artifacts: int


class SystemInfoResponse(BaseModel):
    version: str
    python: str
    downloads_size_mb: float
    library_size_mb: float
    downloads_total_gb: float
    downloads_used_gb: float
    downloads_free_gb: float
    library_total_gb: float
    library_used_gb: float
    library_free_gb: float
    archives_kb: dict[str, float]
    db_stats: dict[str, int]
    inventory_updated_at: str | None = None
    inventory_source: Literal["storage_artifacts"]
    pipeline_stats: DataCenterPipelineStats


class StorageSourceStats(BaseModel):
    size_mb: float
    logical_size_mb: float
    creator_count: int
    work_count: int


class StorageRepositoryNode(BaseModel):
    repository_id: str | None = None
    source: str
    source_display_name: str
    disk_source: str
    directory_name: str
    size_mb: float
    logical_size_mb: float
    work_count: int


class StorageCreatorEntry(BaseModel):
    name: str
    display_name: str
    source: str
    size_mb: float
    work_count: int
    creator_id: str | None = None
    repository_id: str | None = None


class CreatorStorageNode(BaseModel):
    creator_id: str
    display_name: str
    size_mb: float
    work_count: int
    repository_count: int
    repositories: list[StorageRepositoryNode]


class StorageLayer(BaseModel):
    path: str
    size_mb: float
    description: str


class StorageBreakdownResponse(BaseModel):
    sources: dict[str, StorageSourceStats]
    creators: list[StorageCreatorEntry]
    creator_tree: list[CreatorStorageNode]
    unlinked_repositories: list[StorageRepositoryNode]
    db_stats: dict[str, int]
    inventory_updated_at: str | None = None
    inventory_source: Literal["storage_artifacts"]
    pipeline_stats: DataCenterPipelineStats
    layers: dict[str, StorageLayer]
