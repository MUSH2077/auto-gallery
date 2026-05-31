# gallery-dl 提取器配置参考

## 概述

auto-gallery 通过管理后台 **设置 -> gallery-dl 配置** 管理各站点的 gallery-dl 提取器设置。配置持久化到 gallery-dl 的 `config.json` 文件中，位于 `extractor.<source>` 段落下。

本文档描述每个已支持站点的所有可用配置选项。

---

## 连通性测试

每个来源选项卡在 **设置 -> gallery-dl 配置** 中都包含一个**测试连接**按钮。它调用 `POST /api/v1/admin/gallerydl-config/test-connection` 并传入来源名称，使用当前凭据运行 gallery-dl 以验证连通性。结果显示成功/失败及诊断消息。在更新 cookies 或认证凭据后，触发同步前使用此功能确认凭据有效。

---

## auto_enable_on_import

每个来源选项都包含一个**导入时自动启用**开关。此设置控制新导入的对应提取器订阅来源是否默认 `is_enabled=True`。

**重要**：此设置存储在 `GALLERYDL_CONFIG_ROOT/config.json` 中，位于每个提取器段落下（如 `extractor.pixiv.auto-enable-on-import`），而非数据库 `system_settings` 表中。后端在导入时从该配置文件读取。

仅 Pixiv 默认 `auto_enable_on_import=true`。所有其他来源默认 `false`。

---

## 任务级配置生成

下载任务创建时，provider 的 `build_gallerydl_config()` 会生成一个任务级配置字典。此任务级配置仅在订阅分配了**命名模板**时才包含 `directory` 字段。无命名模板时，任务级配置省略 directory，由基础 `config.json` 的默认值控制输出路径。这防止了任务级配置覆盖管理员配置的基础目录模式。

---

## Pixiv

**状态**：完全支持（gallery-dl 内置）
**提取器键**：`extractor.pixiv`
**URL 模式**：`pixiv.net/artworks/<id>`、`pixiv.net/users/<id>`（可选 `/en/` 语言前缀）

### 认证

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `refresh_token` | Refresh Token | `extractor.pixiv.refresh-token` | Pixiv OAuth 刷新令牌。推荐认证方式。通过 gallery-dl 的 `oauth:pixiv` 流程获取。优先级高于 cookies。 |
| `cookies_path` | Cookies Path | `extractor.pixiv.cookies` | Netscape 格式 cookie 文件路径，用于 pixiv.net 会话认证。默认：`/gallerydl-config/cookies/pixiv.txt` |

认证优先级：refresh-token > cookies > 未认证（受限速率）。

### 内容选择

| 字段 | UI 标签 | 配置键 | 可选值 | 描述 |
|------|--------|--------|--------|------|
| `include` | Include | `extractor.pixiv.include` | `artworks`、`favorites`、`bookmarks` | 从用户 URL 下载的内容类型。`artworks` 下载公开插画和漫画。`favorites`/`bookmarks` 需要认证。 |
| `tags` | Tag Language | `extractor.pixiv.tags` | `japanese`、`english`、`translated` | 元数据中包含的标签语言版本。 |
| `ugoira` | Ugoira Format | `extractor.pixiv.ugoira` | `zip`、`gif` | 动画 ugoira 处理方式。`zip`：保留原始帧 ZIP 包。`gif`：通过 ffmpeg 转为 GIF 动图。 |
| `max_posts` | Max Posts | `extractor.pixiv.max-posts` | 整数或留空 | 每次任务最大下载作品数。留空 = 不限制。 |

