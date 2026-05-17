# Provider 系统

## 概述

Provider 封装所有来源特有行为。应用其余部分只与通用领域模型交互。

## Provider 接口

定义于 `backend/app/providers/base.py`：

```python
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
    def build_gallerydl_config(
        self, subscription_source, naming_template
    ) -> dict: ...

    @abstractmethod
    def parse_source_creator(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_work_source(self, raw_metadata: dict) -> dict: ...

    @abstractmethod
    def parse_assets(
        self, raw_metadata: dict, files: list[str]
    ) -> list[dict]: ...

    @abstractmethod
    def parse_source_tags(self, raw_metadata: dict) -> list[dict]: ...
```

其中：

```python
@dataclass
class ProviderCapabilities:
    can_download: bool
    can_import_local: bool
    supports_gallerydl: bool
    supports_tags: bool
    is_reference_only: bool
```

## Provider 注册表

`backend/app/providers/registry.py` 维护 `source_name → provider 实例` 的映射。查找方式：

```python
registry = ProviderRegistry()
provider = registry.get("pixiv")  # 找不到会抛出异常
all_sources = registry.list_sources()
downloadable = registry.list_downloadable()
```

## 已实现的 Provider

### Pixiv (`pixiv.py`)
- **状态**：首个支持的可下载 provider
- `source_name`：`"pixiv"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- URL 模式：`pixiv.net/en/artworks/<id>`、`pixiv.net/artworks/<id>`、`pixiv.net/users/<id>`
- 通过 cookie 认证使用 gallery-dl 的 Pixiv 提取器

### Iwara (`iwara.py`)
- **状态**：可下载（需要 gallery-dl >= 1.32.0）
- `source_name`：`"iwara"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- URL 模式：`iwara.tv/video/<id>`、`iwara.tv/image/<id>`、`iwara.tv/profile/<username>`
- 支持用户名/密码或 cookie 文件认证
- 提取器：user、user-videos、user-images、user-playlists、videos、images、playlists、favorites、followers、following、search、tag

### X / Twitter (`x.py`)
- **状态**：可下载（gallery-dl 内置 Twitter 提取器）
- `source_name`：`"x"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- URL 模式：`x.com/<user>`、`x.com/<user>/status/<id>`、`twitter.com/<user>`
- 使用 gallery-dl 的 Twitter 提取器，cookie 认证
- 提取器：timeline、tweets、media、likes、search、list、bookmark、avatar、background

### Danbooru 参考 (`danbooru_reference.py`)
- **状态**：仅供参考
- `capabilities.is_reference_only`：True
- `capabilities.can_download`：False
- 处理：Danbooru 画师标签规范化、URL 提取、creator_link 建议
- 不实现 `build_gallerydl_config` 或 `parse_assets`

### 本地文件夹 (`local.py`)
- **状态**：计划中
- `capabilities.can_download`：False
- `capabilities.can_import_local`：True
- 处理：目录扫描、从文件结构推断元数据

### 手动上传 (`manual.py`)
- **状态**：计划中
- `capabilities.can_download`：False
- `capabilities.can_import_local`：True
- 处理：管理员上传文件并手动填写元数据

## 添加新 Provider

1. 创建 `backend/app/providers/<name>.py`
2. 继承 `BaseProvider` 并实现所有抽象方法
3. 在 `registry.py` 注册：`registry.register(MyProvider())`
4. 在数据库枚举中添加 `source` 值（迁移）
5. 如果 gallery-dl 支持该来源，实现 `build_gallerydl_config`
6. 在 admin-web 中添加 provider 特有的 `raw_metadata` 渲染组件

## Provider 设计规则

- Provider 不可访问数据库
- `parse_*` 方法接收原始元数据字典，返回普通字典
- Provider 特有字段名绝不能出现在 API 响应中
- 无法归入通用 schema 的来源特有一切数据存入 `raw_metadata` JSONB
- admin-web 可有 provider 特有的详情组件，知道如何按来源渲染 `raw_metadata`
