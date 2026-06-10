# Security Scan Report — auto-gallery

**Date**: 2025-06-10
**Scope**: Full repository (backend, frontend, Docker, config, .claude/)
**Method**: Manual code review of 15 security focus areas (npx scanner unavailable)

---

## Security Grade: B (74/100)

| Area | Score |
|------|-------|
| Secrets management | 6/10 |
| Authorization | 8/10 |
| Input validation | 7/10 |
| Command execution | 6/10 |
| File path safety | 7/10 |
| SQL injection | 7/10 |
| SSRF protection | 6/10 |
| Upload safety | 5/10 |
| Dependency hygiene | 8/10 |
| Configuration security | 6/10 |

---

## Findings Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 2 |
| HIGH | 5 |
| MEDIUM | 7 |
| LOW | 4 |

---

## CRITICAL

### C1: Tar bomb / path traversal in backup restore

- **File**: [api/admin.py:2007-2024](backend/app/api/admin.py#L2007-L2024) (duplicate at line 1851)
- **Evidence**: `file.filename` from user upload used directly in `os.path.join(tmpdir, file.filename)` (line 2015) without sanitization. `tar.extractall(extract_dir)` (line 2024) doesn't validate extracted paths stay within `extract_dir`.
- **Impact**: Attacker uploads `.tar.gz` with `../../../etc/cron.d/backdoor` filenames → arbitrary file write on container filesystem.
- **Fix**: Use fixed filename `upload.tar.gz` (ignore user filename). Validate all tar member paths before extraction: check `os.path.commonpath([extract_dir, resolved_member_path]) == extract_dir`.
- **Confidence**: HIGH

### C2: Database password exposed in subprocess environment

- **File**: [api/admin.py:2038](backend/app/api/admin.py#L2038), [api/admin.py:1769](backend/app/api/admin.py#L1769), [api/admin.py:1885](backend/app/api/admin.py#L1885)
- **Evidence**: `env["PGPASSWORD"] = db["password"]` — DB password visible in `/proc/<pid>/environ` to any process in the container.
- **Impact**: Compromised container process can read DB credentials from procfs.
- **Fix**: Use `~/.pgpass` file with `0600` permissions or `pg_dump --no-password`.
- **Confidence**: HIGH

---

## HIGH

### H1: Default credentials in config.py

- **File**: [config.py:5-11](backend/app/config.py#L5-L11)
- **Evidence**: All defaults `"changeme"`. If `.env` is missing, system starts with well-known credentials.
- **Impact**: `admin:changeme` login, open database access.
- **Fix**: Remove defaults. Raise startup error if required values missing. Partially mitigated by lifespan auto-generation for SECRET_KEY and ADMIN_PASSWORD only.
- **Confidence**: HIGH

### H2: Media endpoint has no authentication

- **File**: [api/media.py:10](backend/app/api/media.py#L10)
- **Evidence**: `router = APIRouter()` — no `dependencies=[RequireAdmin]`.
- **Impact**: Anyone with network access can enumerate/download assets by UUID. UUIDs are unguessable but leak via shared URLs.
- **Fix**: Short-lived signed URLs (`?token=<HMAC>` with expiry); or require auth for `/media/original/` while allowing `/media/thumb/` unauthenticated.
- **Confidence**: HIGH

### H3: Backup restore is destructive without confirmation

- **File**: [api/admin.py:2006-2007](backend/app/api/admin.py#L2006-L2007)
- **Evidence**: Single POST drops entire database schema. No confirmation, no dry-run, no pre-restore backup.
- **Impact**: Accidental POST = irreversible data loss.
- **Fix**: Require `?confirm=DELETE-EVERYTHING` param. Create automatic pre-restore backup.
- **Confidence**: HIGH

### H4: JWT secret_key timing — `changeme` until lifespan completes

- **File**: [config.py:10](backend/app/config.py#L10), [main.py:61](backend/app/main.py#L61)
- **Evidence**: Lifespan generates SECRET_KEY after first request. FastAPI runs lifespan before serving requests, so mitigated. But if lifespan fails (DB not ready), key stays `changeme`.
- **Impact**: Tokens forgeable with well-known key.
- **Fix**: Generate SECRET_KEY in Settings validator, not lifespan. Persist to disk on first run.
- **Confidence**: MEDIUM-HIGH

### H5: Request body redaction list incomplete

- **File**: [main.py:107](backend/app/main.py#L107)
- **Evidence**: Validation error handler redacts `password`, `token`, `secret`, `api_key`, `cookie_content`, `refresh_token`, `cookies`, `cookies_path`. Missing: `current_password`, `new_password`, `access_token`.
- **Impact**: Sensitive values from auth endpoints logged to backend.log.
- **Fix**: Add missing keys. Consider substring matching (any key containing `password`, `token`, `secret`, `key`).
- **Confidence**: MEDIUM

---

## MEDIUM

### M1: SQL f-string table names — safe but fragile

- **File**: [api/admin.py:395](backend/app/api/admin.py#L395), [api/admin.py:568](backend/app/api/admin.py#L568), [api/admin.py:759](backend/app/api/admin.py#L759), [api/admin.py:780](backend/app/api/admin.py#L780)
- **Evidence**: `text(f"SELECT COUNT(*) FROM {table}")` — table names from hardcoded `ENTITIES` dict, not user input. Currently safe.
- **Fix**: Hardcode table names or add comment warning against extending with user input.
- **Confidence**: LOW

### M2: Danbooru URL construction from user input (SSRF limited)

- **File**: [services/danbooru.py:72-96](backend/app/services/danbooru.py#L72-L96)
- **Evidence**: User-provided `source_url` encoded into Danbooru API URL and fetched.
- **Impact**: Limited to danbooru.donmai.us domain. URL validated by provider before reaching this code.
- **Confidence**: LOW

### M3: No tar member path validation during extraction (both sites)

- **File**: [api/admin.py:1878](backend/app/api/admin.py#L1878), [api/admin.py:2024](backend/app/api/admin.py#L2024)
- **Evidence**: `tar.extractall()` without prior member path validation. See C1 for impact.
- **Fix**: Validate all member paths before extraction.
- **Confidence**: HIGH

### M4: Raw asyncpg connection bypasses SQLAlchemy

- **File**: [services/proxy.py:31-66](backend/app/services/proxy.py#L31-L66)
- **Evidence**: Parses DATABASE_URL manually, creates raw asyncpg connection, executes raw SQL.
- **Impact**: Bypasses SQLAlchemy pooling, logging, parameter binding.
- **Fix**: Use existing async_session.
- **Confidence**: LOW

### M5: subprocess.run with args from config — safe with shell=False

- **File**: [jobs/download.py:304-321](backend/app/jobs/download.py#L304-L321)
- **Evidence**: gallery-dl args include `source_url` from DB. With `shell=False` and list args, no injection possible.
- **Fix**: Validate `source_url` starts with `http://` or `https://`.
- **Confidence**: LOW

### M6: JWT in localStorage — XSS risk

- **File**: [admin-web/src/lib/auth.tsx:33](admin-web/src/lib/auth.tsx#L33)
- **Evidence**: `localStorage.getItem("ag_token")` — any XSS on admin-web origin can steal token.
- **Fix**: HttpOnly cookies with `Secure; SameSite=Strict`.
- **Confidence**: MEDIUM

### M7: Redis password in Docker healthcheck command

- **File**: [docker-compose.yaml:112](docker-compose.yaml#L112)
- **Evidence**: Redis URL with password embedded in healthcheck command. Visible in `docker inspect`.
- **Fix**: Use environment variable reference in healthcheck script.
- **Confidence**: LOW

---

## LOW

### L1: `_parse_db_url` returns password to callers
- **File**: [api/admin.py:1696](backend/app/api/admin.py#L1696) — `"password": parsed.password or ""` included in return dict.
- **Fix**: Don't include password; extract only where needed.

### L2: `ENTITIES` dict missing `media` key
- **File**: [api/admin.py:733-740](backend/app/api/admin.py#L733-L740) — no way to clear only media files. Not a vulnerability.

### L3: Inconsistent logging — `print()` in worker_entrypoint.py
- **File**: [worker_entrypoint.py](backend/app/worker_entrypoint.py) — uses `print()` instead of structlog.

### L4: Key redaction list incomplete (see H5)
- Add `current_password`, `new_password`, `access_token`, `credential` to [main.py:107](backend/app/main.py#L107).

---

## 15 Focus Area Assessment

| Area | Grade | Notes |
|------|-------|-------|
| Secrets & credentials | **C** | Defaults, password in env, JWT in localStorage |
| Authorization boundaries | **B+** | All admin routes protected; media intentionally open |
| Insecure direct object refs | **B+** | UUIDs for all IDs; no per-user ownership (single-user) |
| User-controlled file paths | **B-** | Media has guard (new); tar restore doesn't |
| Command execution | **B** | shell=False everywhere; args from config not user input |
| Unsafe deserialization | **A-** | Only json.loads(); tar extraction is only deserialization risk |
| SQL/query construction | **B** | f-string table names safe via dict lookup but fragile pattern |
| SSRF | **B** | Danbooru limited to danbooru.donmai.us; gallery-dl follows user URLs |
| Webhook validation | **N/A** | No webhooks |
| Upload handling | **C-** | Tar bomb + path traversal in backup restore |
| Dependency scripts | **A** | No postinstall scripts; pinned requirements.txt; Dockerfile pinned |
| CI permissions | **N/A** | No CI in repository |
| Claude hooks | **A** | No executable hooks in .claude/; constraints/skills only |
| MCP configuration | **A** | No MCP server configs |
| Repo-local executables | **A** | Only Python entrypoints; no shell scripts except admin-web/entrypoint.sh |

---

## Remediation Priority

| # | Finding | Severity | Effort |
|---|---------|----------|--------|
| 1 | C1: Tar bomb in backup restore | CRITICAL | Medium |
| 2 | C2: Password in subprocess env | CRITICAL | Small |
| 3 | H1: Default credentials | HIGH | Small |
| 4 | H3: Destructive restore no confirmation | HIGH | Small |
| 5 | H4: JWT secret_key timing | HIGH | Small |
| 6 | M3: Tar member path validation | MEDIUM | Small |
| 7 | H5: Sensitive key redaction | HIGH | Small |