### 元数据选项

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `metadata` | Write Metadata | `extractor.pixiv.metadata` | `false` | 将作品元数据写入下载图片旁的 JSON 文件。 |
| `metadata_bookmark` | Bookmark Metadata | `extractor.pixiv.metadata-bookmark` | `false` | 在 JSON 文件中包含收藏/书签元数据。需要 metadata=true。 |
| `captions` | Captions | `extractor.pixiv.captions` | `false` | 下载作品说明文字。 |
| `comments` | Comments | `extractor.pixiv.comments` | `false` | 下载作品评论。 |
| `sanity` | Artwork Age | `extractor.pixiv.sanity` | `false` | 跳过低于特定时间阈值的新作品（完整性检查）。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.pixiv.directory`（数组） | 输出目录结构。支持 gallery-dl 格式令牌。默认：`["pixiv", "{user[account]}", "{id}"]` |
| `filename` | Filename Pattern | `extractor.pixiv.filename` | 输出文件名模式。默认：`{id}_p{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user[id]}` | `1980643` |
| `{user[account]}` | `askzy` |
| `{user[name]}` | `ASK` |
| `{id}` | `38362603` |
| `{title}` | 作品标题 |
| `{date}` | `2024-01-01` |
| `{num}` | 页码（从 1 开始） |
| `{extension}` | `jpg`、`png`、`gif` |

### 速率限制

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `sleep_request` | Sleep（秒） | `extractor.pixiv.sleep-request` | HTTP 请求间隔时间（秒）。有助于避免触发速率限制。默认：`0`（无延迟）。 |

---

## X / Twitter

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.twitter`
**URL 模式**：`x.com/<user>`、`twitter.com/<user>`、`x.com/<user>/status/<id>`

### 认证

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `cookies_path` | Cookies Path | `extractor.twitter.cookies` | Netscape 格式 cookie 文件路径。大多数内容类型需要（尤其是 likes 和 bookmarks）。默认：`/gallerydl-config/cookies/twitter.txt` |
| `cookie_content` | Cookie Content |（写入文件） | 直接粘贴 cookie 文本。保存时自动写入 cookies 路径。 |

### API 策略

| 字段 | UI 标签 | 配置键 | 可选值 | 默认值 | 描述 |
|------|--------|--------|--------|--------|------|
| `strategy` | 获取策略 | `extractor.twitter.strategy` | `tweets`、`media`、`with_replies` | `tweets` | 控制 gallery-dl 使用哪个 GraphQL 端点获取用户时间线。`tweets` 使用 UserTweets（推荐，积极维护中）。`media` 使用 UserMedia（当前已失效）。`with_replies` 在时间线获取中包含回复。 |

已废弃的 SearchTimeline 回退已通过 Dockerfile 补丁从容器镜像中移除。仅使用配置的策略端点。

### 内容选择

| 字段 | UI 标签 | 配置键 | 可选值 | 描述 |
|------|--------|--------|--------|------|
| `include` | Include | `extractor.twitter.include` | `timeline`、`media`、`tweets`、`likes` | 从用户 URL 下载的内容类型。`timeline` 是标准用户时间线。`media` 仅下载媒体推文。`tweets` 是推文列表。`likes` 需要认证。 |
| `max_posts` | Max Posts | `extractor.twitter.max-posts` | 整数或留空 | 每次任务最大推文下载数。留空 = 不限制。 |

