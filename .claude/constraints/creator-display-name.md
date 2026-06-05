# Creator Display Name Constraint

## Rule

The canonical `Creator.display_name` set by the admin MUST be used for display everywhere in the system. Never show raw source-level names when a canonical display name exists.

## Name hierarchy

Three levels of creator naming, in priority order:

| Priority | Source | Example | When used |
|----------|--------|---------|-----------|
| 1 (canonical) | `Creator.display_name` | "ASK" | Set by admin via edit modal; shown everywhere |
| 2 (source) | `SourceCreator.display_name` | "ASK" | From source metadata (e.g. Pixiv user name); used as fallback |
| 3 (raw) | `Creator.name` / `SourceCreator.source_creator_id` | "ask_(askzy)" / "1980643" | Internal identifier; never shown to user |

## Display rules

### Every UI component showing a creator name MUST follow this pattern:

```typescript
// Frontend
{creator.display_name || creator.name}
{c.creator_display_name || c.creator_name}
```

```python
# Backend API responses — always include display_name when returning creator data
display = creator.display_name or source_creator.display_name or creator.name
```

### Specific pages that must use display_name:

| Page | Field | Pattern |
|------|-------|---------|
| Creator list | Name column | `display_name \|\| name` |
| Creator detail | Hero title | `display_name \|\| name` |
| Creator detail | Subtitle (raw name) | `name` only when differs from display_name |
| Subscription list | Creator column | `creator_display_name \|\| creator_name` |
| Subscription detail | Creator link | `creator_display_name \|\| creator_name` |
| Work browser | Creator badge | API returns `creator_name` = `SourceCreator.display_name` |
| Search results | Creator name | API returns `creator_name` = `SourceCreator.display_name` |
| Data center | Storage Top 20 | `display_name \|\| name` |
| Notification Center | Batch job results | `creator_name` from batch import response |
| Download/Import jobs | Job list | `creator_name` from API (populate from `Creator.display_name`) |

### Backend API requirements:

When an API endpoint returns creator information, it MUST include the canonical `display_name`:

```python
# Query pattern — always join through to creators for display_name
stmt = (
    select(
        WorkSource,
        SourceCreator.display_name.label("creator_name"),
        Creator.display_name.label("creator_display_name"),
    )
    .join(SourceCreator, ...)
    .outerjoin(Creator, Creator.id == SourceCreator.creator_id)
)
```

### When admin changes display_name:

All pages must reflect the change immediately (invalidate relevant TanStack Query cache keys):
- `queryKeys.creators.detail(id)`
- `queryKeys.creators.all`
- `queryKeys.subscriptions.all`

## Filesystem vs DB names

The filesystem directory names under `downloads/` are determined by naming templates and may NOT match any database name. Never use filesystem directory names as display names. Always resolve through `work_sources → source_creators → creators`:

```python
# Correct — cross-reference through DB
work_id → work_sources.source_creator_id → source_creators.creator_id → creators.display_name

# Wrong — use filesystem name directly
creator_name = os.path.basename(creator_dir)  # BAD
```

## Why

Creators have different names across platforms. The admin chooses one canonical display name. If different pages show different names for the same creator, it confuses users and undermines the "unified identity" purpose of the creator management system.
