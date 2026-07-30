# Compound search language / 复合搜索语言

auto-gallery uses one server-owned search language across global search,
works, creators, subscriptions, repositories, task operations, scheduler
decisions, and creator pickers. The browser requests parsing and composition
from the API; it does not maintain a second grammar.

auto-gallery 在全局搜索、作品、创作者、订阅、仓库、任务、调度决策和创作者选择器中
使用同一套由服务端维护的搜索语言。浏览器通过 API 请求解析与组合，不维护第二套语法。

## Quick examples / 常用示例

```text
type:work tag:"blue archive" source:pixiv is:favorite
type:creator has:subscription -source:x
type:repo is:auth-error
type:subscription is:never-synced
kind:download status:failed source:pixiv
is:due -source:x
posted:>=2026-01-01 posted:<2026-07-01 sort:posted-desc
```

- Plain Unicode words are full-text terms. Quote values that contain spaces.
  普通 Unicode 文本按全文词处理；包含空格的值需要引号。
- Different qualifier keys are `AND`; repeated values of one key are `OR`.
  不同限定词字段按 `AND` 组合，同一字段的重复值按 `OR` 组合。
- `-tag:`, `-repo:`, `-creator:`, `-source:`, `-is:`, and `-has:` exclude
  matches. These forms also accept the full-width `：` colon on input.
  这些前缀用于排除；输入时也兼容全角冒号 `：`。
- Repository and creator references accept a UUID, canonical URL, source
  account, or unique name. An ambiguous value returns `422` with candidates
  instead of choosing silently.
  仓库与创作者可使用 UUID、规范 URL、来源账号或唯一名称；歧义值返回带候选项的
  `422`，不会静默猜测。
- Tag filters are exact after normalization. Suggestions remain fuzzy.
  标签过滤在规范化后精确匹配，输入建议仍支持模糊检索。

## Qualifiers / 限定词

| Domain | Qualifiers |
|---|---|
| Result type | `type:work\|creator\|tag\|repo\|subscription` |
| Identity | `repo:`, `creator:`, `tag:`, `source:` |
| Work state | `is:favorite\|nsfw\|sfw\|ai\|human\|visible\|trashed` |
| Creator state | `is:active\|inactive\|favorite`, `has:subscription\|repository\|danbooru` |
| Repository state | `is:enabled\|disabled\|auth-ok\|auth-error`, `has:last-sync\|source-creator-id` |
| Subscription state | `is:active\|inactive\|sync-enabled\|sync-disabled\|never-synced` |
| Media | `has:tags\|description\|multiple-assets\|image\|animation\|video` |
| Operations | `status:`, `kind:`, `source:`, `repo:` |
| Scheduler | `is:due\|blocked\|waiting\|manual\|disabled` |
| Dates | `posted:`, `created:`, `updated:`, `synced:` with `<`, `<=`, `=`, `>=`, `>` |
| Sorting | `sort:relevance\|posted-desc\|posted-asc\|created-desc\|created-asc\|updated-desc\|updated-asc\|name-asc\|name-desc\|usage-desc\|last-sync-desc\|last-sync-asc` |

The first version intentionally has no explicit `OR`, parentheses, or
side-effect commands. Search input can only navigate or filter.

第一版有意不提供显式 `OR`、括号和有副作用的命令；搜索输入只能导航或过滤。

## API

```http
GET /api/v1/search?q=type%3Awork%20tag%3A%22landscape%22&scope=global&offset=0&limit=20
POST /api/v1/search/assist
Content-Type: application/json

{
  "before_cursor": "type:work tag:land",
  "after_cursor": "",
  "scope": "works",
  "limit": 10
}
```

`GET /api/v1/search` returns `canonical_query`, parsed tokens, and grouped
`{total, items}` results. Global groups are works, creators, tags,
repositories, and subscriptions, trimmed by permission. `tasks` and
`scheduler` use SQL adapters; local library entities use versioned
Meilisearch indexes. A missing Meilisearch service returns `503`, not an empty
result.

`POST /api/v1/search/assist` returns suggestions, diagnostics, parsed tokens,
and optional server-composed replacements. Invalid dates, incompatible
qualifiers, conflicting states, unknown values, and ambiguous identities
return positional diagnostics.

`GET /api/v1/search` 返回 `canonical_query`、解析 token，以及分组后的
`{total, items}`。全局结果包含作品、创作者、标签、仓库和订阅，并按权限裁剪。
任务和调度使用 SQL 适配器，本地图库实体使用版本化 Meilisearch 索引。
Meilisearch 不可用时返回 `503`，而不是伪装成空结果。

## Index lifecycle / 索引生命周期

Import and mutation paths call shared document builders. A full rebuild creates
versioned staging indexes and swaps them atomically. After upgrading, run
**Data management → Rebuild search index** once before relying on new fields.

导入与编辑路径统一调用共享文档构建器。全量重建先创建版本化暂存索引，再原子切换。
升级后应在“数据管理 → 重建搜索索引”执行一次全量重建，再使用新增字段。

Danbooru remains an explicitly remote query adapter and is never mixed into
local search results.

Danbooru 始终是明确的远端查询适配器，不混入本地搜索结果。