### 内容过滤

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `retweets` | Retweets | `extractor.twitter.retweets` | `false` | 包含用户转发的推文。 |
| `replies` | Replies | `extractor.twitter.replies` | `false` | 包含对其他用户的回复。 |
| `cards` | Cards | `extractor.twitter.cards` | `true` | 下载 Twitter Cards 中的图片（链接预览、摘要卡片）。 |
| `videos` | Videos | `extractor.twitter.videos` | `true` | 下载嵌入视频（MP4）。 |
| `text_tweets` | Text Tweets | `extractor.twitter.text-tweets` | `false` | 包含纯文字推文（无媒体附件）。 |
| `quoted` | Quoted Tweets | `extractor.twitter.quoted` | `false` | 包含引用推文中的媒体。 |
| `pinned` | Pinned | `extractor.twitter.pinned` | `false` | 包含用户置顶推文（即使已归档）。 |
| `previews` | Previews | `extractor.twitter.previews` | `false` | 下载预览/缩略图而非完整尺寸图片。 |
| `articles` | Articles | `extractor.twitter.articles` | `false` | 下载 Twitter Articles（长文内容）。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.twitter.directory`（数组） | 默认：`["twitter", "{user[name]}"]` |
| `filename` | Filename Pattern | `extractor.twitter.filename` | 默认：`{tweet_id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user[id]}` | 数字用户 ID |
| `{user[name]}` | `@username` |
| `{tweet_id}` | `1234567890123456789` |
| `{date}` | `2024-01-01` |
| `{num}` | 推文内媒体序号 |

---

## Iwara

**状态**：支持（需要 gallery-dl >= 1.32.0）
**提取器键**：`extractor.iwara`
**URL 模式**：`iwara.tv/video/<id>`、`iwara.tv/image/<id>`、`iwara.tv/profile/<username>`

### 认证

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `username` | Username | `extractor.iwara.username` | Iwara 账号用户名或邮箱。与 password 配对使用，访问收藏和受限内容时需要。 |
| `password` | Password | `extractor.iwara.password` | Iwara 账号密码。存储在 config.json 中（明文）。如有顾虑可使用 cookie 认证代替。 |
| `cookies_path` | Cookies Path | `extractor.iwara.cookies` | Netscape 格式 cookie 文件路径。可替代用户名/密码认证。默认：`/gallerydl-config/cookies/iwara.txt` |
| `cookie_content` | Cookie Content |（写入文件） | 直接粘贴 cookie 文本。保存时自动写入 cookies 路径。 |

认证优先级：用户名+密码 > cookies > 未认证。未认证只能下载公开、非年龄限制内容。

### 内容选择

| 字段 | UI 标签 | 配置键 | 可选值 | 描述 |
|------|--------|--------|--------|------|
| `include` | Include | `extractor.iwara.include` | `user-images`、`user-videos`、`user-playlists` | 从用户主页下载的内容类型。 |

### 视频质量

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `format` | Format | `extractor.iwara.format` | 无 | 逗号分隔的画质偏好列表。gallery-dl 按顺序尝试每种画质，使用第一个可用的。例如：`"Source, 1080, 720, 540, 360"`。`"Source"` 选择原始上传画质。 |

常用画质值：`Source`、`1080`、`720`、`540`、`360`。留空则 gallery-dl 使用默认值（第一个可用画质）。

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.iwara.directory`（数组） | 默认：`["iwara", "{user[name]}"]` |
| `filename` | Filename Pattern | `extractor.iwara.filename` | 默认：`{date} {id} {title[:200]} {filename}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user[id]}` | 数字用户 ID |
| `{user[name]}` | 显示名称 |
| `{id}` | 视频/图片 ID |
| `{title[:200]}` | 标题（截断至 200 字符） |
| `{date}` | 上传日期 |
| `{filename}` | 原始文件名 |
| `{type}` | `video` 或 `image` |

---

## Danbooru

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.danbooru`
**URL 模式**：`danbooru.donmai.us/posts?tags=<tags>`、`danbooru.donmai.us/artists/<id>`、`danbooru.donmai.us/pools/<id>`

Danbooru 在 auto-gallery 中扮演两个角色：
1. **下载 provider** -- 通过标签搜索下载画作（通常是 artist 标签）
2. **参考 provider** -- 创作者身份映射、URL 提取、创作者链接建议（独立的 `danbooru_reference` provider）

### 认证

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `username` | Username | `extractor.danbooru.username` | Danbooru 账号用户名。API 访问需要。 |
| `password` | Password | `extractor.danbooru.password` | Danbooru 账号密码（或 API key，取决于 Danbooru 实例）。 |
| `api_key` | API Key | `extractor.danbooru.api-key` | Danbooru API key（Danbooru v2+）。替代密码认证。 |
| `cookies_path` | Cookies Path | `extractor.danbooru.cookies` | Netscape 格式 cookie 文件路径。默认：`/gallerydl-config/cookies/danbooru.txt` |
| `cookie_content` | Cookie Content |（写入文件） | 直接粘贴 cookie 文本。保存时自动写入 cookies 路径。 |

### 元数据选项

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `external` | 外部来源 | `extractor.danbooru.external` | `false` | 追踪 `source` 字段从外部原始 URL 下载，而非从 Danbooru CDN。用于获取原始质量文件。 |
| `metadata` | 写入元数据 | `extractor.danbooru.metadata` | `false` | 在下载文件旁写入画作元数据 JSON。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.danbooru.directory`（数组） | 默认：`["danbooru", "{artist[name]}"]` |
| `filename` | Filename Pattern | `extractor.danbooru.filename` | 默认：`{id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{id}` | 画作 ID（如 `1234567`） |
| `{artist[name]}` | 第一个 artist 标签 |
| `{tag_string_artist}` | 全部 artist 标签，空格分隔 |
| `{tag_string_character}` | 角色标签 |
| `{tag_string_copyright}` | 版权/系列标签 |
| `{tag_string_general}` | 通用标签 |
| `{width}`、`{height}` | 图片尺寸 |
| `{file_size}` | 文件大小（字节） |
| `{rating}` | `s`、`q` 或 `e` |
| `{score}` | 社区评分 |
| `{date}` | 上传日期 |
| `{num}` | 画作内媒体序号 |

