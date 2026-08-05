# UI behavior to API capability matrix

This matrix defines the supported LAN surface. It intentionally excludes
arbitrary command execution, arbitrary filesystem reads, internal Redis
signals, database access, and worker implementation details.

| Product behavior | Public HTTP capability | Realtime event |
| --- | --- | --- |
| Sign in and account preferences | `/api/v1/auth/*` | — |
| Browse/search works, creators, tags, repositories, and subscriptions | `/api/v1/search`, `/api/v1/works`, `/api/v1/creators`, `/api/v1/tags`, `/api/v1/repositories`, `/api/v1/subscriptions` | — |
| Read thumbnails, posters, originals, and seekable video | `/media/*`, `/api/v1/works/{work_id}/assets`, `/api/v1/works/{work_id}/assets/{asset_id}/playback-ticket` | — |
| Upload media | `/api/v1/upload/*` multipart operations | task status/progress |
| Validate and import Danbooru references | `/api/v1/reference/*` | task status/progress |
| Create/edit subscriptions and synchronize repositories | `/api/v1/subscriptions/*`, `/api/v1/repositories/*` | task status/progress |
| Inspect and control downloads/imports | `/api/v1/download-jobs/*`, `/api/v1/import-jobs/*`, `/api/v1/tasks/*` | task status/progress |
| Inspect and operate schedules | `/api/v1/scheduler/*` | task status/progress |
| Curate, deduplicate, restore, and purge library data | `/api/v1/curation/*`, `/api/v1/admin/dedup/*`, relevant work/creator actions | task status/progress |
| Backup, restore, rebuild indexes, and inspect integrity | `/api/v1/admin/*` | task status/progress |
| Inspect service health, logs, storage, and workbench status | `/api/v1/system/*` | status change |
| Manage users and permissions | `/api/v1/users/*` | — |
| Subscribe to selected live topics | WebSocket ticket endpoint plus `/api/v1/ws` | connected, subscribed, unsubscribed, pong, status change, progress |

Each OpenAPI operation declares authentication, required permission modules,
administrator-only status, background-task behavior, destructive risk, and
deprecation metadata through `x-*` fields.
