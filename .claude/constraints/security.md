# Security Constraint

## Rule

Security constraints are non-negotiable. Review these before implementing any feature that touches subprocess, file paths, URLs, or authentication.

## Subprocess execution

- `shell=True` is FORBIDDEN in all `subprocess` calls
- gallery-dl must be executed via `subprocess.Popen([...], shell=False)`
- Arguments must be passed as a list, never as a single command string

Example — correct:
```python
subprocess.Popen(["gallery-dl", "--config", config_path, url], shell=False)
```

Example — FORBIDDEN:
```python
subprocess.Popen(f"gallery-dl --config {config_path} {url}", shell=True)
```

## URL validation

- All source URLs must be validated before creating download jobs
- Validation must use the provider's `validate_url(url)` method
- URLs that fail validation must be rejected at the API boundary, not in the worker

## Authentication (Phase 1-5: Admin only)

- `/api/v1/admin/*` routes require authentication
- Admin-web implements simple token or API key auth
- `ADMIN_PASSWORD` from `.env` for initial admin setup

## Authentication (Phase 6+: Multi-user with JWT)

When user model is added, migrate to JWT:

- `POST /api/v1/auth/login` → `{access_token, refresh_token}`
- `POST /api/v1/auth/refresh` → new `access_token`
- Access token: short-lived (15 min), Refresh token: long-lived (30 days)
- Refresh token rotation: each refresh invalidates previous refresh token
- Password hashing: `bcrypt` or `argon2`
- Token storage on client: platform secure storage (not localStorage)
- Worker-to-backend communication: service account or internal token (future)
- All user-scoped data queries must filter by `user_id` from JWT claims
- Admin routes require `role=admin` claim in JWT

## Authorization

- `admin` role: full system access
- `user` role: own subscriptions, own albums, accessible works
- `creator`, `work`, `asset`, `tag` data is shared (readable by all authenticated users)
- `subscription`, `album`, `download_job` data is user-scoped
- Never rely on client-side role checks; enforce at API boundary

## Path exposure

- Clients (admin-web, future Flutter) must never receive real NAS filesystem paths
- All media access must go through controlled `/media/...` endpoints
- API responses must contain only relative URL paths, never absolute filesystem paths

## Network exposure

- Do not expose the system directly to the public internet in the first version
- All services communicate on the internal Docker network
- Only the admin-web port and backend API port may be exposed on the NAS LAN
- Meilisearch, PostgreSQL, Redis ports must not be exposed outside Docker

## Sensitive data

- gallery-dl cookies/configs may contain session tokens — store under `GALLERYDL_CONFIG_ROOT` with restricted permissions
- Database passwords in `.env`, never in application code
- `.env` must be in `.gitignore`

## CORS

admin-web runs on a different port from backend. CORS must be explicitly configured:

- `BACKEND_PORT` (e.g., 8818) and `ADMIN_WEB_PORT` (e.g., 3000) both serve on LAN
- CORS origins: allow only `http://<nas-ip>:<ADMIN_WEB_PORT>` (not wildcard)
- In development: allow `http://localhost:3000`
- Methods: GET, POST, PUT, PATCH, DELETE
- Credentials: True (for auth cookies/tokens if used)

## /media file serving

- All media access through `/media/...` endpoints only
- Support HTTP Range requests (required for video seeking)
- Return `Content-Type`, `Content-Length`, `Accept-Ranges: bytes`
- Do not expose real filesystem paths in headers or responses
- Consider rate limiting on `/media/` in production (prevent NAS I/O saturation)
- Streaming response for large files — never buffer full file in memory

## Why

gallery-dl processes arbitrary URLs from source platforms. `shell=True` with unsanitized input is a command injection vector. URL validation at the API boundary prevents malicious job creation. Path exposure leaks NAS topology. These are not theoretical — a media downloader that shells out to external tools with user-supplied URLs is a high-risk surface.