---

## Pinterest

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.pinterest`
**URL 模式**：`pinterest.com/pin/<id>`、`pinterest.com/<username>/pins/`、`pinterest.com/<username>/<board>/`

### 认证

无需认证。Pinterest 内容公开可访问。无需 cookies 或登录。

### 域名

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `domain` | Domain | `extractor.pinterest.domain` | 自动 | 使用的 Pinterest 区域域名（如 `pinterest.com`、`pinterest.de`、`pinterest.jp`）。留空则根据 URL 自动检测。 |

### 内容过滤

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `stories` | Stories | `extractor.pinterest.stories` | `true` | 下载故事 pin（多图合集）。 |
| `videos` | Videos | `extractor.pinterest.videos` | `true` | 下载视频 pin。 |
| `sections` | Sections | `extractor.pinterest.sections` | `true` | 下载整个画板时同时追踪子版块。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.pinterest.directory`（数组） | 默认：`["pinterest", "{category}", "{user}", "{board[name]}"]` |
| `filename` | Filename Pattern | `extractor.pinterest.filename` | 默认：`{id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user}` | Pinterest 用户名 |
| `{board[name]}` | 画板名称 |
| `{id}` | Pin ID |
| `{category}` | Pin 分类 |
| `{title}` | Pin 标题/描述 |
| `{num}` | Pin 内图片序号 |
| `{extension}` | `jpg`、`png`、`mp4` |

---

## LOFTER

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.lofter`
**URL 模式**：`<blog>.lofter.com/post/<id>`、`<blog>.lofter.com/`

### 认证

无需认证。LOFTER 博客内容公开可访问。

**重要**：目录模式必须使用 `["lofter", "{blog_name}", "{id}"]` 结构。使用扁平目录（无 `{id}`）会导致所有帖子合并为一个作品。

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.lofter.directory`（数组） | 默认：`["lofter", "{blog_name}", "{id}"]` -- `{id}` 子目录对按作品分离至关重要。 |
| `filename` | Filename Pattern | `extractor.lofter.filename` | 默认：`{id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{blog_name}` | 博客子域名 |
| `{id}` | 帖子 ID |
| `{title}` | 帖子标题 |
| `{date}` | 发布日期 |
| `{num}` | 帖内图片序号 |
| `{extension}` | `jpg`、`png`、`gif` |

---

## 微博（Weibo）

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.weibo`
**URL 模式**：`weibo.com/<uid>`、`weibo.com/u/<uid>`、`weibo.com/n/<name>`、`weibo.com/detail/<id>`、`m.weibo.cn/<uid>`

### 认证

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `cookies_path` | Cookies Path | `extractor.weibo.cookies` | Netscape 格式 cookie 文件路径。大多数内容类型需要。默认：`/gallerydl-config/cookies/weibo.txt` |
| `cookie_content` | Cookie Content |（写入文件） | 直接粘贴 cookie 文本。保存时自动写入 cookies 路径。 |

### 内容过滤

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `videos` | Videos | `extractor.weibo.videos` | `true` | 下载视频帖子。 |
| `retweets` | Retweets | `extractor.weibo.retweets` | `false` | 包含转发的帖子。 |
| `gifs` | GIFs | `extractor.weibo.gifs` | `true` | 下载动图帖子。 |
| `livephoto` | Live Photo | `extractor.weibo.livephoto` | `false` | 下载 live photo（短视频片段）帖子。 |
| `movies` | Movies | `extractor.weibo.movies` | `false` | 下载完整电影/长视频帖子。 |
| `text` | Text Posts | `extractor.weibo.text` | `false` | 包含纯文字帖子（无媒体附件）。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.weibo.directory`（数组） | 默认：`["weibo", "{user[screen_name]}"]` |
| `filename` | Filename Pattern | `extractor.weibo.filename` | 默认：`{id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user[id]}` | 数字用户 ID |
| `{user[screen_name]}` | 用户显示名称 |
| `{id}` | 微博/帖子 ID |
| `{date}` | 发布日期 |
| `{num}` | 帖内媒体序号 |
| `{extension}` | `jpg`、`png`、`gif`、`mp4` |

