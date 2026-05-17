# gallery-dl 提取器配置参考

## 概述

auto-gallery 通过管理后台 **设置 → gallery-dl 配置** 管理各站点的 gallery-dl 提取器设置。配置持久化到 gallery-dl 的 `config.json` 文件中，位于 `extractor.<source>` 段落下。

本文档描述每个已支持站点的所有可用配置选项。

---

## Pixiv

**状态**：完全支持（gallery-dl 内置）  
**提取器键**：`extractor.pixiv`  
**URL 模式**：`pixiv.net/en/artworks/<id>`、`pixiv.net/users/<id>`

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
| `ugoira` | Download Ugoira | `extractor.pixiv.ugoira` | `true` / `false` | 是否下载动画 ugoira（根据后处理设置存储为 ZIP、MP4 或 GIF）。 |
| `max_posts` | Max Posts | `extractor.pixiv.max-posts` | 整数或留空 | 每次任务最大下载作品数。留空 = 不限制。 |

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

### 内容选择

| 字段 | UI 标签 | 配置键 | 可选值 | 描述 |
|------|--------|--------|--------|------|
| `include` | Include | `extractor.twitter.include` | `timeline`、`media`、`tweets`、`likes` | 从用户 URL 下载的内容类型。`timeline` 是标准用户时间线。`media` 仅下载媒体推文。`likes` 需要认证。 |
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

认证优先级：用户名+密码 > cookies > 未认证。未认证只能下载公开、非年龄限制内容。

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

## 配置文件结构

各站点设置映射到 gallery-dl 的 `config.json`：

```json
{
  "extractor": {
    "pixiv": {
      "refresh-token": "token_string",
      "cookies": "/gallerydl-config/cookies/pixiv.txt",
      "include": "artworks",
      "tags": "japanese",
      "ugoira": true,
      "max-posts": null,
      "sleep-request": 2.5,
      "directory": ["pixiv", "{user[account]}", "{id}"],
      "filename": "{id}_p{num}.{extension}"
    },
    "twitter": {
      "cookies": "/gallerydl-config/cookies/twitter.txt",
      "include": "timeline",
      "retweets": false,
      "replies": false,
      "cards": true,
      "videos": true,
      "text-tweets": false,
      "quoted": false,
      "max-posts": null,
      "directory": ["twitter", "{user[name]}"],
      "filename": "{tweet_id}_{num}.{extension}"
    },
    "iwara": {
      "username": "your_username",
      "password": "your_password",
      "cookies": "/gallerydl-config/cookies/iwara.txt",
      "format": "Source, 1080, 720",
      "directory": ["iwara", "{user[name]}"],
      "filename": "{date} {id} {title[:200]} {filename}.{extension}"
    }
  }
}
```

## 键值映射（API ↔ 配置）

管理后台 API 使用驼峰命名（camelCase）。下表列出 API 字段与 gallery-dl 配置键的对应关系。

### Pixiv 映射

| API 字段 | 配置键 |
|----------|--------|
| `refresh_token` | `refresh-token` |
| `cookies_path` | `cookies` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 → 分割为数组） |
| `include` | `include` |
| `tags` | `tags` |
| `ugoira` | `ugoira` |
| `sleep_request` | `sleep-request` |
| `max_posts` | `max-posts` |

### Twitter 映射

| API 字段 | 配置键 |
|----------|--------|
| `cookies_path` | `cookies` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 → 分割为数组） |
| `include` | `include` |
| `retweets` | `retweets` |
| `replies` | `replies` |
| `cards` | `cards` |
| `videos` | `videos` |
| `text_tweets` | `text-tweets` |
| `quoted` | `quoted` |
| `max_posts` | `max-posts` |

### Iwara 映射

| API 字段 | 配置键 |
|----------|--------|
| `cookies_path` | `cookies` |
| `username` | `username` |
| `password` | `password` |
| `filename` | `filename` |
| `directory` | `directory`（字符串 → 分割为数组） |
| `format` | `format` |

## 命名模板集成

此处定义的各站点 gallery-dl 配置提供基本目录模式。命名模板（**设置 → Naming Templates**）可以按创作者覆盖：

1. 创建命名模板，指定 `source`、`template` 模式和可选的 `is_default`
2. 下载任务运行时，provider 的 `build_gallerydl_config()` 检查匹配的命名模板
3. 如果找到且 `is_default=True`，模板模式会覆盖基础的 `directory` 配置

这使得无需更改全局设置即可为每个创作者自定义文件组织方式。
