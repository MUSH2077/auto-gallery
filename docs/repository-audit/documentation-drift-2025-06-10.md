# Documentation Drift Report

**Date**: 2025-06-10
**Method**: Line-by-line comparison of 39 documentation files against current implementation
**Commit baseline**: `61d8034`

---

## Summary

| Classification | Count | Files |
|---------------|-------|-------|
| Authoritative & current | 18 | Core constraints, recent audits, setup docs |
| Useful but incomplete | 8 | Missing newer providers/services |
| **Stale** | **7** | Claims don't match code |
| **Contradictory** | **4** | Directly contradict implementation |
| Duplicate | 0 | — |
| Historical/archive | 2 | release-checklist, legacy plans |
| Should be deleted | 0 | — |

---

## STALE Documents

### 1. `.claude/CLAUDE.md` — provider list missing weibo + bilibili

**Stale claims**:
- Line 264: "All registered providers (Pixiv, Danbooru, Iwara, X, Pinterest, LOFTER) support gallery-dl download." — X does NOT support download anymore. Weibo and Bilibili are missing from the list.
- Lines 341-351: Provider file listing omits `providers/weibo.py` and `providers/bilibili.py`.

**Code reality**: `providers/__init__.py` registers 11 providers including WeiboProvider and BilibiliProvider. `registry.list_downloadable()` returns `['pixiv', 'iwara', 'danbooru', 'pinterest', 'lofter', 'weibo', 'bilibili']` (7 sources). X has `can_download=False`.

### 2. `docs/providers.md` — X/Twitter downloadable status

**Stale claims**:
- Line 71: "All 8 downloadable providers (Pixiv, **X**, Iwara, Danbooru, Weibo, Bilibili, Pinterest, Lofter)" — X is not downloadable. Count should be 7.
- Line 99: "`capabilities.can_download`: True" for X/Twitter — should be False.
- Line 102: "Uses gallery-dl Twitter extractor with cookie-based auth" — misleading; the extractor code exists but is never invoked by the download pipeline.

**Code reality**: `XProvider.capabilities.can_download = False`, `XProvider.capabilities.supports_gallerydl = False` (set in commit `c54779c`).

### 3. `docs/providers.zh.md` — identical stale claims to providers.md

Lines 71, 99, 102 in Chinese version contain the same stale information about X/Twitter.

### 4. `.claude/constraints/security.md` — media endpoint auth boundary undocumented

**Stale claim**: The constraint doesn't mention media endpoint authentication at all.

**Code reality**: Commit `61d8034` added `RequireAdmin` to `/media/preview/` and `/media/original/`. `/media/thumb/` is intentionally open. The security constraint should document this split.

### 5. `.claude/CLAUDE.md` — service layer completely undocumented

**Stale claim**: CLAUDE.md contains zero references to any file in `services/`.

**Code reality**: 16 service files exist: `cache.py`, `creator.py`, `creator_dedup.py`, `danbooru.py`, `download.py`, `job_manifest.py`, `job_state.py`, `locks.py`, `log_buffer.py`, `proxy.py`, `redis_client.py`, `search.py`, `settings.py`, `subscription.py`, `subscription_enqueue.py`, `thumbnail.py`.

### 6. `.claude/skills/backend-architecture.md` — missing service + job layer details

**Stale claim**: Documents the layered architecture at a high level without describing the actual service and job modules.

**Code reality**: 16 services and 5 job modules exist but are not documented in the architecture skill.

### 7. `docs/gallerydl-config.md` + `.zh.md` — X/Twitter extractor config documented as active

**Stale claim**: The gallery-dl configuration docs describe Twitter extractor settings as configurable and usable.

**Code reality**: X/Twitter extractor configuration exists in `config.json` and the admin UI, but the download pipeline never invokes it because `XProvider.can_download = False`. The docs should note this.

---

## CONTRADICTORY Documents

### 1. `docs/providers.md` line 99 vs `providers/x.py:16-22`

| Document says | Code says |
|---------------|-----------|
| `X.capabilities.can_download = True` | `can_download = False` |
| `X.capabilities.supports_gallerydl = True` | `supports_gallerydl = False` |

### 2. `CLAUDE.md` self-contradiction on X/Twitter

| Line 105 | Line 264 |
|----------|----------|
| "Placeholder with `can_download = False`" | "X supports gallery-dl download" |

The same document contradicts itself on two different lines.

### 3. `CLAUDE.md` line 264 vs `registry.list_downloadable()`

