# Client API Constraint

## Rule

The backend must serve both the admin-web and future remote clients (Flutter/mobile). All API design must account for multi-user scenarios, mobile bandwidth constraints, and client-friendly response shapes.

## User Model (required before any client)

The system must support multiple user accounts:

```
user
  - id: UUID
  - username: String (unique)
  - email: String (unique, optional for v1)
  - hashed_password: String
  - role: Enum(admin, user)
  - is_active: Boolean
  - created_at, updated_at
```

**Permission levels**:
- `admin`: full access to all data, system settings, job management, all creators/works
- `user`: access to own subscriptions, own albums, public works; cannot manage system settings or trigger downloads for other users

**Data isolation rules**:
- `subscription`: scoped to user (add `user_id` foreign key; drop the `unique(creator_id)` constraint — multiple users can subscribe to the same creator)
- `album`: scoped to user (add `user_id` foreign key)
- `download_job`: scoped to subscription → user (inherited)
- `creator`, `work`, `asset`, `tag`: **shared across users** (do not duplicate creator records per user; works are downloaded once and referenced by all)

## Authentication (client-ready)

Must support both admin-web and remote client:

- **JWT-based** (not simple API key for client routes)
- Login endpoint: `POST /api/v1/auth/login` → returns `access_token` + `refresh_token`
- Token refresh: `POST /api/v1/auth/refresh` → new `access_token`
- Access token: short-lived (15 min), refresh token: long-lived (30 days)
- Admin-web routes (`/api/v1/admin/*`) require `role=admin`
- Client/user routes require authenticated user (any role)
- Public routes (if any): health, media serving (may require token for media)

**Transition plan**:
- Phase 1-5: simple API key for admin routes only (no user model yet)
- Phase 6+: add user model, JWT auth, migrate admin auth to JWT with admin role
- Do NOT implement two parallel auth systems; replace API key with JWT when user model arrives

## Client-facing API groups

In addition to admin routes, add client-facing route groups:

```
/api/v1/auth           Login, refresh, logout, me
/api/v1/me/
  /subscriptions       My subscriptions
  /albums              My albums/collections
  /feed                Recent works from subscribed creators
/api/v1/creators       Browse, view creator detail (public)
/api/v1/works          Browse works (public), view work detail
/api/v1/search         Search across accessible works
/api/v1/albums         CRUD for own albums (scoped to user)
/media                 Media serving (may require auth token)
```

Admin-only routes remain at `/api/v1/admin/*`.

## Album / Collection model

Users need to organize works into albums:

```
album
  - id: UUID
  - user_id: FK → user
  - title: String
  - description: Text (nullable)
  - is_public: Boolean (default false)
  - cover_asset_id: FK → asset (nullable)
  - sort_order: Integer
  - created_at, updated_at

album_work
  - album_id: FK → album
  - work_id: FK → work
  - sort_order: Integer
  - added_at: DateTime
  - unique(album_id, work_id)
```

Albums are per-user. A user cannot modify another user's albums. Admin can view all albums.

## Mobile-friendly response design

Client API responses must be optimized for mobile:

**Thumbnails**:
- Every asset response must include thumbnail URLs at multiple sizes
- Generate thumbnails at import time: `thumb_sm` (200px), `thumb_md` (600px), `thumb_lg` (1200px)
- Thumbnail URL pattern: `/media/thumb/<size>/<asset_id>.jpg`
- Client fetches appropriate size; never downloads full image for grid views

**Pagination**:
- Default page size: 20 items (mobile), 50 items (admin-web)
- Max page size: 100 items
- Cursor-based for feeds and timelines (stable under new data arrival)
- Offset-based for fixed lists (albums, subscriptions)

**Response envelopes**:
```json
{
  "data": [...],
  "cursor": "eyJsYXN0X2lkIjogIi4uLiJ9",
  "has_more": true,
  "total": 1523
}
```

## Client ↔ Server interaction pattern

- Client polls for new data (REST); no WebSockets in v1
- Subscription sync status: poll `GET /api/v1/me/subscriptions/:id` for `last_synced_at`
- Download progress: poll `GET /api/v1/download-jobs/:id` for status + progress
- Client never triggers downloads directly; it requests subscription sync via `POST /api/v1/me/subscriptions/:id/sync` (creates a download job, admin approval may be required)

## Offline considerations (v2+)

Not required for v1. But API design should not preclude:
- Timestamp-based incremental sync (`?updated_since=2026-05-01T00:00:00Z`)
- ETag / If-None-Match support on media endpoints
- These are deferred but must not be architecturally blocked
