from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ProviderCapabilities:
    can_download: bool = False
    can_import_local: bool = False
    supports_gallerydl: bool = False
    supports_tags: bool = False
    is_reference_only: bool = False


class BaseProvider(ABC):
    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def normalize_url(self, input_text: str) -> str | None: ...

    @abstractmethod
    def validate_url(self, url: str) -> bool: ...

    @abstractmethod
    def build_gallerydl_config(self, subscription_source, naming_template) -> dict: ...

    @abstractmethod
    def parse_source_creator(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_work_source(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_assets(self, raw_metadata: dict, files: list[str]) -> list[dict]: ...

    @abstractmethod
    def parse_source_tags(self, raw_metadata: dict) -> list[dict]: ...

    def get_creator_directory_name(self, raw_metadata: dict) -> str:
        """Return the directory name for a creator from raw metadata.

        Must match the gallery-dl directory template used in build_gallerydl_config().
        Default: uses parse_source_creator()['source_creator_id'].
        """
        return str(self.parse_source_creator(raw_metadata).get("source_creator_id", "unknown"))