| Document (6 sources) | Code (7 sources) |
|----------------------|-------------------|
| Pixiv, Danbooru, Iwara, X, Pinterest, LOFTER | pixiv, iwara, danbooru, pinterest, lofter, **weibo**, **bilibili** |

Weibo and Bilibili are fully implemented but not mentioned. X is listed but not downloadable.

### 4. `docs/providers.md` line 71 provider count vs actual

| Document | Code |
|----------|------|
| 8 downloadable providers (includes X) | 7 downloadable providers (X excluded) |

---

## AUTHORITATIVE & CURRENT (18 documents)

| Document | Why current |
|----------|------------|
| `.claude/constraints/tech-stack.md` | Prescriptive — all technologies match |
| `.claude/constraints/docker.md` | Prescriptive — service list matches docker-compose.yaml |
| `.claude/constraints/source-abstraction.md` | Prescriptive — naming rules enforced |
| `.claude/constraints/danbooru-reference.md` | Prescriptive — Danbooru role constraints valid |
| `.claude/constraints/deduplication.md` | Prescriptive — dedup policy unchanged |
| `.claude/constraints/filesystem-paths.md` | Prescriptive — path structure unchanged |
| `.claude/constraints/creator-display-name.md` | Prescriptive — display name hierarchy valid |
| `.claude/constraints/source-colors.md` | Prescriptive — color palette stable |
| `.claude/constraints/metadata-detection.md` | Prescriptive — detection rules stable |
| `.claude/constraints/verification.md` | Prescriptive — verification discipline unchanged |
| `.claude/constraints/deployment.md` | Prescriptive — deployment discipline unchanged |
| `.claude/constraints/frontend-discipline.md` | Prescriptive — frontend rules stable |
| `.claude/constraints/commit-checklist.md` | Prescriptive — checklist rules stable |
| `.claude/constraints/client-api.md` | Prescriptive — API design rules stable |
| `.claude/constraints/remote-access.md` | Prescriptive — network architecture rules stable |
| `.claude/skills/provider-design.md` | Prescriptive — provider interface stable |
| `.claude/skills/nas-deployment.md` | Prescriptive — NAS deployment rules stable |
| `docs/repository-audit/*.md` (4 files) | Generated today — authoritative by construction |

---

## USEFUL BUT INCOMPLETE (8 documents)

| Document | Gap |
|----------|-----|
| `.claude/CLAUDE.md` | Missing weibo/bilibili; missing service layer; self-contradictory on X |
| `docs/architecture.md` + `.zh.md` | Service and job layers not documented |
| `docs/setup.md` + `.zh.md` | Missing weibo/bilibili provider config notes |
| `docs/development.md` + `.zh.md` | Missing test DB fixture and service testing guidance |
| `docs/risks.md` + `.zh.md` | X/Twitter risk status should update from "placeholder" to "explicitly disabled" |
| `.claude/skills/backend-architecture.md` | Missing services/ and jobs/ layer documentation |
| `.claude/skills/gallerydl-integration.md` | X/Twitter extractor noted as active but is disabled |
| `README.md` | Provider count and capabilities summary needs update |

---

## HISTORICAL / ARCHIVE (2 documents)

| Document | Reason |
|----------|--------|
| `docs/release-checklist.md` | Per-release checklist — historical reference |
| `.claude/plans/` (legacy) | Archived implementation plans |

---

## SHOULD BE DELETED

**None.** All 39 documents serve a purpose.

---

## Prioritized Remediation Plan

| Priority | Document | Issue | Effort |
|----------|----------|-------|--------|
| **P0** | `docs/providers.md` + `.zh.md` | X/Twitter `can_download=True` is WRONG | Small |
| **P0** | `.claude/CLAUDE.md` | Self-contradictory on X status (lines 105 vs 264) | Small |
| **P1** | `.claude/CLAUDE.md` | Add weibo, bilibili to provider list | Small |
| **P1** | `.claude/CLAUDE.md` | Add service layer documentation section | Medium |
| **P1** | `docs/providers.md` + `.zh.md` | Update downloadable count (8→7), remove X | Small |
| **P2** | `.claude/skills/backend-architecture.md` | Add services/ and jobs/ layer descriptions | Medium |
| **P2** | `docs/architecture.md` + `.zh.md` | Add service layer to data flow section | Medium |
| **P2** | `.claude/constraints/security.md` | Document media endpoint auth boundaries | Small |
| **P3** | `docs/risks.md` + `.zh.md` | Update X/Twitter risk status | Small |
| **P3** | `README.md` | Update provider capabilities summary | Small |
| **P3** | `.claude/skills/gallerydl-integration.md` | Note X/Twitter extractor disabled | Small |
