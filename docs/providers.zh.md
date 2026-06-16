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
    def build_gallerydl_config(self, subscription_source) -> dict: ...

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

额外方法：

```python
    def get_creator_dir_from_url(self, url: str, source_creator_id: str) -> str: ...
```

`get_creator_dir_from_url()` 将来源 URL 映射为文件系统安全的作者目录名。所有可下载 provider 均已实现。

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

所有 8 个可下载 provider（Pixiv、X、Iwara、Danbooru、微博、Bilibili、Pinterest、Lofter）均已完整实现 `build_gallerydl_config()`。

`auto_enable_on_import` 标志按来源在 gallery-dl 设置页面中配置。每个来源都有开关，控制新导入的订阅来源是否默认启用。仅 Pixiv 默认自动启用；其余来源均默认禁用。

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
- **状态**：完整支持的可下载 provider
- `source_name`：`"pixiv"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- URL 模式：`pixiv.net/artworks/<id>`、`pixiv.net/users/<id>`（可选 `/en/` 语言前缀）
- 通过 cookie 认证使用 gallery-dl 的 Pixiv 提取器

### X / Twitter (`x.py`)
- **状态**：可下载
- `source_name`：`"x"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：True
- URL 模式：`x.com/<user>`、`x.com/<user>/status/<id>`、`twitter.com/<user>`
- 使用 gallery-dl 的 Twitter 提取器，cookie 认证（`strategy: "tweets"`）
- SearchTimeline 回退端点已在 Dockerfile 中修补移除（Twitter 已弃用该端点）

### Iwara (`iwara.py`)
- **状态**：可下载（需要 gallery-dl >= 1.32.0）
- `source_name`：`"iwara"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- URL 模式：`iwara.tv/video/<id>`、`iwara.tv/image/<id>`、`iwara.tv/profile/<username>`
- 支持用户名/密码或 cookie 文件认证
- 提取器：user、user-videos、user-images、user-playlists、videos、images、playlists、favorites、followers、following、search、tag

### Danbooru (`danbooru.py`)
- **状态**：可下载（同时作为标签元数据参考）
- `source_name`：`"danbooru"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：True（5 种标签分类：artist、character、copyright、general、meta）
- 通过标签搜索下载帖子（`posts?tags=artist_name`）
- 通过用户名/密码或 API key 认证
- URL 模式：`danbooru.donmai.us/posts?tags=...`、`danbooru.donmai.us/artists/<id>`、`danbooru.donmai.us/pools/<id>`

### Danbooru 参考 (`danbooru_reference.py`)
- **状态**：仅供参考
- `source_name`：`"danbooru_reference"`
- `capabilities.is_reference_only`：True
- `capabilities.can_download`：False
- 处理：Danbooru 画师标签规范化、URL 提取、creator_link 建议
- 不实现 `build_gallerydl_config` 或 `parse_assets`

### 微博 / Weibo (`weibo.py`)
- **状态**：可下载
- `source_name`：`"weibo"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：True
- URL 模式：`weibo.com/u/<uid>`、`weibo.com/<username>`、`weibo.com/<uid>/<status_id>`
- 同时支持 `weibo.cn` 移动端域名
- 用户名验证会拒绝以连字符开头的名称和裸前缀关键词
- 从帖子文本中的 `#话题#` 模式提取标签
- Cookie 可选 — 如需认证访问，设置 `data/config/gallery-dl/cookies/weibo.txt`
- gallery-dl 请求频率限制：1.0–2.0 秒/请求

### 哔哩哔哩 / Bilibili (`bilibili.py`)
- **状态**：可下载
- `source_name`：`"bilibili"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：True
- URL 模式：
  - 用户文章：`space.bilibili.com/<uid>/article`
  - 单篇文章：`bilibili.com/read/cv<id>`
  - 用户文章收藏夹：`space.bilibili.com/<uid>/favlist?ftype=article`
- 公开内容无需认证
- 从文章标签列表提取标签
- gallery-dl 请求频率限制：3.0–6.0 秒/请求
- 默认下载 `livephoto` 文件（可通过配置关闭）

### Pinterest (`pinterest.py`)
- **状态**：可下载
- `source_name`：`"pinterest"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：False
- URL 模式：`pinterest.com/pin/<id>`、`pinterest.com/<user>/pins/`、`pinterest.com/<user>/<board>/`
- 仅使用公开 API — 无需认证

### Lofter (`lofter.py`)
- **状态**：可下载
- `source_name`：`"lofter"`
- `capabilities.can_download`：True
- `capabilities.supports_gallerydl`：True
- `capabilities.supports_tags`：False
- URL 模式：`<blog>.lofter.com/post/<id>`、`<blog>.lofter.com/`
- 排除 www.lofter.com（非博客托管地址）
- 必须使用 `["lofter", "{blog_name}", "{id}"]` 目录模式，避免扁平目录导致所有帖子合并为一个作品
- 无需认证

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