---

## 哔哩哔哩 Bilibili

**状态**：支持（gallery-dl 内置）
**提取器键**：`extractor.bilibili`
**URL 模式**：`space.bilibili.com/<uid>/article`、`bilibili.com/read/cv<id>`、`space.bilibili.com/<uid>/favlist?ftype=article`

### 认证

公开文章和用户文章页面无需认证。

### 内容选项

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `livephoto` | Live Photo | `extractor.bilibili.livephoto` | `true` | 下载动态图文件（部分文章附带的短视频片段）。设为 `false` 可跳过。 |

### 文件组织

| 字段 | UI 标签 | 配置键 | 描述 |
|------|--------|--------|------|
| `directory` | Directory Pattern | `extractor.bilibili.directory`（数组） | 默认：`["bilibili", "{user[name]}", "{id}"]` |
| `filename` | Filename Pattern | `extractor.bilibili.filename` | 默认：`{id}_{num}.{extension}` |

#### 常用令牌

| 令牌 | 示例值 |
|------|--------|
| `{user[id]}` | 数字 UID |
| `{user[name]}` | 显示名称 |
| `{id}` | 文章 ID（如 `12345678`） |
| `{title}` | 文章标题 |
| `{date}` | 发布日期 |
| `{num}` | 文章内图片序号 |
| `{extension}` | `jpg`、`png`、`gif` |

### 速率限制

| 字段 | UI 标签 | 配置键 | 默认值 | 描述 |
|------|--------|--------|--------|------|
| `sleep_request` | Sleep（秒） | `extractor.bilibili.sleep-request` | `"3.0-6.0"` | HTTP 请求间隔时间（秒）。支持区间字符串（如 `"3.0-6.0"` 表示在该区间内随机取值）。 |

---

## 配置文件结构

各站点设置映射到 gallery-dl 的 `config.json`：

```json
{
  "extractor": {
    "pixiv": {
      "refresh-token": "token_string",
      "cookies": "/gallerydl-config/cookies/pixiv.txt",
      "auto-enable-on-import": true,
      "include": "artworks",
      "tags": "japanese",
      "ugoira": "zip",
      "max-posts": null,
      "sleep-request": 2.5,
      "directory": ["pixiv", "{user[account]}", "{id}"],
      "filename": "{id}_p{num}.{extension}"
    },
    "twitter": {
      "cookies": "/gallerydl-config/cookies/twitter.txt",
      "auto-enable-on-import": false,
      "strategy": "tweets",
      "include": "timeline",
      "retweets": false,
      "replies": false,
      "cards": true,
      "videos": true,
      "text-tweets": false,
      "quoted": false,
      "pinned": false,
      "previews": false,
      "articles": false,
      "max-posts": null,
      "directory": ["twitter", "{user[name]}"],
      "filename": "{tweet_id}_{num}.{extension}"
    },
    "iwara": {
      "username": "your_username",
      "password": "your_password",
      "cookies": "/gallerydl-config/cookies/iwara.txt",
      "auto-enable-on-import": false,
      "include": "user-videos",
      "format": "Source, 1080, 720",
      "directory": ["iwara", "{user[name]}"],
      "filename": "{date} {id} {title[:200]} {filename}.{extension}"
    },
    "danbooru": {
      "username": null,
      "password": null,
      "api-key": null,
      "cookies": "/gallerydl-config/cookies/danbooru.txt",
      "auto-enable-on-import": false,
      "external": false,
      "metadata": false,
      "directory": ["danbooru", "{artist[name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "pinterest": {
      "auto-enable-on-import": false,
      "domain": null,
      "stories": true,
      "videos": true,
      "sections": true,
      "directory": ["pinterest", "{category}", "{user}", "{board[name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "lofter": {
      "auto-enable-on-import": false,
      "directory": ["lofter", "{blog_name}", "{id}"],
      "filename": "{id}_{num}.{extension}"
    },
    "weibo": {
      "cookies": "/gallerydl-config/cookies/weibo.txt",
      "auto-enable-on-import": false,
      "videos": true,
      "retweets": false,
      "gifs": true,
      "livephoto": false,
      "movies": false,
      "text": false,
      "directory": ["weibo", "{user[screen_name]}"],
      "filename": "{id}_{num}.{extension}"
    },
    "bilibili": {
      "auto-enable-on-import": false,
      "livephoto": true,
      "sleep-request": "3.0-6.0",
      "directory": ["bilibili", "{user[name]}", "{id}"],
      "filename": "{id}_{num}.{extension}"
    }
  }
}
```

## 键值映射（API <-> 配置）

管理后台 API 使用驼峰命名（camelCase）。下表列出 API 字段与 gallery-dl 配置键的对应关系。

### Pixiv 映射

| API 字段 | 配置键 |
|----------|--------|
| `refresh_token` | `refresh-token` |
| `cookies_path` | `cookies` |
| `cookie_content` |（写入 cookies 文件） |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |
| `include` | `include` |
| `tags` | `tags` |
| `ugoira` | `ugoira` |
| `sleep_request` | `sleep-request` |
| `max_posts` | `max-posts` |
| `metadata` | `metadata` |
| `metadata_bookmark` | `metadata-bookmark` |
| `captions` | `captions` |
| `comments` | `comments` |
| `sanity` | `sanity` |

### Twitter 映射

| API 字段 | 配置键 |
|----------|--------|
| `cookies_path` | `cookies` |
| `cookie_content` |（写入 cookies 文件） |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |
| `strategy` | `strategy` |
| `include` | `include` |
| `retweets` | `retweets` |
| `replies` | `replies` |
| `cards` | `cards` |
| `videos` | `videos` |
| `text_tweets` | `text-tweets` |
| `quoted` | `quoted` |
| `pinned` | `pinned` |
| `previews` | `previews` |
| `articles` | `articles` |
| `max_posts` | `max-posts` |

### Iwara 映射

| API 字段 | 配置键 |
|----------|--------|
| `cookies_path` | `cookies` |
| `cookie_content` |（写入 cookies 文件） |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `username` | `username` |
| `password` | `password` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |
| `include` | `include` |
| `format` | `format` |

### Danbooru 映射

| API 字段 | 配置键 |
|----------|--------|
| `username` | `username` |
| `password` | `password` |
| `api_key` | `api-key` |
| `cookies_path` | `cookies` |
| `cookie_content` |（写入 cookies 文件） |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `external` | `external` |
| `metadata` | `metadata` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |

### Pinterest 映射

| API 字段 | 配置键 |
|----------|--------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `domain` | `domain` |
| `stories` | `stories` |
| `videos` | `videos` |
| `sections` | `sections` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |

### LOFTER 映射

| API 字段 | 配置键 |
|----------|--------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |

### 微博映射

| API 字段 | 配置键 |
|----------|--------|
| `cookies_path` | `cookies` |
| `cookie_content` |（写入 cookies 文件） |
| `auto_enable_on_import` | `auto-enable-on-import` |
| `videos` | `videos` |
| `retweets` | `retweets` |
| `gifs` | `gifs` |
| `livephoto` | `livephoto` |
| `movies` | `movies` |
| `text` | `text` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |

### Bilibili 映射

| API 字段 | 配置键 |
|----------|--------|
| `auto_enable_on_import` | `auto-enable-on-import` |
| `livephoto` | `livephoto` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 -> 分割为数组） |
| `sleep_request` | `sleep-request` |

## 命名模板集成

此处定义的各站点 gallery-dl 配置提供基本目录模式。命名模板（**设置 -> Naming Templates**）可以按创作者覆盖：

1. 创建命名模板，指定 `source`、`template` 模式和可选的 `is_default`
2. 下载任务运行时，provider 的 `build_gallerydl_config()` 检查匹配的命名模板
3. 如果找到且 `is_default=True`，模板模式会覆盖基础的 `directory` 配置

这使得无需更改全局设置即可为每个创作者自定义文件组织方式。
