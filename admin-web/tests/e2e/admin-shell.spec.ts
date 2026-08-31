import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";

const me = {
  id: 1,
  username: "ui-review",
  display_name: "UI Review",
  is_admin: true,
  is_active: true,
  permissions: [],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

const acceptanceHost = new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname;

const workbench = {
  updated_at: "2026-07-27T12:00:00Z",
  queue: {
    default: 3,
    scheduled: 1,
    failed: 1,
    active_download_count: 1,
    active_import_count: 0,
    failed_download_count: 1,
    failed_import_count: 0,
    stale_download_count: 0,
    stale_import_count: 0,
    stale_count: 0,
  },
  scheduler: {
    enabled: true,
    mode: "interval",
    timezone: "Asia/Shanghai",
    scan_interval_minutes: 15,
  },
  storage: {
    disk_total_bytes: 1000000000,
    disk_free_bytes: 600000000,
    disk_used_bytes: 400000000,
    disk_used_percent: 40,
    disk_free_percent: 60,
    risk_level: "ok",
  },
  health: {},
  attention: {
    auth_unhealthy_count: 0,
    failed_download_count: 1,
    failed_import_count: 0,
    stale_job_count: 0,
    low_disk_warning: false,
    scheduler_disabled_warning: false,
  },
  recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] },
};

const longDownloadJob = {
  id: "download-job-0123456789",
  subscription_id: "subscription-0123456789",
  subscription_source_id: "source-0123456789",
  subscription_name: "A deliberately long subscription name for responsive overflow validation",
  creator_name: "xianyuliangryo-with-a-very-long-creator-name",
  source: "x",
  source_url: "https://x.com/xianyuliangryo/status/123456789012345678901234567890",
  status: "downloading",
  operation_type: "download",
  retry_count: 0,
  created_at: "2026-07-27T11:40:00Z",
  updated_at: "2026-07-27T11:45:00Z",
  progress_data: {
    stage: "downloading",
    current: 4,
    total: 10,
    percent: 40,
    message: "Downloading media",
  },
  pipeline_stage: "downloading",
  error_log: null,
  outcome: null,
};

const providerFixtures = [
  {
    source_name: "pixiv",
    display_name: "Pixiv",
    capabilities: {
      can_download: true,
      can_import_local: false,
      supports_gallerydl: true,
      supports_tags: true,
      is_reference_only: false,
    },
  },
  {
    source_name: "danbooru_reference",
    display_name: "Danbooru Reference",
    capabilities: {
      can_download: false,
      can_import_local: false,
      supports_gallerydl: false,
      supports_tags: false,
      is_reference_only: true,
    },
  },
] as const;

const REPRESENTATIVE_ROUTES = [
  "/admin",
  "/admin/creators",
  "/admin/subscriptions",
  "/admin/jobs?tab=downloads",
  "/admin/scheduler",
  "/admin/notifications",
  "/admin/works",
  "/admin/tags",
  "/admin/upload",
  "/admin/upload/danbooru",
  "/admin/data-mgmt",
  "/admin/data-mgmt/curation",
  "/admin/data-mgmt/dedup",
  "/admin/system",
  "/admin/sources",
  "/admin/search",
  "/admin/settings/users",
  "/admin/settings",
  "/admin/settings/appearance",
  "/admin/settings/backup",
  "/admin/settings/dedup",
  "/admin/settings/download-defaults",
  "/admin/settings/gallerydl",
  "/admin/settings/gitllery",
  "/admin/settings/logs",
  "/admin/profile",
  "/admin/settings/proxy",
  "/admin/settings/scheduler-defaults",
  "/admin/settings/slideshow",
  "/admin/settings/subscription-defaults",
] as const;

const DYNAMIC_ROUTES = [
  "/admin/works/fixture-work",
  "/admin/creators/fixture-creator",
  "/admin/creators/fixture-creator/mapping",
  "/admin/creators/duplicates",
  "/admin/subscriptions/fixture-subscription",
  "/admin/subscriptions/repositories/fixture-repository",
  "/admin/tags/fixture-tag",
  "/admin/settings/users/1",
] as const;

const QUALITY_ROUTES = [...REPRESENTATIVE_ROUTES, ...DYNAMIC_ROUTES] as const;

const PRIMARY_ADMIN_ROUTES = [
  "/admin/works",
  "/admin/tags",
  "/admin/upload",
  "/admin/upload/danbooru",
  "/admin/creators",
  "/admin/subscriptions",
  "/admin/jobs?tab=downloads",
  "/admin/scheduler",
  "/admin/data-mgmt",
  "/admin/system",
  "/admin/settings",
] as const;

const OPERATION_REDESIGN_AUDIT_ROUTES = [
  ["home", "/admin"],
  ["works", "/admin/works"],
  ["creators", "/admin/creators"],
  ["subscriptions", "/admin/subscriptions"],
  ["repository", "/admin/subscriptions/repositories/fixture-repository"],
  ["operations", "/admin/jobs"],
  ["legacy-scheduler", "/admin/scheduler"],
  ["data", "/admin/data-mgmt"],
  ["system", "/admin/system"],
  ["settings", "/admin/settings"],
] as const;

const NAVIGATION_SELECTION_MATRIX = [
  ["/admin/works", "/admin/works"],
  ["/admin/tags", "/admin/tags"],
  ["/admin/search?q=atlas", "/admin/works"],
  ["/admin/upload", "/admin/upload"],
  ["/admin/upload/danbooru", "/admin/upload/danbooru"],
  ["/admin/creators", "/admin/creators"],
  ["/admin/creators/fixture-creator", "/admin/creators"],
  ["/admin/subscriptions", "/admin/subscriptions"],
  ["/admin/subscriptions/repositories/fixture-repository", "/admin/subscriptions"],
  ["/admin/jobs?tab=imports", "/admin/jobs"],
  ["/admin/scheduler?page=1", "/admin/scheduler"],
  ["/admin/data-mgmt", "/admin/data-mgmt"],
  ["/admin/data-mgmt/curation", "/admin/data-mgmt"],
  ["/admin/data-mgmt/dedup?status=pending", "/admin/data-mgmt"],
  ["/admin/system?tab=sources", "/admin/system"],
  ["/admin/settings", "/admin/settings"],
  ["/admin/settings/users", "/admin/settings"],
  ["/admin/settings/users/1", "/admin/settings"],
  ["/admin/notifications", null],
] as const;

async function installFixtureRoutes(context: BrowserContext) {
  await context.addCookies([{
    name: "ag_token",
    value: "ui-test-token",
    domain: acceptanceHost,
    path: "/",
  }]);
  await context.addInitScript(() => {
    window.localStorage.setItem("ag_token", "ui-test-token");
    if (!window.localStorage.getItem("auto-gallery-lang")) {
      window.localStorage.setItem("auto-gallery-lang", "en");
    }
    if (!window.localStorage.getItem("auto-gallery-theme")) {
      window.localStorage.setItem("auto-gallery-theme", "dark");
    }
  });
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") {
      await route.fulfill({ json: me });
    } else if (path === "/api/v1/works/fixture-work") {
      await route.fulfill({
        json: {
          id: "fixture-work",
          title: "Fixture Work",
          description: "Fixture description",
          posted_at: "2026-07-27T10:00:00Z",
          is_nsfw: false,
          is_ai_generated: false,
          asset_count: 0,
          is_favorite: false,
          creator_id: "fixture-creator",
          creator_name: "fixture-creator",
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (/^\/api\/v1\/works\/fixture-work\/(assets|sources|tags)$/.test(path)) {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/creators/fixture-creator") {
      await route.fulfill({
        json: {
          id: "fixture-creator",
          name: "fixture-creator",
          display_name: "Fixture Creator",
          description: "Fixture description",
          is_active: true,
          is_favorite: false,
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/links") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/creators/fixture-creator/references") {
      await route.fulfill({ json: { pixiv: [], danbooru: null } });
    } else if (path === "/api/v1/creators/fixture-creator/timeline") {
      const year = Number((url.searchParams.get("from_date") || "2026").slice(0, 4));
      const days = year === 2023
        ? []
        : year === 2024
        ? [
            { date: "2024-02-29", total: 1, pixiv: 1, pixiv_ids: ["work-2024-1"] },
          ]
        : year === 2025
        ? [
            { date: "2025-02-04", total: 2, pixiv: 2, pixiv_ids: ["work-2025-1", "work-2025-2"] },
            { date: "2025-09-18", total: 1, x: 1, x_ids: ["work-2025-3"] },
          ]
        : [
            { date: "2026-01-03", total: 2, pixiv: 2, pixiv_ids: ["work-2026-1", "work-2026-2"] },
            { date: "2026-04-12", total: 5, pixiv: 3, x: 2, pixiv_ids: ["work-2026-3"], x_ids: ["work-2026-4"] },
            { date: "2026-07-27", total: 1, x: 1, x_ids: ["work-2026-5"] },
          ];
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          sources: year === 2023 ? ["pixiv"] : ["pixiv", "x"],
          days,
          total: days.reduce((sum, day) => sum + day.total, 0),
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/stats") {
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          total_works: 120,
          total_assets: 168,
          total_tags: 8,
          source_breakdown: [
            { source: "pixiv", count: 82 },
            { source: "x", count: 38 },
          ],
          tag_distribution: [
            { tag: "architectural-light", count: 67 },
            { tag: "long-label-for-responsive-validation", count: 52 },
            { tag: "night", count: 31 },
            { tag: "water", count: 23 },
            { tag: "city", count: 19 },
            { tag: "blue", count: 12 },
            { tag: "seventh-is-not-charted", count: 8 },
          ],
          monthly_frequency: [
            { month: "2023-01", count: 0 },
            { month: "2024-02", count: 1 },
            { month: "2025-01", count: 1 },
            { month: "2025-02", count: 3 },
            { month: "2025-09", count: 2 },
            { month: "2026-01", count: 8 },
            { month: "2026-04", count: 24 },
            { month: "2026-07", count: 7 },
          ],
        },
      });
    } else if (path === "/api/v1/creators/fixture-creator/subscription-overview") {
      await route.fulfill({
        json: {
          creator_id: "fixture-creator",
          subscriptions: [],
          repositories: [],
          summary: {
            subscription_count: 0,
            repository_count: 0,
            enabled_repository_count: 0,
            running_job_count: 0,
          },
        },
      });
    } else if (path === "/api/v1/creators/duplicates") {
      await route.fulfill({ json: { duplicates: [], total: 0 } });
    } else if (path === "/api/v1/subscriptions/fixture-subscription") {
      await route.fulfill({
        json: {
          id: "fixture-subscription",
          creator_id: "fixture-creator",
          name: "Fixture Subscription",
          creator_name: "fixture-creator",
          creator_display_name: "Fixture Creator",
          is_active: true,
          sync_enabled: true,
          sync_interval_hours: 6,
          source_count: 0,
          enabled_source_count: 0,
          running_job_count: 0,
          failed_job_count: 0,
          created_at: "2026-07-27T10:00:00Z",
          updated_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/subscriptions/fixture-subscription/sources") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/repositories/fixture-repository") {
      await route.fulfill({
        json: {
          repository: {
            id: "fixture-repository",
            subscription_id: "fixture-subscription",
            source: "pixiv",
            source_display_name: "Pixiv",
            source_creator_id: "fixture-source",
            source_url: "https://www.pixiv.net/users/1",
            is_enabled: true,
            auth_healthy: true,
            can_download: true,
            supports_gallerydl: true,
            url_valid: true,
            is_repository: true,
          },
          creator: {
            id: "fixture-creator",
            name: "fixture-creator",
            display_name: "Fixture Creator",
            is_favorite: false,
          },
          subscription: {
            id: "fixture-subscription",
            name: "Fixture Subscription",
            is_active: true,
            sync_enabled: true,
            sync_interval_hours: 6,
          },
          provider: {
            source: "pixiv",
            display_name: "Pixiv",
            normalized_url: "https://www.pixiv.net/users/1",
            url_valid: true,
            capabilities: {
              can_download: true,
              can_import_local: true,
              supports_gallerydl: true,
              supports_tags: true,
              is_reference_only: false,
            },
          },
          recent_jobs: [],
          sync_history: [],
          work_total: 13,
          recent_works: [],
        },
      });
    } else if (path === "/api/v1/repositories/fixture-repository/tags") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/repositories/fixture-repository/curation-graph") {
      await route.fulfill({
        json: {
          repository_id: "fixture-repository",
          nodes: [],
          edges: [],
          total: 0,
          offset: 0,
          limit: 100,
        },
      });
    } else if (path === "/api/v1/tags/fixture-tag") {
      await route.fulfill({
        json: {
          id: "fixture-tag",
          normalized_name: "fixture-tag",
          category: "general",
          usage_count: 0,
          top_creators: [],
          created_at: "2026-07-27T10:00:00Z",
        },
      });
    } else if (path === "/api/v1/users/1") {
      await route.fulfill({ json: { ...me, created_at: "2026-07-27T10:00:00Z" } });
    } else if (path === "/api/v1/operations/overview") {
      const view = (url.searchParams.get("view") || "attention") as "attention" | "active" | "resolved";
      await route.fulfill({
        json: {
          view,
          total: 0,
          summary: {
            attention: 0,
            critical: 0,
            warning: 0,
            resolved: 0,
            active: 0,
            resource_limited: 0,
          },
          items: [],
        },
      });
    } else if (path === "/api/v1/system/workbench") {
      await route.fulfill({ json: workbench });
    } else if (path === "/api/v1/system/scheduler-decisions") {
      await route.fulfill({
        json: {
          updated_at: "2026-07-27T12:00:00Z",
          scheduler_enabled: true,
          timezone: "UTC",
          items: [],
        },
      });
    } else if (path === "/api/v1/subscriptions/summaries") {
      const ids = (url.searchParams.get("ids") || "").split(",").filter(Boolean);
      await route.fulfill({
        json: {
          updated_at: "2026-07-27T12:00:00Z",
          items: ids.map((subscriptionId) => ({
            subscription_id: subscriptionId,
            latest_state: { state: "never_synced", status: null },
            active_count: 0,
            attention_count: 0,
            source_count: 0,
            enabled_source_count: 0,
            schedule: {
              configured_mode: "inherit",
              effective_mode: "interval",
              inherited: true,
              timezone: "UTC",
              scheduled_times: null,
              sync_interval_hours: 6,
              next_due_at: "2026-07-27T18:00:00Z",
              oldest_due_at: null,
              due_sources: 0,
              overdue_sources: 0,
              blocked_sources: 0,
            },
          })),
        },
      });
    } else if (path === "/api/v1/download-jobs") {
      await route.fulfill({ json: [longDownloadJob] });
    } else if (path === "/api/v1/tasks") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/import-jobs") {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit: 50 } });
    } else if (path === "/api/v1/search/assist") {
      const body = route.request().postDataJSON() as {
        before_cursor?: string;
        after_cursor?: string;
      };
      const query = `${body.before_cursor || ""}${body.after_cursor || ""}`.trim();
      await route.fulfill({
        json: {
          query,
          canonical_query: query,
          parsed: {
            raw: query,
            canonical: query,
            scope: "global",
            targets: ["works", "creators", "tags", "repositories", "subscriptions"],
            tokens: [],
          },
          diagnostics: [],
          suggestions: [],
          catalog: [],
        },
      });
    } else if (path === "/api/v1/search") {
      const scope = url.searchParams.get("scope") || "global";
      const target = scope === "works" || scope === "creator-picker"
        ? (scope === "creator-picker" ? "creators" : "works")
        : scope;
      const groups = scope === "global"
        ? {
            works: { total: 0, items: [] },
            creators: { total: 0, items: [] },
            tags: { total: 0, items: [] },
            repositories: { total: 0, items: [] },
            subscriptions: { total: 0, items: [] },
          }
        : { [target]: { total: 0, items: [] } };
      const query = url.searchParams.get("q") || "";
      await route.fulfill({
        json: {
          query,
          canonical_query: query,
          parsed: {
            raw: query,
            canonical: query,
            scope,
            targets: Object.keys(groups),
            tokens: [],
          },
          groups,
          total: 0,
          results: [],
          creators: [],
          tags: [],
          repositories: [],
          subscriptions: [],
        },
      });
    } else if (path === "/api/v1/works") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/works/derivative-progress") {
      await route.fulfill({ json: {
        total: 0,
        completed: 0,
        pending: 0,
        processing: 0,
        failed: 0,
        remaining: 0,
        affected_works: 0,
        completion_percent: 100,
        status: "idle",
        last_completed_at: null,
        oldest_unfinished_at: null,
        stall_after_seconds: 300,
      } });
    } else if (path === "/api/v1/creators") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/creators/count") {
      await route.fulfill({ json: { count: 0 } });
    } else if (path === "/api/v1/subscriptions") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/subscriptions/count") {
      await route.fulfill({ json: { count: 0 } });
    } else if (path === "/api/v1/users") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/admin/system-info") {
      await route.fulfill({
        json: {
          version: "fixture",
          downloads_size_mb: 0,
          library_size_mb: 0,
          downloads_free_gb: 500,
          archives_kb: {},
          db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 },
        },
      });
    } else if (path === "/api/v1/admin/storage-breakdown") {
      await route.fulfill({
        json: {
          sources: {},
          creator_tree: [],
          unlinked_repositories: [],
          db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 },
        },
      });
    } else if (path === "/api/v1/admin/integrity-check") {
      await route.fulfill({
        json: { issues: [], db_stats: {}, checked_at: "2026-07-27T12:00:00Z" },
      });
    } else if (path === "/api/v1/system/health") {
      await route.fulfill({
        json: {
          status: "ok",
          services: { postgres: "up", redis: "up", meilisearch: "up" },
          version: "test",
          business: {},
        },
      });
    } else if (path === "/api/v1/admin/auth-status") {
      await route.fulfill({
        json: {
          summary: { total: 1, healthy: 1, unhealthy: 0, unknown: 0 },
          sources: [{
            id: "fixture-source",
            source: "pixiv",
            source_url: "https://www.pixiv.net/users/2048",
            source_creator_id: "2048",
            auth_healthy: true,
            auth_status: "healthy",
            auth_error_reason: null,
            last_auth_checked_at: "2026-07-30T12:00:00Z",
            last_successful_auth: "2026-07-30T12:00:00Z",
            is_enabled: true,
            subscription: { id: "fixture-subscription", name: "Atlas archive", is_active: true, sync_enabled: true },
            creator: { id: "fixture-creator", name: "atlas", display_name: "Atlas Studio" },
          }],
        },
      });
    } else if (path === "/api/v1/admin/gitllery/settings") {
      await route.fulfill({
        json: {
          product_name: "Gitllery",
          product_version: "v1",
          format_id: "gitllery-segment",
          format_revision: 1,
          projection_mode: "shadow",
          build_generation: "segment-r1-fixture",
          managed_by: "deployment_environment",
          read_only: true,
          capabilities: {
            automatic_projection: { enabled: false, reason: "gitllery_shadow_only" },
            reconcile: { enabled: false, reason: "gitllery_shadow_only" },
            backfill: { enabled: false, reason: "gitllery_shadow_only" },
            rebuild: { enabled: false, reason: "gitllery_shadow_only" },
            push: { enabled: false, reason: "gitllery_shadow_only" },
            pull: { enabled: false, reason: "gitllery_shadow_only" },
            verify: { enabled: true, reason: null },
            commit: { enabled: true, reason: null },
          },
          cli: {
            max_works_per_commit: 25,
            max_operations_per_commit: 100,
            token_storage: "client_only",
            server_stores_cli_token: false,
            examples: {
              config: "gitllery config set url http://auto-gallery.test",
              login: "gitllery auth login --username admin",
              status: "gitllery --remote status",
              log: "gitllery --remote log --limit 50",
              verify: "gitllery verify --remote",
              commit: "gitllery --remote commit --message \"curate work\" work favorite 00000000-0000-0000-0000-000000000001 --set on",
            },
          },
          governance_scope: {
            observation: "host_and_auto_gallery",
            enforcement: "auto_gallery_only",
            modifies_other_projects: false,
            modifies_host_configuration: false,
          },
          status: {
            repositories: [{
              repository_id: "pixiv:fixture",
              source: "pixiv",
              creator_dir: "Fixture Creator",
              exists: true,
              behind: 0,
              object_integrity_ok: true,
              drift: [],
              clean: true,
              product_version: "v1",
              format_id: "gitllery-segment",
              format_revision: 1,
              projection_mode: "shadow",
              head_segment: "segment-fixture-head",
              last_complete_commit_id: "00000000-0000-0000-0000-000000000001",
            }],
            missing_repos: 0,
            behind_total: 0,
            needs_reconcile: false,
            product_version: "v1",
            format_id: "gitllery-segment",
            format_revision: 1,
            projection_mode: "shadow",
          },
        },
      });
    } else if (path === "/api/v1/curation/gitllery/verify") {
      await route.fulfill({ json: { status: "enqueued", job_id: "verify-fixture" } });
    } else if (path === "/api/v1/admin/settings") {
      await route.fulfill({
        json: {
          dedup: {},
          subscription_defaults: {
            default_sync_interval_hours: 6,
            scheduler_scan_interval_minutes: 60,
            scheduler_enabled: true,
            schedule_mode: "interval",
            scheduled_times: "",
            timezone: "UTC",
          },
          download_defaults: {
            timeout_seconds: 1800,
            stall_timeout_seconds: 300,
            max_retries: 3,
            retry_backoff_base_seconds: 60,
            max_posts: 0,
            skip_ai_generated: false,
            gallerydl_retries: 3,
            gallerydl_timeout: 30,
            gallerydl_abort: 300,
            download_concurrency: 2,
          },
          proxy: { http_proxy: "", https_proxy: "", no_proxy: "", enabled: false },
        },
      });
    } else if (path === "/api/v1/admin/gallerydl-config") {
      await route.fulfill({
        json: {
          pixiv: {}, twitter: {}, iwara: {}, danbooru: {},
          pinterest: {}, lofter: {}, weibo: {}, bilibili: {}, sources: {},
        },
      });
    } else if (path === "/api/v1/admin/dedup/cases") {
      await route.fulfill({
        json: { items: [], total: 0, offset: 0, limit: 25 },
      });
    } else if (path === "/api/v1/curation/commits") {
      await route.fulfill({ json: { items: [], total: 0 } });
    } else if (path === "/api/v1/curation/purge/preview") {
      await route.fulfill({ json: { work_count: 0, asset_count: 0, bytes_reclaimable: 0, works: [], assets: [] } });
    } else if (path === "/api/v1/curation/rule-suggestions") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/curation/backfill/status") {
      await route.fulfill({ json: { is_complete: false, expected: {}, existing: {}, missing: {} } });
    } else if (path === "/api/v1/curation/gitllery/status") {
      await route.fulfill({
        json: {
          repositories: [],
          missing_repos: 0,
          behind_total: 0,
          needs_reconcile: false,
          product_version: "v1",
          format_id: "gitllery-segment",
          format_revision: 1,
          projection_mode: "shadow",
        },
      });
    } else if (path === "/api/v1/admin/backup/list") {
      await route.fulfill({ json: { backups: [] } });
    } else if (path === "/api/v1/admin/backup/estimate") {
      await route.fulfill({ json: { components: {} } });
    } else if (path === "/api/v1/tags") {
      await route.fulfill({ json: [] });
    } else if (path === "/api/v1/sources") {
      await route.fulfill({ json: { sources: providerFixtures } });
    } else if (path === "/api/v1/system/logs") {
      await route.fulfill({ json: { entries: [], total: 0, levels: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] } });
    } else if (path.includes("/notifications")) {
      await route.fulfill({ json: { items: [], total: 0, unread_count: 0 } });
    } else {
      await route.fulfill({ json: {} });
    }
  });
}

async function expectNoPageOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth <= root.clientWidth;
  })).toBe(true);
}

async function expectUniqueNavigationSelection(
  page: Page,
  sidebarHref: string | null,
) {
  const pathname = new URL(page.url()).pathname;
  const managementHrefs = ["/admin/data-mgmt", "/admin/data-mgmt/curation", "/admin/data-mgmt/dedup"];
  const expectedManagementHref = managementHrefs.includes(pathname) ? pathname : null;
  const contextNavigation = page.locator('[data-page-header] nav[aria-label="Data management sections"]:visible');
  await expect(contextNavigation).toHaveCount(expectedManagementHref ? 1 : 0);
  if (expectedManagementHref) {
    await expect(contextNavigation.locator('[aria-current="page"]')).toHaveCount(1);
    await expect(contextNavigation.locator(`a[href="${expectedManagementHref}"]`)).toHaveAttribute("aria-current", "page");
  }

  const primaryNavigation = page.locator('#admin-sidebar nav[aria-label="Primary navigation"]');
  await expect(primaryNavigation.locator('[aria-current="page"]')).toHaveCount(sidebarHref ? 1 : 0);
  if (sidebarHref) {
    await expect(primaryNavigation.locator(`a[href="${sidebarHref}"]`))
      .toHaveAttribute("aria-current", "page");
  }
}

test.beforeEach(async ({ context }) => {
  await installFixtureRoutes(context);
});

test("operation redesign screenshot and accessibility audit", async ({ page }) => {
  test.setTimeout(600_000);
  const outputDir = process.env.PLAYWRIGHT_AUDIT_DIR || "/tmp/auto-gallery-ui-audit";
  for (const viewport of [
    { name: "desktop", width: 1440, height: 960 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "mobile", width: 390, height: 844 },
  ] as const) {
    await page.setViewportSize(viewport);
    for (const [name, route] of OPERATION_REDESIGN_AUDIT_ROUTES) {
      await page.goto(route);
      await expect(page.locator("#main-content")).toBeVisible();
      await expect(page.getByRole("main")).toHaveCount(1);
      await expectNoPageOverflow(page);
      const results = await new AxeBuilder({ page })
        .include("#main-content")
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();
      expect(
        results.violations.map((violation) => ({
          id: violation.id,
          targets: violation.nodes.map((node) => node.target.join(" ")),
        })),
        `${route} should have no axe violations at ${viewport.width}px`,
      ).toEqual([]);
      await page.screenshot({
        path: `${outputDir}/${viewport.name}-${name}.png`,
        fullPage: true,
      });
    }
  }
});

test("legacy showcase preferences migrate once into standalone slideshow settings", async ({ page }) => {
  let preferencePayload: unknown = null;
  await page.addInitScript(() => {
    window.localStorage.removeItem("auto-gallery-slideshow-v1");
    window.localStorage.setItem("auto-gallery-showcase-v1", JSON.stringify({
      slideDwellMs: 7500,
      slideTransition: "crossfade",
      slideLoop: false,
      slideShowMeta: false,
      layoutMode: "webgl",
      autoplay: true,
    }));
  });
  await page.route("**/api/v1/auth/me/preferences", async (route) => {
    preferencePayload = JSON.parse(route.request().postData() || "{}");
    const parsed = preferencePayload;
    const preferences = parsed && typeof parsed === "object" && "preferences" in parsed
      ? parsed.preferences
      : {};
    await route.fulfill({ json: { preferences } });
  });

  await page.goto("/admin/settings/slideshow");
  await expect(page.getByRole("heading", { level: 1, name: "Slideshow" })).toBeVisible();
  await expect(page.getByLabel("Slide dwell time")).toHaveValue("7500");
  await expect(page.getByRole("button", { name: "Crossfade" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "Loop playback" })).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("button", { name: "Show work info" })).toHaveAttribute("aria-pressed", "false");

  const storage = await page.evaluate(() => ({
    legacy: window.localStorage.getItem("auto-gallery-showcase-v1"),
    slideshow: JSON.parse(window.localStorage.getItem("auto-gallery-slideshow-v1") || "null"),
  }));
  expect(storage.legacy).toBeNull();
  expect(storage.slideshow).toMatchObject({
    slideDwellMs: 7500,
    slideTransition: "crossfade",
    slideLoop: false,
    slideShowMeta: false,
  });
  expect(storage.slideshow).not.toHaveProperty("layoutMode");
  expect(storage.slideshow).not.toHaveProperty("autoplay");

  await page.getByRole("button", { name: "Loop playback" }).click();
  await expect.poll(() => preferencePayload, { timeout: 3000 }).not.toBeNull();
  const saved = preferencePayload;
  if (!saved || typeof saved !== "object" || !("preferences" in saved)) {
    throw new Error("Expected the slideshow preferences request payload");
  }
  expect(saved.preferences).toHaveProperty("slideshow");
  expect(saved.preferences).not.toHaveProperty("showcase");
});

test("stored compact sidebar keeps primary page shells stable and aligned from first paint", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.addInitScript(() => {
    window.localStorage.setItem("auto-gallery-sidebar-wide-v2", "compact");
    const samples: Array<{ x: number; width: number }> = [];
    const debugWindow = window as typeof window & {
      __adminShellSamples?: Array<{ x: number; width: number }>;
      __adminLayoutShift?: number;
      __adminLayoutShiftSources?: Array<{
        value: number;
        targets: string[];
        rects: Array<{ previousX: number; previousWidth: number; currentX: number; currentWidth: number }>;
      }>;
    };
    debugWindow.__adminShellSamples = samples;
    debugWindow.__adminLayoutShift = 0;
    debugWindow.__adminLayoutShiftSources = [];
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        const shift = entry as PerformanceEntry & {
          hadRecentInput?: boolean;
          value?: number;
          sources?: Array<{
            node?: Node | null;
            previousRect?: DOMRectReadOnly;
            currentRect?: DOMRectReadOnly;
          }>;
        };
        if (!shift.hadRecentInput) {
          debugWindow.__adminLayoutShift! += shift.value || 0;
          debugWindow.__adminLayoutShiftSources!.push({
            value: shift.value || 0,
            targets: (shift.sources || []).map((source) => {
              const element = source.node instanceof Element ? source.node : null;
              if (!element) return "unknown";
              return `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ""}${element.className ? `.${String(element.className).split(" ").join(".")}` : ""}`;
            }),
            rects: (shift.sources || []).map((source) => ({
              previousX: source.previousRect?.x || 0,
              previousWidth: source.previousRect?.width || 0,
              currentX: source.currentRect?.x || 0,
              currentWidth: source.currentRect?.width || 0,
            })),
          });
        }
      }
    }).observe({ type: "layout-shift", buffered: true });
    let frames = 0;
    const sample = () => {
      const shell = document.querySelector<HTMLElement>("[data-page-shell]");
      if (shell) {
        const box = shell.getBoundingClientRect();
        samples.push({ x: box.x, width: box.width });
      }
      frames += 1;
      if (frames < 180) window.requestAnimationFrame(sample);
    };
    window.requestAnimationFrame(sample);
  });

  const inspectFirstPaint = async (route: string, heading: string) => {
    await page.goto(route);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await page.waitForTimeout(750);

    const result = await page.evaluate(() => {
      const debugWindow = window as typeof window & {
        __adminShellSamples?: Array<{ x: number; width: number }>;
        __adminLayoutShift?: number;
        __adminLayoutShiftSources?: Array<{
          value: number;
          targets: string[];
          rects: Array<{ previousX: number; previousWidth: number; currentX: number; currentWidth: number }>;
        }>;
      };
      const shell = document.querySelector<HTMLElement>("[data-page-shell]")!.getBoundingClientRect();
      return {
        samples: debugWindow.__adminShellSamples || [],
        layoutShift: debugWindow.__adminLayoutShift || 0,
        layoutShiftSources: debugWindow.__adminLayoutShiftSources || [],
        shell: { x: shell.x, width: shell.width },
      };
    });

    expect(result.samples.length).toBeGreaterThan(1);
    const xValues = result.samples.map((sample) => sample.x);
    const widthValues = result.samples.map((sample) => sample.width);
    const mainColumnShifts = result.layoutShiftSources.filter((shift) => (
      shift.targets.some((target) => target.includes("div.flex.min-w-0.flex-1.flex-col"))
      && shift.rects.some((rect) => (
        Math.abs(rect.currentX - rect.previousX) > 1
        || Math.abs(rect.currentWidth - rect.previousWidth) > 1
      ))
    ));
    expect(mainColumnShifts, `${route}: total CLS ${result.layoutShift}`).toEqual([]);
    expect(Math.max(...xValues) - Math.min(...xValues), `${route} shell x`).toBeLessThanOrEqual(1);
    expect(Math.max(...widthValues) - Math.min(...widthValues), `${route} shell width`).toBeLessThanOrEqual(1);
    return result.shell;
  };

  const creatorsShell = await inspectFirstPaint("/admin/creators", "Creators");
  await page.screenshot({ path: "/tmp/auto-gallery-creators-stable-shell.png", fullPage: false });
  const subscriptionsShell = await inspectFirstPaint("/admin/subscriptions", "Subscriptions");
  await page.screenshot({ path: "/tmp/auto-gallery-subscriptions-stable-shell.png", fullPage: false });
  const systemShell = await inspectFirstPaint("/admin/system", "System & Sources");
  const serviceCards = page.locator("#system-panel-services article");
  await expect(serviceCards).toHaveCount(4);
  const serviceCardY = await serviceCards.evaluateAll((cards) => (
    cards.map((card) => card.getBoundingClientRect().y)
  ));
  expect(Math.max(...serviceCardY) - Math.min(...serviceCardY)).toBeLessThanOrEqual(1);
  await page.screenshot({ path: "/tmp/auto-gallery-system-balanced-grid.png", fullPage: false });
  const jobsShell = await inspectFirstPaint("/admin/jobs?tab=downloads", "Jobs");

  for (const [name, shell] of [
    ["creators", creatorsShell],
    ["subscriptions", subscriptionsShell],
    ["system", systemShell],
  ] as const) {
    expect(Math.abs(shell.x - jobsShell.x), `${name} shell x should match Jobs`).toBeLessThanOrEqual(1);
    expect(Math.abs(shell.width - jobsShell.width), `${name} shell width should match Jobs`).toBeLessThanOrEqual(1);
  }
});

test("desktop sidebar is the sole peer-page navigation and command palette remains usable", async ({ page }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=downloads/);
  expect(await page.title()).not.toBe("");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("body")).not.toHaveText("");
  await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
  await expect(page.locator("aside").first()).toHaveCSS("width", "248px");
  const sidebar = page.locator("#admin-sidebar");
  await expect(sidebar.locator("nav a")).toHaveCount(12);
  await expect(sidebar.getByRole("link", { name: "Remote Discovery", exact: true })).toHaveAttribute(
    "href",
    "/admin/discovery",
  );
  await expect(sidebar.locator("nav").getByRole("link", { name: "Dashboard", exact: true })).toHaveCount(0);
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAttribute("href", "/admin");
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAccessibleName("Go to dashboard");
  await expect(sidebar.locator("nav h2")).toHaveCount(0);
  await expect(sidebar.locator('section[aria-label="Upload & Import"]')).toBeVisible();
  await expect(sidebar.locator("section[data-sidebar-group] + section[data-sidebar-group]").first()).toHaveCSS(
    "border-top-style",
    "solid",
  );
  await expect(sidebar.getByRole("link", { name: "Upload" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Danbooru" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Notifications" })).toHaveCount(0);
  await expect(page.locator('[data-page-header] nav[aria-label="Related pages"]')).toHaveCount(0);
  await expect(sidebar.locator('a[href="/admin/jobs"]')).toHaveAttribute("aria-current", "page");
  await expect(sidebar.locator('a[href="/admin/scheduler"]')).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toHaveCount(0);
  await expect(page.getByText("Live", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("source-code-link")).toHaveAttribute(
    "href",
    "https://github.com/MUSH2077/auto-gallery",
  );
  await expectNoPageOverflow(page);

  await page.keyboard.press("Control+k");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const commandSearch = dialog.getByRole("combobox", { name: "Search works..." });
  await commandSearch.fill("creator");
  await expect(dialog.getByRole("option").first()).toBeVisible();
  await commandSearch.fill("merge candidate");
  await expect(dialog.getByRole("option", { name: /^Asset Deduplication\b/ })).toBeVisible();
  await expect(dialog.getByRole("option", { name: /^Merge Candidates\b/ })).toHaveCount(0);
  await commandSearch.fill("source provider");
  await expect(dialog.getByRole("option", { name: /^System & Sources\b/ })).toBeVisible();
  await commandSearch.fill("notifications");
  await expect(dialog.getByRole("option", { name: /^Notifications\b/ })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-command-palette.png", fullPage: false });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  const notificationBell = page.locator('header button[aria-label="Notifications"]');
  await notificationBell.click();
  await page.getByRole("button", { name: /Notifications\s*→/ }).click();
  await expect(page).toHaveURL(/\/admin\/notifications$/);
  await expect(page.locator("#main-content").getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible();
  await expect(page.locator("#main-content header nav")).toHaveCount(0);
  expect(pageErrors).toEqual([]);
  expect(consoleErrors.filter((message) => !message.includes("WebSocket"))).toEqual([]);
});

test("sidebar resolves exactly one active route without duplicate peer-page tabs", async ({ page }) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1440, height: 960 });

  for (const [route, sidebarHref] of NAVIGATION_SELECTION_MATRIX) {
    await page.goto(route);
    await expect(page.locator("#main-content").getByRole("heading", { level: 1 })).toBeVisible();
    await expectUniqueNavigationSelection(page, sidebarHref);
  }

  await page.goto("/admin/upload");
  await expectUniqueNavigationSelection(page, "/admin/upload");
  await page.locator("#admin-sidebar").getByRole("link", { name: "Danbooru", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/upload\/danbooru$/);
  await expectUniqueNavigationSelection(page, "/admin/upload/danbooru");
  await page.screenshot({ path: "/tmp/auto-gallery-danbooru-single-active-desktop.png", fullPage: false });

  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/upload$/);
  await expectUniqueNavigationSelection(page, "/admin/upload");
  await page.goForward();
  await expect(page).toHaveURL(/\/admin\/upload\/danbooru$/);
  await expectUniqueNavigationSelection(page, "/admin/upload/danbooru");

  await page.goto("/admin/reference/danbooru?artist=atlas");
  await expect(page).toHaveURL(/\/admin\/upload\/danbooru\?artist=atlas$/);
  await expectUniqueNavigationSelection(page, "/admin/upload/danbooru");

  await page.goto("/admin/dedup?status=deferred");
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=deferred$/);
  await expectUniqueNavigationSelection(page, "/admin/data-mgmt");

  const managementNav = page.getByRole("navigation", { name: "Data management sections" });
  await managementNav.getByRole("link", { name: "Curation" }).click();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/curation$/);
  await expectUniqueNavigationSelection(page, "/admin/data-mgmt");
  await page.getByRole("navigation", { name: "Data management sections" }).getByRole("link", { name: "Data Mgmt" }).click();
  await expect(page).toHaveURL(/\/admin\/data-mgmt$/);
});

test("top-level page headers share the task page alignment and works has no creator picker", async ({ page }) => {
  test.setTimeout(60_000);
  await page.setViewportSize({ width: 1440, height: 960 });

  const shellBox = async (path: string) => {
    await page.goto(path);
    const heading = page.locator("#main-content").getByRole("heading", { level: 1 });
    await expect(heading).toBeVisible();
    const shell = await page.locator("[data-page-shell]").first().boundingBox();
    const header = await page.locator("[data-page-header]").first().boundingBox();
    const primary = await page.locator("[data-page-primary-content]:visible").first().boundingBox();
    const headingBox = await heading.boundingBox();
    expect(shell).not.toBeNull();
    expect(header).not.toBeNull();
    expect(primary).not.toBeNull();
    expect(headingBox).not.toBeNull();
    expect(
      Math.abs(primary!.y - (header!.y + header!.height)),
      `${path} primary content should follow the standard 24px header margin`,
    ).toBeGreaterThanOrEqual(23);
    expect(Math.abs(primary!.y - (header!.y + header!.height))).toBeLessThanOrEqual(25);
    return { shell: shell!, heading: headingBox! };
  };

  const taskPage = await shellBox("/admin/jobs?tab=downloads");
  for (const path of PRIMARY_ADMIN_ROUTES.filter((route) => !route.startsWith("/admin/jobs"))) {
    const current = await shellBox(path);
    expect(Math.abs(current.shell.x - taskPage.shell.x), `${path} shell should align with Jobs`).toBeLessThanOrEqual(1);
    expect(Math.abs(current.shell.width - taskPage.shell.width), `${path} shell should match Jobs width`).toBeLessThanOrEqual(1);
    expect(Math.abs(current.heading.y - taskPage.heading.y), `${path} heading should share the Jobs baseline`).toBeLessThanOrEqual(1);
  }

  await page.goto("/admin/works?creator=creator-atlas");
  await expect(page.getByRole("combobox", { name: "Filter creator" })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-page-alignment.png", fullPage: false });
});

for (const viewport of [
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
] as const) {
  test(`primary admin routes keep their hierarchy and reflow at ${viewport.name} width`, async ({ page }) => {
    test.setTimeout(60_000);
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const route of PRIMARY_ADMIN_ROUTES) {
      await page.goto(route);
      const main = page.locator("#main-content");
      await expect(main.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("[data-page-shell]").first()).toBeVisible();
      await expect(page.locator("[data-page-header]").first()).toBeVisible();
      await expect(page.locator("[data-page-primary-content]:visible").first()).toBeVisible();
      await expect.poll(() => page.evaluate(() => {
        const heading = document.querySelector("#main-content h1")?.getBoundingClientRect();
        const topbar = document.querySelector("header.sticky")?.getBoundingClientRect();
        return Boolean(heading && topbar && heading.top >= topbar.bottom);
      })).toBe(true);
      await expectNoPageOverflow(page);
    }
  });
}

test("creator activity calendar aligns real month spans and its year listbox supports keyboard selection", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/creators/fixture-creator");

  await expect(page.getByTestId("creator-activity-chart")).toBeVisible();
  await expect(page.getByTestId("creator-source-chart")).toBeVisible();
  await expect(page.getByTestId("creator-tag-chart")).toBeVisible();
  await expect(page.getByTestId("creator-monthly-chart")).toBeVisible();
  await expect(page.locator('[data-chart-kind="tick-rows"]')).toHaveAttribute("data-chart-unit", "5");
  await expect(page.locator('[data-chart-kind="ballot-tally"] a')).toHaveCount(6);
  await page.screenshot({ path: "/tmp/auto-gallery-creator-charts-desktop.png", fullPage: true });

  const activityGrid = page.locator('[data-chart-kind="activity-dot-matrix"] [role="grid"]');
  await expect(activityGrid).toHaveAttribute("data-calendar-grid", "shared");
  await expect(activityGrid.getByRole("row")).toHaveCount(7);
  await expect(activityGrid.getByRole("row").first().getByRole("gridcell")).not.toHaveCount(0);
  const activityGridAxe = await new AxeBuilder({ page })
    .include('[data-chart-kind="activity-dot-matrix"]')
    .analyze();
  expect(activityGridAxe.violations.filter((violation) => (
    violation.id === "aria-required-children" || violation.id === "aria-required-parent"
  ))).toEqual([]);
  await expect(activityGrid.locator('[data-calendar-month="0"]')).toHaveCSS("grid-column", "2 / span 4");
  await expect(activityGrid.locator('[data-calendar-month="11"]')).toHaveCSS("grid-column", "50 / span 5");
  await expect(activityGrid.locator("[data-calendar-weekday]")).toHaveCount(7);
  await expect(activityGrid.locator('[data-calendar-weekday="1"]')).not.toBeEmpty();
  await expect(activityGrid.locator("#activity-day-2026-01-01")).toHaveCSS("grid-column", "2");
  await expect(activityGrid.locator("#activity-day-2026-01-01")).toHaveCSS("grid-row", "5");
  await activityGrid.focus();
  const firstActiveDay = await activityGrid.getAttribute("aria-activedescendant");
  await page.keyboard.press("ArrowRight");
  const nextActiveDay = await activityGrid.getAttribute("aria-activedescendant");
  expect(nextActiveDay).not.toBe(firstActiveDay);
  await page.keyboard.press("Enter");
  await expect(page.locator('[data-chart-kind="activity-dot-matrix"] [aria-live="polite"]')).toBeVisible();
  await page.keyboard.press("Escape");

  const multiSourceDay = page.locator("#activity-day-2026-04-12");
  await expect(multiSourceDay.locator("[data-activity-source]")).toHaveCount(2);
  const pixivCircle = multiSourceDay.locator('[data-activity-source="pixiv"]');
  const xCircle = multiSourceDay.locator('[data-activity-source="x"]');
  expect(Number(await pixivCircle.getAttribute("r"))).toBeGreaterThan(Number(await xCircle.getAttribute("r")));
  expect(await pixivCircle.getAttribute("fill")).not.toBe(await xCircle.getAttribute("fill"));

  const request2025 = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/creators/fixture-creator/timeline"
      && url.searchParams.get("from_date") === "2025-01-01";
  });
  const yearPicker = page.getByRole("button", { name: "Year", exact: true });
  await yearPicker.click();
  const yearListbox = page.getByRole("listbox", { name: "Year", exact: true });
  await expect(yearListbox).toBeFocused();
  await expect(yearListbox).toHaveAttribute("aria-activedescendant", "activity-year-option-2026");
  await expect(page.getByRole("option", { name: "2026", exact: true })).toHaveAttribute("aria-selected", "true");
  await yearListbox.press("ArrowUp");
  await expect(yearListbox).toHaveAttribute("aria-activedescendant", "activity-year-option-2025");
  await yearListbox.press("Escape");
  await expect(yearPicker).toBeFocused();
  await yearPicker.click();
  await expect(yearListbox).toHaveAttribute("aria-activedescendant", "activity-year-option-2026");
  await yearListbox.press("ArrowUp");
  await page.getByRole("heading", { name: "Fixture Creator" }).click();
  await expect(yearListbox).toBeHidden();
  await yearPicker.click();
  await expect(yearListbox).toHaveAttribute("aria-activedescendant", "activity-year-option-2026");
  await yearListbox.press("Home");
  await yearListbox.press("End");
  await yearListbox.press("ArrowUp");
  await yearListbox.press(" ");
  await expect(yearListbox).toBeHidden();
  await expect(yearPicker).toBeFocused();
  const request = await request2025;
  const requestUrl = new URL(request.url());
  expect(requestUrl.searchParams.get("to_date")).toBe("2026-01-01");
  await expect(page.getByTestId("creator-activity-chart")).toContainText("Activity peaked on");
  await expect(activityGrid.getByRole("gridcell")).toHaveCount(365);

  await yearPicker.click();
  await yearListbox.press("Tab");
  await expect(yearListbox).toBeHidden();
  await expect(page.getByRole("button", { name: "Next year", exact: true })).toBeFocused();
  await yearPicker.click();
  await yearListbox.press("Shift+Tab");
  await expect(yearListbox).toBeHidden();
  await expect(yearPicker).toBeFocused();

  const request2024 = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return url.pathname === "/api/v1/creators/fixture-creator/timeline"
      && url.searchParams.get("from_date") === "2024-01-01";
  });
  await page.getByRole("button", { name: "Previous year", exact: true }).click();
  await request2024;
  await expect(activityGrid.locator("#activity-day-2024-02-29")).toBeVisible();
  await expect(activityGrid.getByRole("gridcell")).toHaveCount(366);

  await expect(page.locator("[data-chart-frame] details")).toHaveCount(0);
  await expect(page.locator("[data-chart-frame] table")).toHaveCount(0);
  await expect(page.getByText("View data", { exact: true })).toHaveCount(0);
  for (const viewport of [
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
    const overflowingCharts = await page.locator("[data-chart-frame], [data-chart-kind]").evaluateAll((nodes) => (
      nodes
        .filter((node) => node.scrollWidth > node.clientWidth + 1)
        .map((node) => ({
          kind: node.getAttribute("data-chart-kind") || node.getAttribute("data-testid"),
          clientWidth: node.clientWidth,
          scrollWidth: node.scrollWidth,
        }))
    ));
    expect(overflowingCharts).toEqual([]);
  }
  const calendarScrollMetrics = await page.locator('[data-calendar-scroll]').evaluate((node) => ({
    scrollWidth: node.scrollWidth,
    clientWidth: node.clientWidth,
  }));
  expect(calendarScrollMetrics.scrollWidth).toBeGreaterThan(calendarScrollMetrics.clientWidth);
  await page.screenshot({ path: "/tmp/auto-gallery-creator-charts-mobile.png", fullPage: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await expect(page.getByTestId("creator-activity-chart")).toBeVisible();
  await expect(page.locator(".chart-dot-enter")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("creator references keep Pixiv identities and Danbooru aliases in separate read-only groups", async ({ page }) => {
  const mappingWrites: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && (path.includes("source-creators") || path.endsWith("/links"))) {
      mappingWrites.push(`${request.method()} ${path}`);
    }
  });
  await page.route("**/api/v1/creators/fixture-creator/references", (route) => route.fulfill({
    json: {
      pixiv: [
        {
          source_creator_id: "100",
          display_name: "Current Pixiv Name",
          username: "pixiv_account",
          profile_url: "https://www.pixiv.net/users/100",
          avatar_url: null,
          status: "remote",
          error_code: null,
        },
        {
          source_creator_id: "200",
          display_name: "Stored Pixiv Name",
          username: null,
          profile_url: "https://www.pixiv.net/users/200",
          avatar_url: null,
          status: "fallback",
          error_code: "remote_unavailable",
        },
      ],
      danbooru: {
        artist_id: 300,
        name: "danbooru_primary",
        other_names: ["danbooru_alias", "second_alias"],
        profile_url: "https://danbooru.donmai.us/artists/300",
        status: "remote",
      },
    },
  }));

  await page.goto("/admin/creators/fixture-creator");
  const references = page.getByRole("region", { name: "Name references" });
  await expect(references.getByRole("heading", { name: "Pixiv" })).toBeVisible();
  await expect(references.getByRole("heading", { name: "Danbooru" })).toBeVisible();
  await expect(references.getByText("Current Pixiv Name")).toBeVisible();
  await expect(references.getByText("Stored Pixiv Name")).toBeVisible();
  await expect(references.getByText("Showing stored profile data or the user ID because the remote profile is unavailable.")).toBeVisible();
  await expect(references.getByText("danbooru_primary")).toBeVisible();

  await references.getByRole("button", { name: "@pixiv_account" }).click();
  let edit = page.getByRole("dialog", { name: "Edit Creator" });
  await expect(edit.getByRole("textbox").nth(1)).toHaveValue("pixiv_account");
  await edit.getByRole("button", { name: "Cancel" }).click();

  await references.getByRole("button", { name: "danbooru_alias" }).click();
  edit = page.getByRole("dialog", { name: "Edit Creator" });
  await expect(edit.getByRole("textbox").nth(1)).toHaveValue("danbooru_alias");
  await edit.getByRole("button", { name: "Cancel" }).click();
  expect(mappingWrites).toEqual([]);
});

test("creator activity distinguishes a failed request from a genuinely empty year", async ({ page }) => {
  await page.route("**/api/v1/creators/fixture-creator/timeline?*", (route) => route.fulfill({
    status: 500,
    json: { detail: "fixture timeline failure" },
  }));
  await page.goto("/admin/creators/fixture-creator");

  const activity = page.getByTestId("creator-activity-chart");
  await expect(activity.getByRole("alert")).toContainText("Publishing activity could not be loaded");
  await expect(activity).not.toContainText("No publishing activity was recorded");
  await expect(activity.getByRole("button", { name: "Retry" })).toBeVisible();
});

test("creator activity hides stale data while a selected year request loads or fails", async ({ page }) => {
  await page.goto("/admin/creators/fixture-creator");

  const activity = page.getByTestId("creator-activity-chart");
  await expect(activity.locator("#activity-day-2026-04-12")).toBeVisible();

  let releaseSelectedYear: (() => void) | undefined;
  let selectedYearRequestCount = 0;
  await page.route("**/api/v1/creators/fixture-creator/timeline?*", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("from_date") !== "2025-01-01") {
      await route.fallback();
      return;
    }
    selectedYearRequestCount += 1;
    if (selectedYearRequestCount === 1) {
      await new Promise<void>((resolve) => {
        releaseSelectedYear = resolve;
      });
    }
    await route.fulfill({ status: 500, json: { detail: "selected-year fixture failure" } });
  });

  await page.getByRole("button", { name: "Year", exact: true }).click();
  const listbox = page.getByRole("listbox", { name: "Year", exact: true });
  await listbox.press("ArrowUp");
  await listbox.press("Enter");

  await expect(activity.locator("#activity-day-2026-04-12")).toHaveCount(0);
  await expect(activity).not.toContainText("Activity peaked on");
  if (!releaseSelectedYear) throw new Error("Expected the selected-year request to be intercepted");
  releaseSelectedYear();
  await expect(activity.getByRole("alert")).toContainText("Publishing activity could not be loaded");
  await expect(activity.locator("#activity-day-2026-04-12")).toHaveCount(0);
  const picker = page.getByRole("button", { name: "Year", exact: true });
  await expect(picker).toBeVisible();
  await picker.focus();
  await expect(picker).toBeFocused();
  await picker.click();
  const recoveryListbox = page.getByRole("listbox", { name: "Year", exact: true });
  await recoveryListbox.press("ArrowDown");
  await recoveryListbox.press("Enter");
  await expect(picker).toBeFocused();
  await expect(activity.locator("#activity-day-2026-04-12")).toBeVisible();
  await expect(activity).toContainText("Activity peaked on");
});

test("creator activity accepts an empty response only for its requested year", async ({ page }) => {
  await page.goto("/admin/creators/fixture-creator");

  const activity = page.getByTestId("creator-activity-chart");
  const picker = page.getByRole("button", { name: "Year", exact: true });
  await picker.click();
  const listbox = page.getByRole("listbox", { name: "Year", exact: true });
  await listbox.press("Home");
  await listbox.press("Enter");
  await expect(picker).toHaveText(/2023/);
  await expect(activity).toContainText("pixiv");

  let releaseSelectedYear: (() => void) | undefined;
  await page.route("**/api/v1/creators/fixture-creator/timeline?*", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("from_date") !== "2024-01-01") {
      await route.fallback();
      return;
    }
    await new Promise<void>((resolve) => {
      releaseSelectedYear = resolve;
    });
    await route.fulfill({
      json: { creator_id: "fixture-creator", sources: ["x"], days: [], total: 0 },
    });
  });

  await picker.click();
  await listbox.press("ArrowDown");
  await listbox.press("Enter");
  await expect(picker).toBeFocused();
  await expect(activity.getByRole("status")).toContainText("Loading...");
  await expect(activity).not.toContainText("pixiv");
  if (!releaseSelectedYear) throw new Error("Expected the empty selected-year request to be intercepted");
  releaseSelectedYear();
  await expect(activity.getByRole("status")).toHaveCount(0);
  await expect(activity).toContainText("x");
  await expect(activity).not.toContainText("Activity peaked on");
});

test("data management charts preserve 100 ticks, exact values, hierarchy, and diagnostics", async ({ page }) => {
  await page.route("**/api/v1/admin/system-info", (route) => route.fulfill({
    json: {
      version: "fixture",
      downloads_size_mb: 1000,
      library_size_mb: 75,
      downloads_free_gb: 500,
      archives_kb: {},
    },
  }));
  await page.route("**/api/v1/admin/storage-breakdown", (route) => route.fulfill({
    json: {
      sources: {
        pixiv: { size_mb: 500, creator_count: 2, work_count: 80 },
        x: { size_mb: 250, creator_count: 2, work_count: 42 },
        fanbox: { size_mb: 100, creator_count: 1, work_count: 18 },
        weibo: { size_mb: 50, creator_count: 1, work_count: 9 },
        iwara: { size_mb: 40, creator_count: 1, work_count: 7 },
        tumblr: { size_mb: 30, creator_count: 1, work_count: 6 },
        local: { size_mb: 30, creator_count: 1, work_count: 4 },
      },
      creator_tree: [
        {
          creator_id: "fixture-creator",
          display_name: "Fixture Creator",
          size_mb: 600,
          work_count: 92,
          repository_count: 2,
          repositories: [
            {
              repository_id: "fixture-repository",
              source: "pixiv",
              source_display_name: "Pixiv",
              disk_source: "pixiv",
              directory_name: "fixture-pixiv-repository",
              size_mb: 400,
              work_count: 62,
            },
            {
              repository_id: "fixture-x-repository",
              source: "x",
              source_display_name: "X",
              disk_source: "twitter",
              directory_name: "fixture-x-repository",
              size_mb: 200,
              work_count: 30,
            },
          ],
        },
        {
          creator_id: "fixture-creator-2",
          display_name: "Second Fixture",
          size_mb: 300,
          work_count: 48,
          repository_count: 1,
          repositories: [
            {
              repository_id: "fixture-repository-2",
              source: "fanbox",
              source_display_name: "Fanbox",
              disk_source: "fanbox",
              directory_name: "fixture-fanbox-repository",
              size_mb: 300,
              work_count: 48,
            },
          ],
        },
      ],
      unlinked_repositories: [
        {
          repository_id: null,
          source: "local",
          source_display_name: "Local",
          disk_source: "local",
          directory_name: "unlinked-fixture",
          size_mb: 100,
          work_count: 3,
        },
      ],
      db_stats: { works: 140, assets: 221, tags: 57 },
      creators: [],
    },
  }));

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/data-mgmt");
  await expect(page.getByTestId("storage-source-chart")).toBeVisible();
  await expect(page.getByTestId("creator-storage-chart")).toBeVisible();
  await expect(page.locator('[data-chart-kind="tick-donut"] svg line')).toHaveCount(100);
  await expect(page.getByTestId("storage-source-chart")).toContainText("Other");
  await expect(page.getByRole("heading", { name: "Unlinked repositories" })).toBeVisible();

  await page.getByRole("button", { name: "Expand Fixture Creator" }).click();
  await expect(page.getByRole("button", { name: "Collapse Fixture Creator" })).toBeVisible();
  await expect(page.getByRole("link", { name: /fixture-pixiv-repository/ })).toHaveAttribute(
    "href",
    "/admin/subscriptions/repositories/fixture-repository",
  );

  await expect(page.getByTestId("storage-source-chart")).toContainText("500.0 MB");
  await expect(page.locator("[data-chart-frame] details")).toHaveCount(0);
  await expect(page.locator("[data-chart-frame] table")).toHaveCount(0);
  await expect(page.getByText("View data", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-data-charts-desktop.png", fullPage: true });
  const axe = await new AxeBuilder({ page })
    .include("#main-content")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  for (const viewport of [
    { width: 768, height: 1024 },
    { width: 390, height: 844 },
    { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await expectNoPageOverflow(page);
  }
  await page.screenshot({ path: "/tmp/auto-gallery-data-charts-mobile.png", fullPage: true });
  await expectNoPageOverflow(page);
});

test("data center overview shows a retryable error instead of permanent placeholders", async ({ page }) => {
  await page.route("**/api/v1/admin/system-info", (route) => route.fulfill({
    status: 500,
    json: { detail: "ledger unavailable" },
  }));
  await page.route("**/api/v1/admin/storage-breakdown", (route) => route.fulfill({
    json: {
      sources: {},
      creators: [],
      creator_tree: [],
      unlinked_repositories: [],
      db_stats: { works: 0, assets: 0, creators: 0, subscriptions: 0, tags: 0 },
      inventory_source: "storage_artifacts",
      inventory_updated_at: null,
      pipeline_stats: {
        pending_import_works: 0,
        orphan_pending_artifacts: 0,
        failed_artifacts: 0,
      },
    },
  }));

  await page.goto("/admin/data-mgmt");

  const overview = page.locator('[data-page-primary-content]');
  await expect(overview.getByRole("alert")).toContainText("Data Center overview could not be loaded");
  await expect(overview.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(overview).not.toContainText("Original Media -");
});

for (const route of QUALITY_ROUTES) {
  test(`route quality: ${route}`, async ({ page }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.stack || error.message));
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(route);
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
    if (route === "/admin/sources") {
      await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
      await expect(page.locator("#main-content .page-item").last()).toHaveCSS("opacity", "1");
    }
    expect(pageErrors, `${route} should not throw a framework error`).toEqual([]);
    const mainText = (await page.locator("#main-content").innerText()).replaceAll("中文", "");
    expect(mainText, `${route} should not leak Chinese copy in English mode`).not.toMatch(/[\u3400-\u9fff]/u);
    const results = await new AxeBuilder({ page })
      .include("#main-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        targets: violation.nodes.map((node) => node.target.join(" ")),
      })),
      `${route} should have no axe violations`,
    ).toEqual([]);
  });
}

for (const route of QUALITY_ROUTES) {
  test(`Chinese light route quality: ${route}`, async ({ page }) => {
    const missingTranslations: string[] = [];
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.stack || error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && message.text().includes("[i18n] Missing")) {
        missingTranslations.push(message.text());
      }
    });
    await page.addInitScript(() => {
      window.localStorage.setItem("auto-gallery-lang", "zh");
      window.localStorage.setItem("auto-gallery-theme", "light");
    });
    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(route);
    await expect(page.locator("html")).not.toHaveClass(/dark/);
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
    if (route === "/admin/sources") {
      await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
      await expect(page.locator("#main-content .page-item").last()).toHaveCSS("opacity", "1");
    }
    expect(pageErrors, `${route} should not throw a framework error`).toEqual([]);
    expect(missingTranslations, `${route} should not use raw translation keys`).toEqual([]);
    if (route === "/admin/settings/logs") {
      await page.screenshot({ path: "/tmp/auto-gallery-logs-zh-light.png", fullPage: false });
    }
    const results = await new AxeBuilder({ page })
      .include("#main-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(
      results.violations.map((violation) => ({
        id: violation.id,
        targets: violation.nodes.map((node) => node.target.join(" ")),
      })),
      `${route} should have no axe violations in Chinese light mode`,
    ).toEqual([]);
  });
}

test("system and source tabs fetch only their active data and retain provider tools", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  let healthRequests = 0;
  let sourceRequests = 0;
  await page.route("**/api/v1/system/health", async (route) => {
    healthRequests += 1;
    await route.fulfill({
      json: {
        status: "ok",
        services: { postgres: "up", redis: "up", meilisearch: "up" },
        version: "test",
        business: {},
      },
    });
  });
  await page.route("**/api/v1/sources", async (route) => {
    sourceRequests += 1;
    await route.fulfill({ json: { sources: providerFixtures } });
  });

  await page.goto("/admin/system");
  await expect(page.getByRole("heading", { level: 1, name: "System & Sources" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Service Status" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tab", { name: "Sources" })).toBeVisible();
  await expect.poll(() => healthRequests).toBeGreaterThan(0);
  expect(sourceRequests).toBe(0);
  await page.screenshot({ path: "/tmp/auto-gallery-system-services.png", fullPage: false });

  await page.getByRole("tab", { name: "Sources" }).click();
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
  await expect.poll(() => sourceRequests).toBeGreaterThan(0);

  const pixivCard = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Pixiv" }) });
  await pixivCard.getByRole("button", { name: /Try default URL/ }).click();
  await pixivCard.getByRole("textbox", { name: "Test URL Validation" }).press("Enter");
  await expect(pixivCard.getByRole("status")).toContainText("matches expected Pixiv pattern");

  const healthRequestsBeforeRefresh = healthRequests;
  const sourceRequestsBeforeRefresh = sourceRequests;
  const refreshButton = page.getByRole("button", { name: "Refresh" });
  await refreshButton.click();
  await expect.poll(() => sourceRequests).toBeGreaterThan(sourceRequestsBeforeRefresh);
  await expect(refreshButton).toBeEnabled();
  expect(healthRequests).toBe(healthRequestsBeforeRefresh);

  const axe = await new AxeBuilder({ page })
    .include("#main-content")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-system-sources.png", fullPage: true });
});

test("resource controller renders constrained compatibility state and authoritative concurrency", async ({ page }) => {
  await page.route("**/api/v1/system/health", (route) => route.fulfill({
    json: {
      status: "degraded",
      services: { postgres: "up", redis: "up", meilisearch: "up" },
      version: "acceptance",
      business: { outboxes: { search: { waiting: 12, processing: 1, failed: 0 } } },
      resource_pressure: {
        status: "warning",
        controller_mode: "constrained",
        hard_reasons: [],
        soft_reasons: ["io_psi_high"],
        sampled_at: "2026-08-11T00:00:00Z",
        memory: { available_bytes: 2147483648, available_ratio: 0.25 },
        swap: { free_bytes: 3221225472, free_ratio: 0.5 },
        psi: { memory_full_avg10: 0.5, io_full_avg10: 18 },
        redis: { usage_ratio: 0.5, writable: true },
        download_concurrency: {
          configured: 3,
          cap: 1,
          effective: 1,
          desired_effective: 1,
          restart_required: true,
        },
        budget: {
          governance_mode: "shadow",
          effective_throughput_scale: 1,
          computed_throughput_scale: 0.5,
          profiles: { import_db: { allowed: true } },
          reservation: { active_count: 0, reserved_bytes: 0 },
        },
      },
    },
  }));
  await page.goto("/admin/system");
  await expect(page.getByRole("region", { name: "Resource protection" })).toBeVisible();
  await expect(page.getByText("Constrained", { exact: true })).toBeVisible();
  await expect(page.getByText("Shadow mode", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Resource protection" }).getByText("50%", { exact: true }).last()).toBeVisible();
  await expect(page.getByText("1 / 1", { exact: true })).toBeVisible();
  await page.goto("/admin/settings/download-defaults");
  await expect(page.getByText("Saved setting 3, current worker effective concurrency 1, NAS cap 1.", { exact: true })).toBeVisible();
  await expect(page.getByText("Desired effective concurrency is 1; restart the download worker to apply it.", { exact: true })).toBeVisible();
});

test("critical compatibility state and dependency degradation remain explicit", async ({ page }) => {
  await page.route("**/api/v1/system/health", (route) => route.fulfill({
    json: {
      status: "degraded",
      services: { postgres: "up", redis: "down", meilisearch: "degraded" },
      version: "acceptance",
      business: {},
      resource_pressure: {
        status: "paused",
        controller_mode: "critical",
        hard_reasons: ["memory_available_critical", "redis_unavailable"],
        soft_reasons: [],
        sampled_at: "2026-08-11T00:00:00Z",
        memory: { available_bytes: 1073741824, available_ratio: 0.125 },
        swap: { free_bytes: 1073741824, free_ratio: 0.1 },
        psi: { memory_full_avg10: 8, io_full_avg10: 30 },
        redis: { usage_ratio: 0.95, writable: false },
        download_concurrency: { configured: 1, cap: 1, effective: 0, restart_required: false },
        controller: { governance_mode: "shadow", enforced_profiles: [] },
        budget: { governance_mode: "shadow", effective_throughput_scale: 0, computed_throughput_scale: 0, profiles: {} },
      },
    },
  }));
  await page.goto("/admin/system");
  await expect(page.getByRole("region", { name: "Resource protection" })).toBeVisible();
  await expect(page.getByText("Hard protection", { exact: true })).toBeVisible();
  await expect(page.getByText(/Available memory is below the pause threshold/)).toBeVisible();
});

test("task rows expose running waiting and yielded resource states", async ({ page }) => {
  await page.route("**/api/v1/tasks**", (route) => route.fulfill({
    json: {
      total: 3,
      offset: 0,
      limit: 50,
      items: [
        { id: "task-running", kind: "admin", operation_type: "media", status: "running", resource_state: "running", title: "Running fixture", created_at: "2026-08-11T00:00:00Z" },
        { id: "task-waiting", kind: "admin", operation_type: "search", status: "running", resource_state: "waiting", resource_reason: "profile_memory_reserve", title: "Waiting fixture", created_at: "2026-08-11T00:00:00Z" },
        { id: "task-yielded", kind: "admin", operation_type: "import", status: "running", resource_state: "yielded", resource_reason: "slice_complete", title: "Yielded fixture", created_at: "2026-08-11T00:00:00Z" },
      ],
    },
  }));
  await page.goto("/admin/jobs?tab=admin");
  await expect(page.getByText("Resource granted", { exact: true })).toBeVisible();
  await expect(page.getByText(/Waiting for resources/)).toBeVisible();
  await expect(page.getByText(/Resources yielded/)).toBeVisible();
  await expect(page.getByText(/Not enough memory headroom/)).toBeVisible();
});

test("a 30-work page submits curation in bounded 25-work chunks", async ({ page }) => {
  const chunks: string[][] = [];
  const works = Array.from({ length: 30 }, (_, index) => ({
    id: `fixture-work-${String(index).padStart(2, "0")}`,
    title: `Fixture work ${index}`,
    description: null,
    posted_at: "2026-08-11T00:00:00Z",
    is_nsfw: false,
    is_ai_generated: false,
    asset_count: 0,
    is_favorite: false,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
  }));
  await page.route("**/api/v1/search**", (route) => route.fulfill({ json: {
    query: "",
    canonical_query: "",
    parsed: { raw: "", canonical: "", scope: "works", targets: ["works"], tokens: [] },
    groups: { works: { items: works, total: 30 } },
    total: 30,
    results: [],
    creators: [],
    tags: [],
    repositories: [],
    subscriptions: [],
  } }));
  await page.route("**/api/v1/works/batch-curate", async (route) => {
    chunks.push((route.request().postDataJSON() as { ids: string[] }).ids);
    await route.fulfill({ json: { id: `commit-${chunks.length}`, changes: [] } });
  });
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/admin/works");
  await page.getByRole("button", { name: "Select page" }).click();
  await page.getByRole("button", { name: "Move to trash" }).click();
  await expect.poll(() => chunks.length).toBe(2);
  expect(chunks.map((chunk) => chunk.length)).toEqual([25, 5]);
});

test("pending derivatives render the original and retain a recovery label", async ({ page }) => {
  let originalRequests = 0;
  await page.route("**/api/v1/works/fixture-work/assets", (route) => route.fulfill({
    json: [{
      id: "pending-asset",
      file_name: "pending.jpg",
      file_path: "acceptance/pending.jpg",
      mime_type: "image/jpeg",
      media_kind: "image",
      derivative_status: "pending",
      original_url: "/media/original/pending-asset",
      created_at: "2026-08-11T00:00:00Z",
    }],
  }));
  await page.route("**/media/original/pending-asset", async (route) => {
    originalRequests += 1;
    await route.fulfill({
      contentType: "image/png",
      body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
    });
  });
  await page.goto("/admin/works/fixture-work");
  await expect(page.getByText("Preview is being generated in the background").first()).toBeVisible();
  await expect.poll(() => originalRequests).toBeGreaterThan(0);
});

test("works page shows aggregate preview progress and refreshes it", async ({ page }) => {
  let requests = 0;
  let resumed = false;
  await page.route("**/api/v1/works/derivative-progress", async (route) => {
    requests += 1;
    await route.fulfill({ json: !resumed ? {
      total: 5150,
      completed: 4362,
      pending: 788,
      processing: 0,
      failed: 0,
      remaining: 788,
      affected_works: 504,
      completion_percent: 84.7,
      status: "stalled",
      last_completed_at: "2026-08-29T03:44:01+08:00",
      oldest_unfinished_at: "2026-08-27T20:08:23+08:00",
      stall_after_seconds: 300,
    } : {
      total: 5150,
      completed: 4363,
      pending: 786,
      processing: 1,
      failed: 0,
      remaining: 787,
      affected_works: 503,
      completion_percent: 84.7,
      status: "running",
      last_completed_at: "2026-08-30T12:20:00+08:00",
      oldest_unfinished_at: "2026-08-27T20:08:23+08:00",
      stall_after_seconds: 300,
    } });
  });

  await page.goto("/admin/works");
  const progress = page.getByRole("region", { name: "Preview generation progress" });
  await expect(progress).toBeVisible();
  await expect(progress.getByText("Generation appears stalled")).toBeVisible();
  await expect(progress.getByText("4,362 / 5,150")).toBeVisible();
  await expect(progress.getByText("788 remaining across 504 works")).toBeVisible();

  resumed = true;
  await progress.getByRole("button", { name: "Refresh progress" }).click();
  await expect(progress.getByText("Generating previews")).toBeVisible();
  await expect(progress.getByText("4,363 / 5,150")).toBeVisible();
  await expect.poll(() => requests).toBeGreaterThanOrEqual(2);
});

test("system and source tabs preserve module-level permissions", async ({ page }) => {
  let healthRequests = 0;
  let sourceRequests = 0;
  await page.route("**/api/v1/system/health", async (route) => {
    healthRequests += 1;
    await route.fulfill({ json: { status: "ok", services: {}, version: "test", business: {} } });
  });
  await page.route("**/api/v1/sources", async (route) => {
    sourceRequests += 1;
    await route.fulfill({ json: { sources: providerFixtures } });
  });

  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["system"],
      modules: { system: true, subscriptions: false },
    },
  }));
  await page.goto("/admin/system?tab=sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=services$/);
  await expect(page.getByRole("tab", { name: "Service Status" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveCount(0);
  await expect.poll(() => healthRequests).toBeGreaterThan(0);
  expect(sourceRequests).toBe(0);

  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["subscriptions"],
      modules: { system: false, subscriptions: true },
    },
  }));
  await page.goto("/admin/sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Service Status" })).toHaveCount(0);
  await expect(page.locator("#admin-sidebar").getByRole("link", { name: "System & Sources" })).toBeVisible();
  const healthRequestsBeforeSourceOnly = healthRequests;
  await expect.poll(() => sourceRequests).toBeGreaterThan(0);
  expect(healthRequests).toBe(healthRequestsBeforeSourceOnly);
});

for (const viewport of [
  { name: "tablet", width: 768, height: 1024 },
  { name: "mobile", width: 390, height: 844 },
] as const) {
  test(`source registry reflows at ${viewport.name} width`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/admin/system?tab=sources");
    await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { level: 3, name: "Pixiv" })).toBeVisible();
    await expectNoPageOverflow(page);
    if (viewport.name === "mobile") {
      await page.screenshot({ path: "/tmp/auto-gallery-system-sources-mobile.png", fullPage: true });
    }
  });
}

test("dedup status is addressable and browser history restores the selected review queue", async ({ page }) => {
  await page.goto("/admin/data-mgmt/dedup?status=deferred");
  await expect(page.getByText("0 candidates in the current view")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Pending" }).click();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await page.goBack();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=deferred$/);
  await expect(page.getByRole("tab", { name: "Deferred" })).toHaveAttribute("aria-selected", "true");
});

test("legacy task, source, merge, and settings data routes redirect to their maintained destinations", async ({ page }) => {
  await page.goto("/admin/import-jobs");
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=imports$/);
  await expect(page.locator("[data-page-shell]")).toBeVisible();

  await page.goto("/admin/sources");
  await expect(page).toHaveURL(/\/admin\/system\?tab=sources$/);
  await expect(page.getByRole("tab", { name: "Sources" })).toHaveAttribute("aria-selected", "true");

  await page.goto("/admin/merge-candidates");
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup\?status=pending$/);
  await expect(page.getByRole("tab", { name: "Pending" })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("[data-page-shell]")).toBeVisible();

  await page.goto("/admin/settings/data-mgmt");
  await expect(page).toHaveURL(/\/admin\/data-mgmt$/);
  await expect(page.getByRole("heading", { level: 1, name: "Data Management" })).toBeVisible();
});

test("migrated admin URLs return permanent redirects and preserve deep-link state", async ({ page }) => {
  const cases = [
    ["/admin/reference/danbooru?artist=atlas&tag=a&tag=b", "/admin/upload/danbooru?artist=atlas&tag=a&tag=b"],
    ["/admin/repositories/repo-1?tab=tags&page=2", "/admin/subscriptions/repositories/repo-1?tab=tags&page=2"],
    ["/admin/curation?cursor=commit-1", "/admin/data-mgmt/curation?cursor=commit-1"],
    ["/admin/dedup?status=deferred&page=3", "/admin/data-mgmt/dedup?status=deferred&page=3"],
    ["/admin/users?status=inactive", "/admin/settings/users?status=inactive"],
    ["/admin/users/42?from=audit", "/admin/settings/users/42?from=audit"],
    ["/admin/merge-candidates?view=grid&status=deferred", "/admin/data-mgmt/dedup?view=grid&status=pending"],
    ["/admin/sources?from=bookmark", "/admin/system?from=bookmark&tab=sources"],
    ["/admin/import-jobs?from=bookmark", "/admin/jobs?from=bookmark&tab=imports"],
    ["/admin/settings/data-mgmt?from=bookmark", "/admin/data-mgmt?from=bookmark"],
    ["/admin/settings/auth-status?from=bookmark", "/admin/scheduler?from=bookmark#auth-status"],
  ] as const;

  for (const [legacy, canonical] of cases) {
    const response = await page.request.get(legacy, { maxRedirects: 0 });
    expect(response.status(), legacy).toBe(308);
    const location = new URL(response.headers().location, "http://127.0.0.1:13000");
    expect(`${location.pathname}${location.search}${location.hash}`, legacy).toBe(canonical);
  }
});

test("tasks request actionable pages while scheduler paginates its normal-plan view", async ({ page }) => {
  const requestedTaskOffsets: number[] = [];
  await page.route("**/api/v1/tasks**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/tasks") {
      await route.fallback();
      return;
    }
    expect(url.searchParams.get("visibility")).toBe("actionable");
    const limit = Number(url.searchParams.get("limit") || 0);
    if (limit !== 100) {
      await route.fulfill({ json: { items: [], total: 0, offset: 0, limit } });
      return;
    }
    const offset = Number(url.searchParams.get("offset") || 0);
    requestedTaskOffsets.push(offset);
    const task = {
      id: `task-${offset}`,
      kind: "admin",
      operation_type: "contract-check",
      status: "running",
      title: `Task page ${offset / 100 + 1}`,
      created_at: "2026-07-27T10:00:00Z",
    };
    await route.fulfill({ json: { items: [task], total: 205, offset, limit: 100 } });
  });
  const decisions = Array.from({ length: 55 }, (_, index) => ({
      subscription_id: `subscription-${index}`,
      subscription_name: "Fixture subscription",
      subscription_active: true,
      subscription_sync_enabled: true,
      creator_id: `creator-${index}`,
      creator_name: `Scheduled creator ${index + 1}`,
      source_id: `repository-${index}`,
      source: "pixiv",
      source_url: "https://www.pixiv.net/users/10000001",
      source_enabled: true,
      effective_mode: "interval",
      timezone: "UTC",
      sync_interval_hours: 6,
      due: true,
      decision: "sync",
      reason: "due",
      auth_healthy: true,
      url_valid: true,
      can_download: true,
      attention: false,
      is_overdue: false,
    }));
  await page.route("**/api/v1/system/scheduler-decisions**", async (route) => {
    const url = new URL(route.request().url());
    const view = url.searchParams.get("view") || "all";
    await route.fulfill({ json: {
      updated_at: "2026-07-27T12:00:00Z",
      scheduler_enabled: true,
      timezone: "UTC",
      view,
      total: view === "attention" ? 0 : decisions.length,
      items: view === "attention" ? [] : decisions,
    } });
  });

  await page.goto("/admin/jobs");
  await expect(page.getByText("Task page 1")).toBeVisible();
  await expect(page.getByText("Page 1 of 3")).toBeVisible();
  await page.getByRole("navigation", { name: "Pagination" }).getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/\/admin\/jobs\?page=2$/);
  await expect(page.getByText("Task page 2")).toBeVisible();
  expect(requestedTaskOffsets).toContain(0);
  expect(requestedTaskOffsets).toContain(100);

  await page.goto("/admin/scheduler");
  await page.locator("details").filter({ hasText: "Healthy schedules" }).locator("summary").click();
  await expect(page.getByText("Scheduled creator 1", { exact: true })).toBeVisible();
  await page.getByRole("navigation", { name: "Pagination" }).getByRole("button", { name: "Next" }).click();
  await expect(page).toHaveURL(/\/admin\/scheduler\?page=2$/);
  await expect(page.getByText("Scheduled creator 26", { exact: true })).toBeVisible();
});

test("settings no longer duplicates data management or language controls", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/settings");
  const main = page.locator("#main-content");
  await expect(main.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await expect(main.getByRole("link", { name: /Data Management/ })).toHaveCount(0);
  await expect(main.getByRole("heading", { name: "Language" })).toHaveCount(0);
  await expect(main.getByRole("link", { name: /Auth & Cookie Status/ })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-settings-clean.png", fullPage: false });
});

test("subscription list uses one authoritative latest state and page-scoped summary ids", async ({ page }) => {
  const subscriptionId = "11111111-1111-4111-8111-111111111111";
  let requestedIds = "";
  let runtimeScheduleRule: unknown = { frequency: "daily", times: ["22:00:00"] };
  await page.route("**/api/v1/search**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/v1/search" || url.searchParams.get("scope") !== "subscriptions") {
      await route.fallback();
      return;
    }
    const item = {
      id: subscriptionId,
      creator_id: "22222222-2222-4222-8222-222222222222",
      creator_name: "isaya_(pixiv4541633)",
      creator_display_name: "isaya_(pixiv4541633)",
      name: "isaya_(pixiv4541633)",
      is_active: true,
      sync_enabled: true,
      sync_interval_hours: 6,
      schedule_mode: null,
      scheduled_times: null,
      last_synced_at: "2026-08-13T14:07:00Z",
      source_count: 3,
      enabled_source_count: 1,
      running_job_count: 0,
      failed_job_count: 1,
      latest_job_status: "stale",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-08-13T14:07:00Z",
    };
    await route.fulfill({ json: {
      query: "",
      canonical_query: "",
      parsed: { raw: "", canonical: "", scope: "subscriptions", targets: ["subscriptions"], tokens: [] },
      groups: { subscriptions: { total: 1, items: [item] } },
      total: 1,
      results: [],
      subscriptions: [item],
    } });
  });
  await page.route("**/api/v1/subscriptions/summaries**", async (route) => {
    const url = new URL(route.request().url());
    requestedIds = url.searchParams.get("ids") || "";
    await route.fulfill({ json: {
      updated_at: "2026-08-13T14:10:00Z",
      items: [{
        subscription_id: subscriptionId,
        latest_state: {
          state: "success",
          status: "complete",
          occurred_at: "2026-08-13T14:07:00Z",
          outcome_code: "no_changes",
          repository_id: "33333333-3333-4333-8333-333333333333",
        },
        active_count: 0,
        attention_count: 0,
        source_count: 3,
        enabled_source_count: 1,
        schedule: {
          configured_mode: "inherit",
          effective_mode: "calendar",
          inherited: true,
          timezone: "Asia/Shanghai",
          scheduled_times: null,
          schedule_rule: runtimeScheduleRule,
          sync_interval_hours: 6,
          next_due_at: "2026-08-14T14:00:00Z",
          oldest_due_at: null,
          due_sources: 0,
          overdue_sources: 0,
          blocked_sources: 0,
        },
      }],
    } });
  });

  for (const viewport of [
    { name: "desktop", width: 1440, height: 960 },
    { name: "tablet", width: 768, height: 1024 },
    { name: "mobile", width: 390, height: 844 },
  ] as const) {
    await page.setViewportSize(viewport);
    await page.goto("/admin/subscriptions");
    await expect(page.getByText("Sync successful · No new works")).toBeVisible();
    await expect(page.getByText("1 failed")).toHaveCount(0);
    await expect(page.getByText("Stale", { exact: true })).toHaveCount(0);
    await expect(page.getByText("System default · Calendar · Daily at 22:00")).toBeVisible();
    await expectNoPageOverflow(page);
    await expect(page.locator("#main-content .page-item").last()).toHaveCSS("opacity", "1");
    const results = await new AxeBuilder({ page })
      .include("#main-content")
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
      .analyze();
    expect(results.violations, `subscription status should pass axe at ${viewport.width}px`).toEqual([]);
    await page.screenshot({
      path: `/tmp/auto-gallery-subscription-latest-${viewport.name}.png`,
      fullPage: true,
    });
  }
  expect(requestedIds).toBe(subscriptionId);

  runtimeScheduleRule = { frequency: "weekly", times: "22:00:00" };
  await page.goto("/admin/subscriptions");
  await expect(page.getByText("System default · Calendar · Schedule at 22:00")).toBeVisible();
  await expect(page.getByText("Application error")).toHaveCount(0);

  runtimeScheduleRule = { frequency: "daily", times: {} };
  await page.goto("/admin/subscriptions");
  await expect(page.getByText("System default · Calendar · Daily at —")).toBeVisible();
  await expect(page.getByText("System default · Calendar · Daily at 22:00")).toHaveCount(0);
});

test("saving inherit sends the typed strategy and survives authoritative reload", async ({ page }) => {
  let inherited = false;
  let updatePayload: Record<string, unknown> | null = null;
  const subscription = () => ({
    id: "fixture-subscription",
    creator_id: "fixture-creator",
    name: "Fixture Subscription",
    creator_name: "fixture-creator",
    creator_display_name: "Fixture Creator",
    is_active: true,
    sync_enabled: inherited,
    sync_interval_hours: 6,
    schedule_mode: inherited ? null : "manual",
    scheduled_times: null,
    source_count: 1,
    enabled_source_count: inherited ? 1 : 0,
    running_job_count: 0,
    failed_job_count: 0,
    configured_mode: inherited ? "inherit" : "manual",
    effective_mode: inherited ? "calendar" : "manual",
    schedule_rule: inherited ? { frequency: "daily", times: ["22:00:00"] } : null,
    auto_enabled_source: inherited
      ? { id: "fixture-source", source: "pixiv", source_url: "https://www.pixiv.net/users/1" }
      : null,
    next_sync_at: inherited ? "2026-08-14T14:00:00Z" : null,
    created_at: "2026-07-27T10:00:00Z",
    updated_at: "2026-08-13T14:07:00Z",
  });

  await page.route("**/api/v1/subscriptions/fixture-subscription", async (route) => {
    if (route.request().method() === "PATCH") {
      updatePayload = JSON.parse(route.request().postData() || "{}");
      inherited = true;
    }
    await route.fulfill({ json: subscription() });
  });
  await page.route("**/api/v1/subscriptions/fixture-subscription/sources", async (route) => {
    await route.fulfill({ json: [{
      id: "fixture-source",
      subscription_id: "fixture-subscription",
      source: "pixiv",
      source_url: "https://www.pixiv.net/users/1",
      source_creator_id: "1",
      is_enabled: inherited,
      auth_healthy: true,
      auth_status: "healthy",
      next_sync_at: inherited ? "2026-08-14T14:00:00Z" : null,
    }] });
  });
  await page.route("**/api/v1/subscriptions/summaries**", async (route) => {
    await route.fulfill({ json: {
      updated_at: "2026-08-13T14:10:00Z",
      items: [{
        subscription_id: "fixture-subscription",
        latest_state: { state: "never_synced", status: null },
        active_count: 0,
        attention_count: 0,
        source_count: 1,
        enabled_source_count: inherited ? 1 : 0,
        schedule: {
          configured_mode: inherited ? "inherit" : "manual",
          effective_mode: inherited ? "calendar" : "manual",
          inherited,
          timezone: "Asia/Shanghai",
          scheduled_times: null,
          schedule_rule: inherited ? { frequency: "daily", times: ["22:00:00"] } : null,
          sync_interval_hours: 6,
          next_due_at: inherited ? "2026-08-14T14:00:00Z" : null,
          oldest_due_at: null,
          due_sources: 0,
          overdue_sources: 0,
          blocked_sources: 0,
        },
      }],
    } });
  });

  await page.goto("/admin/subscriptions/fixture-subscription");
  await expect(page.getByText("Manual Only", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Edit" }).click();
  await page.getByRole("dialog").locator("select").selectOption("inherit");
  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => updatePayload).not.toBeNull();
  expect(updatePayload).toMatchObject({ schedule_mode: "inherit" });
  expect(updatePayload).not.toHaveProperty("sync_enabled");
  await expect(page.locator("dl").getByText("System default · Calendar · Daily at 22:00")).toBeVisible();
  await expect(page.locator("dl").getByText("Manual Only", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.locator("dl").getByText("System default · Calendar · Daily at 22:00")).toBeVisible();
});

test("compact scheduler omits healthy auth details and storage chart footers are removed", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/scheduler#auth-status");
  await expect(page.getByRole("heading", { level: 1, name: "Scheduler" })).toBeVisible();
  await expect(page.locator("#auth-status").getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Auth & Cookie Status" })).toHaveCount(0);
  await expect(page.getByText("Healthy schedules", { exact: true })).toBeVisible();
  await page.screenshot({ path: "/tmp/auto-gallery-scheduler-auth-status.png", fullPage: true });

  await page.goto("/admin/settings/auth-status?from=legacy");
  await expect(page).toHaveURL(/\/admin\/scheduler\?from=legacy#auth-status$/);
  await expect(page.locator("#auth-status").getByRole("heading", { name: "Needs attention" })).toBeVisible();

  await page.goto("/admin/data-mgmt");
  await expect(page.getByText("Source: original media storage scan · exact capacity retained for every source")).toHaveCount(0);
  await expect(page.getByText("Source: creator and repository storage tree · sorted by total storage")).toHaveCount(0);
});

test("scheduler separates task controls from system status permissions", async ({ page }) => {
  let authRequests = 0;
  await page.route("**/api/v1/admin/auth-status", async (route) => {
    authRequests += 1;
    await route.fallback();
  });
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["system"],
      modules: { system: true, tasks: false },
    },
  }));
  await page.goto("/admin/scheduler#auth-status");
  await expect(page.getByRole("heading", { level: 1, name: "Scheduler" })).toBeVisible();
  await expect(page.locator("#auth-status").getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Auth & Cookie Status" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run scheduler scan" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sync all enabled sources" })).toHaveCount(0);
  expect(authRequests).toBe(0);

  authRequests = 0;
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: {
      ...me,
      is_admin: false,
      permissions: ["tasks"],
      modules: { system: false, tasks: true },
    },
  }));
  await page.goto("/admin/scheduler");
  await expect(page.getByRole("button", { name: "Run scheduler scan" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sync all enabled sources" })).toBeVisible();
  await expect(page.locator("#auth-status").getByRole("heading", { name: "Needs attention" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Auth & Cookie Status" })).toHaveCount(0);
  expect(authRequests).toBe(0);
});

test("Danbooru refresh and scheduler sync-all send the bounded batch modes", async ({ page }) => {
  let refreshRequests = 0;
  let schedulerPayload: unknown = null;

  await page.route("**/api/v1/reference/danbooru/mappings/refresh", async (route) => {
    refreshRequests += 1;
    await route.fulfill({ json: {
      status: "enqueued",
      job_id: "mapping-refresh-job",
      operation_type: "danbooru-mapping-refresh",
      message: "Danbooru mapping refresh queued",
    } });
  });
  await page.route("**/api/v1/reference/danbooru/mappings/refresh/mapping-refresh-job", async (route) => {
    await route.fulfill({ json: {
      job_id: "mapping-refresh-job",
      status: "complete",
      operation_type: "danbooru-mapping-refresh",
      progress: { phase: "complete", current: 2, total: 2 },
      result: { scanned: 2, total: 2, found: 2, not_found: 0, errors: 0, skipped: 0, aborted: false },
    } });
  });
  await page.route("**/api/v1/admin/scheduler/sync-now", async (route) => {
    schedulerPayload = JSON.parse(route.request().postData() || "{}");
    await route.fulfill({ json: {
      status: "ok",
      message: "queued",
      task_id: "sync-all-task",
      mode: "manual_all_enabled",
      candidate_count: 3,
      enqueued_count: 2,
      skipped_count: 1,
      error_count: 0,
      job_ids: ["one", "two"],
    } });
  });

  await page.goto("/admin/upload/danbooru");
  await page.getByRole("button", { name: "Refresh all mappings" }).click();
  await expect.poll(() => refreshRequests).toBe(1);
  await expect(page.getByText("The incremental mapping refresh for all creators is queued. Follow its progress in notifications.")).toBeVisible();

  await page.goto("/admin/scheduler");
  await page.getByRole("button", { name: "Sync all enabled sources" }).click();
  await expect.poll(() => schedulerPayload).toEqual({ mode: "manual_all_enabled" });
  await expect(page.getByText("Checked 3 enabled sources: 2 queued, 1 skipped, 0 failed")).toBeVisible();
});

test("mobile data management switcher keeps curation and dedup reachable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/data-mgmt");
  const switcher = page.locator("[data-page-header] details");
  await expect(switcher).toBeVisible();
  await switcher.locator("summary").click();
  await switcher.getByRole("link", { name: "Asset Deduplication" }).click();
  await expect(page).toHaveURL(/\/admin\/data-mgmt\/dedup$/);
  await expectNoPageOverflow(page);
  await page.locator("[data-page-header] details summary").click();
  await page.screenshot({ path: "/tmp/auto-gallery-management-switcher-mobile.png", fullPage: false });
});

test("settings children use clickable breadcrumbs without duplicate back controls", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  const settingsChildren = [
    ["/admin/settings/appearance", "Appearance"],
    ["/admin/settings/backup", "Backup & Restore"],
    ["/admin/settings/dedup", "Deduplication Settings"],
    ["/admin/settings/download-defaults", "Download Job Defaults"],
    ["/admin/settings/gallerydl", "gallery-dl Configuration"],
    ["/admin/settings/logs", "System Logs"],
    ["/admin/settings/proxy", "Network Proxy"],
    ["/admin/settings/scheduler-defaults", "Scheduler Defaults"],
    ["/admin/settings/slideshow", "Slideshow"],
    ["/admin/settings/subscription-defaults", "Subscription Defaults"],
    ["/admin/settings/users", "User Management"],
  ] as const;

  for (const [route, title] of settingsChildren) {
    await page.goto(route);
    const breadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
    await expect(breadcrumb.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/admin/settings");
    await expect(breadcrumb.getByText(title, { exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("button", { name: "Back", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Back", exact: true })).toHaveCount(0);
  }

  await page.goto("/admin/settings/users/1");
  const userBreadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(userBreadcrumb.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/admin/settings");
  await expect(userBreadcrumb.getByRole("link", { name: "User Management" })).toHaveAttribute("href", "/admin/settings/users");
  await expect(userBreadcrumb.getByText("UI Review", { exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("button", { name: "Back", exact: true })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-settings-breadcrumbs.png", fullPage: false });

  await userBreadcrumb.getByRole("link", { name: "User Management" }).click();
  await expect(page).toHaveURL(/\/admin\/settings\/users$/);
  await page.getByRole("navigation", { name: "Breadcrumb" }).getByRole("link", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/admin\/settings$/);
});

test("subscription and repository details use clickable hierarchy breadcrumbs", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });

  await page.goto("/admin/subscriptions/fixture-subscription");
  const subscriptionBreadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(subscriptionBreadcrumb.getByRole("link", { name: "Subscriptions" }))
    .toHaveAttribute("href", "/admin/subscriptions");
  await expect(subscriptionBreadcrumb.getByText("Fixture Subscription", { exact: true }))
    .toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("link", { name: "Back to Subscriptions" })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-subscription-detail-breadcrumb.png", fullPage: false });

  await page.goto("/admin/subscriptions/repositories/fixture-repository");
  const repositoryBreadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(repositoryBreadcrumb.getByRole("link", { name: "Subscriptions" }))
    .toHaveAttribute("href", "/admin/subscriptions");
  await expect(repositoryBreadcrumb.getByRole("link", { name: "Fixture Subscription" }))
    .toHaveAttribute("href", "/admin/subscriptions/fixture-subscription");
  await expect(repositoryBreadcrumb.getByText("pixiv/fixture-source", { exact: true }))
    .toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("link", { name: "Back to creator" })).toHaveCount(0);
  await page.screenshot({ path: "/tmp/auto-gallery-subscription-breadcrumbs.png", fullPage: false });

  await repositoryBreadcrumb.getByRole("link", { name: "Fixture Subscription" }).click();
  await expect(page).toHaveURL(/\/admin\/subscriptions\/fixture-subscription$/);
  await page.getByRole("navigation", { name: "Breadcrumb" })
    .getByRole("link", { name: "Subscriptions" })
    .click();
  await expect(page).toHaveURL(/\/admin\/subscriptions$/);
});

test("slow administrator integrity flow starts once, polls one task, and renders its snapshot", async ({ page }) => {
  let starts = 0;
  let polls = 0;
  let releaseCompletion!: () => void;
  const completionGate = new Promise<void>((resolve) => {
    releaseCompletion = resolve;
  });
  await page.route("**/api/v1/admin/integrity-check/latest", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    await route.fulfill({ json: { snapshot: null } });
  });
  await page.route("**/api/v1/admin/integrity-check", async (route) => {
    expect(route.request().method()).toBe("POST");
    starts += 1;
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      status: 202,
      json: {
        task_id: "integrity-task",
        job_id: "admin-integrity-task-attempt-1",
        status: "enqueued",
        operation_type: "admin-integrity-scan",
      },
    });
  });
  await page.route("**/api/v1/admin/operations/integrity-task", async (route) => {
    polls += 1;
    const complete = polls >= 2;
    if (complete) await completionGate;
    await route.fulfill({
      json: {
        task_id: "integrity-task",
        job_id: "integrity-task",
        rq_job_id: "admin-integrity-task-attempt-1",
        status: complete ? "complete" : "running",
        operation_type: "admin-integrity-scan",
        progress: complete
          ? { phase: "complete", label: "Integrity scan complete" }
          : { phase: "scanning", label: "Scanning data integrity", current: 2, total: 6 },
        result: complete
          ? { issues: [], db_stats: { works: 12 }, checked_at: "2026-08-24T12:00:00Z", message: "Integrity scan complete" }
          : {},
        error: null,
        updated_at: Date.now() / 1000,
      },
    });
  });

  await page.goto("/admin/data-mgmt");
  await expect(page.getByText("Loading the latest successful result…").first()).toBeVisible();
  const runButton = page.getByRole("button", { name: "Run Check" });
  await runButton.click();
  await expect(runButton).toBeDisabled();
  await expect(page.getByRole("link", { name: "Task detail" }).first())
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=integrity-task");
  await expect(page.getByText("Scanning data integrity")).toBeVisible();
  releaseCompletion();
  await expect(page.getByText("All Clear")).toBeVisible({ timeout: 10_000 });
  expect(starts).toBe(1);
  expect(polls).toBeGreaterThanOrEqual(2);
  expect(polls).toBeLessThanOrEqual(3);

  const results = await new AxeBuilder({ page })
    .include("[data-admin-operation='admin-integrity-scan']")
    .analyze();
  expect(results.violations).toEqual([]);
});

test("slow administrator proxy failure exposes structured retry and the successful result", async ({ page }) => {
  let retried = false;
  let postPending = false;
  let proxyStarts = 0;
  let taskPolls = 0;
  await page.route("**/api/v1/admin/proxy/test/latest", (route) => route.fulfill({
    json: { snapshot: null },
  }));
  await page.route("**/api/v1/admin/proxy/test", async (route) => {
    expect(route.request().method()).toBe("POST");
    proxyStarts += 1;
    postPending = true;
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      status: 202,
      json: {
        task_id: "proxy-task",
        job_id: "admin-proxy-task-attempt-1",
        status: "enqueued",
        operation_type: "admin-proxy-test",
      },
    });
  });
  await page.route("**/api/v1/admin/operations/proxy-task/retry", async (route) => {
    retried = true;
    await route.fulfill({
      status: 202,
      json: {
        task_id: "proxy-task",
        job_id: "admin-proxy-task-attempt-2",
        status: "enqueued",
        operation_type: "admin-proxy-test",
      },
    });
  });
  await page.route("**/api/v1/admin/operations/proxy-task", async (route) => {
    taskPolls += 1;
    if (!retried) {
      await route.fulfill({
        json: {
          task_id: "proxy-task",
          job_id: "proxy-task",
          status: "failed",
          operation_type: "admin-proxy-test",
          progress: { phase: "failed", label: "Operation failed" },
          result: {},
          error: "Proxy probe worker exited unexpectedly",
          reason_code: "worker_crash",
        },
      });
      return;
    }
    const complete = taskPolls >= 3;
    await route.fulfill({
      json: {
        task_id: "proxy-task",
        job_id: "proxy-task",
        status: complete ? "complete" : "running",
        operation_type: "admin-proxy-test",
        progress: complete
          ? { phase: "complete", label: "Proxy connectivity test complete" }
          : { phase: "testing", label: "Testing proxy connectivity" },
        result: complete ? {
          proxy_enabled: true,
          proxy_reachable: true,
          proxy_reachable_error: "",
          proxy_config: { http: "configured", https: "configured" },
          results: [{
            name: "Pixiv", url: "https://www.pixiv.net", direct_ok: true,
            direct_ms: 25, direct_error: "", proxy_ok: true, proxy_ms: 30, proxy_error: "",
          }],
          message: "Proxy connectivity test complete",
        } : {},
        error: null,
      },
    });
  });

  await page.goto("/admin/settings/proxy");
  const start = page.getByRole("button", { name: "Test Now" });
  await start.click();
  await expect.poll(() => postPending).toBe(true);
  await expect(page.getByRole("button", { name: "Starting…" })).toBeDisabled();
  await expect(page.locator("[data-admin-operation='admin-proxy-test']").getByRole("alert"))
    .toContainText("Proxy probe worker exited unexpectedly");
  await expect(page.getByText("worker_crash")).toBeVisible();
  await expect(page.getByRole("link", { name: "Task detail" }))
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=proxy-task");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Testing proxy connectivity")).toBeVisible();
  await expect(page.getByText("Proxy is reachable")).toBeVisible({ timeout: 10_000 });
  expect(retried).toBe(true);
  await expect(start).toBeEnabled();
  await start.click();
  await expect.poll(() => proxyStarts).toBe(2);

  const results = await new AxeBuilder({ page })
    .include("[data-admin-operation='admin-proxy-test']")
    .analyze();
  expect(results.violations).toEqual([]);
});

test("backup estimate and creation use independent TaskRuns without request waterfalls", async ({ page }) => {
  let estimateStarts = 0;
  let createStarts = 0;
  await page.route("**/api/v1/admin/backup/estimate/latest", (route) => route.fulfill({
    json: {
      snapshot: {
        task_id: "old-estimate",
        job_id: "admin-old-estimate-attempt-1",
        status: "complete",
        operation_type: "admin-backup-estimate",
        progress: { phase: "complete", label: "Backup estimate complete" },
        result: { components: { database: 4, "gallerydl-config": 2 }, message: "Backup estimate complete" },
        completed_at: "2026-08-24T12:00:00Z",
      },
    },
  }));
  await page.route("**/api/v1/admin/backup/latest", (route) => route.fulfill({ json: { snapshot: null } }));
  await page.route("**/api/v1/admin/backup/estimate", async (route) => {
    expect(route.request().method()).toBe("POST");
    estimateStarts += 1;
    await route.fulfill({ status: 202, json: {
      task_id: "estimate-task", job_id: "admin-estimate-task-attempt-1",
      status: "enqueued", operation_type: "admin-backup-estimate",
    } });
  });
  await page.route("**/api/v1/admin/backup", async (route) => {
    expect(route.request().method()).toBe("POST");
    createStarts += 1;
    await route.fulfill({ status: 202, json: {
      task_id: "backup-task", job_id: "admin-backup-task-attempt-1",
      status: "enqueued", operation_type: "admin-backup-create",
    } });
  });
  await page.route("**/api/v1/admin/operations/estimate-task", (route) => route.fulfill({ json: {
    task_id: "estimate-task", job_id: "estimate-task", status: "complete",
    operation_type: "admin-backup-estimate",
    progress: { phase: "complete", label: "Backup estimate complete" },
    result: { components: { database: 8, "gallerydl-config": 3 }, message: "Backup estimate complete" },
  } }));
  await page.route("**/api/v1/admin/operations/backup-task", (route) => route.fulfill({ json: {
    task_id: "backup-task", job_id: "backup-task", status: "complete",
    operation_type: "admin-backup-create",
    progress: { phase: "complete", label: "Backup created" },
    result: { filename: "auto-gallery-backup_20260824_120000.tar.gz", size_mb: 12.5, message: "Backup created" },
  } }));

  await page.goto("/admin/settings/backup");
  await expect(page.getByText("6 KB")).toBeVisible();
  await page.getByRole("button", { name: "Refresh estimate" }).click();
  await expect(page.getByText("11 KB")).toBeVisible();
  await page.getByRole("button", { name: "Create Backup" }).click();
  await expect(page.getByText("auto-gallery-backup_20260824_120000.tar.gz")).toBeVisible();
  await expect(page.getByRole("link", { name: "Task detail" }).last())
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=backup-task");
  expect(estimateStarts).toBe(1);
  const createButton = page.getByRole("button", { name: "Create Backup" });
  await expect(createButton).toBeEnabled();
  await createButton.click();
  await expect.poll(() => createStarts).toBe(2);

  const results = await new AxeBuilder({ page })
    .include("[data-admin-operation]")
    .analyze();
  expect(results.violations).toEqual([]);
});

test("restore stages ordered chunks, validates once, and surfaces external rollback diagnostics", async ({ page }) => {
  const uploadId = "00000000-0000-0000-0000-000000000123";
  const token = "restore-capability";
  const restoreBytes = Buffer.alloc(2 * 1024 * 1024 + 3, 7);
  const expectedArchiveHash = createHash("sha256").update(restoreBytes).digest("hex");
  const chunkIndexes: number[] = [];
  let validationStarts = 0;
  let latestPolls = 0;
  let taskPolls = 0;
  let receiptPolls = 0;

  await page.route("**/api/v1/admin/backup/restore/uploads", async (route) => {
    expect(route.request().method()).toBe("POST");
    const body = route.request().postDataJSON();
    expect(body.filename).toBe("restore-fixture.tar.gz");
    expect(body.total_chunks).toBe(3);
    expect(body.sha256).toBe(expectedArchiveHash);
    await route.fulfill({ status: 201, json: {
      upload_id: uploadId,
      upload_token: token,
      filename: body.filename,
      size_bytes: body.size_bytes,
      sha256: body.sha256,
      chunk_size: body.chunk_size,
      total_chunks: body.total_chunks,
      received_chunks: 0,
      received_bytes: 0,
      next_chunk: 0,
      state: "uploading",
      created_at: "2026-08-24T12:00:00Z",
      updated_at: "2026-08-24T12:00:00Z",
    } });
  });
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}/chunks/*`, async (route) => {
    const index = Number(new URL(route.request().url()).pathname.split("/").at(-1));
    expect(route.request().method()).toBe("PUT");
    expect(route.request().headers()["x-restore-token"]).toBe(token);
    const chunkBody = route.request().postDataBuffer();
    expect(chunkBody).not.toBeNull();
    expect(route.request().headers()["x-chunk-sha256"]).toBe(
      createHash("sha256").update(chunkBody!).digest("hex"),
    );
    chunkIndexes.push(index);
    await route.fulfill({ json: {
      upload_id: uploadId,
      next_chunk: index + 1,
      received_chunks: index + 1,
      received_bytes: Math.min((index + 1) * 1024 * 1024, 2 * 1024 * 1024 + 3),
      total_chunks: 3,
      state: index === 2 ? "uploaded" : "uploading",
      idempotent: false,
    } });
  });
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}/validation/latest`, (route) => {
    latestPolls += 1;
    return route.fulfill({ json: { snapshot: null } });
  });
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}/validate`, async (route) => {
    validationStarts += 1;
    await route.fulfill({ status: 202, json: {
      task_id: "restore-validation-task",
      job_id: "admin-restore-validation-task-attempt-1",
      status: "enqueued",
      operation_type: "admin-restore-validate",
    } });
  });
  await page.route("**/api/v1/admin/operations/restore-validation-task", async (route) => {
    taskPolls += 1;
    if (taskPolls === 1) {
      await route.fulfill({ json: {
        task_id: "restore-validation-task",
        job_id: "admin-restore-validation-task-attempt-1",
        status: "running",
        operation_type: "admin-restore-validate",
        progress: { phase: "validating", label: "Validating restore archive", current: 2, total: 3 },
        result: null,
      } });
      return;
    }
    await route.fulfill({ json: {
      task_id: "restore-validation-task",
      job_id: "admin-restore-validation-task-attempt-1",
      status: "complete",
      operation_type: "admin-restore-validate",
      progress: { phase: "ready", label: "Ready for offline host execution" },
      result: {
        state: "ready",
        request_id: uploadId,
        host_command: `./scripts/offline-restore.py --request "$HOST_RESTORE_STAGING/${uploadId}/ready-request.json"`,
        manifest: { version: "0.3.0", contents: ["database"] },
        message: "Restore request is ready for offline host execution",
      },
    } });
  });
  await page.route(`**/api/v1/admin/backup/restore/receipts/${uploadId}`, async (route) => {
    receiptPolls += 1;
    await route.fulfill({ json: receiptPolls === 1
      ? { request_id: uploadId, status: "pending", phase: "handoff" }
      : {
          request_id: uploadId,
          status: "recovery_failed",
          phase: "integrity",
          rollback_performed: true,
          rollback_status: "failed",
          diagnostic: "Foreground services only; background writers remain stopped.",
          error: "Restore failed during integrity: RestoreHostError",
          rollback_components: {
            files: { status: "complete" },
            database: { status: "failed", error: "RestoreHostError: identity unproven" },
            redis: { status: "complete" },
            foreground: { status: "complete" },
          },
        },
    });
  });

  await page.goto("/admin/settings/backup");
  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles({
    name: "restore-fixture.tar.gz",
    mimeType: "application/gzip",
    buffer: restoreBytes,
  });
  await page.getByRole("dialog").getByRole("button", { name: "Confirm" }).click();

  await expect(page.getByText("Ready for offline host execution")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(uploadId, { exact: true })).toBeVisible();
  await expect(page.getByText("Foreground services only; background writers remain stopped.")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("Rollback needs manual diagnosis")).toBeVisible();
  await expect(page.getByText("database: failed")).toBeVisible();
  await expect(page.getByText("RestoreHostError: identity unproven")).toBeVisible();
  expect(chunkIndexes).toEqual([0, 1, 2]);
  expect(validationStarts).toBe(1);
  expect(latestPolls).toBe(1);
  expect(taskPolls).toBe(2);
  expect(receiptPolls).toBe(2);

  const results = await new AxeBuilder({ page })
    .include("[data-restore-flow]")
    .analyze();
  expect(results.violations).toEqual([]);
});

test("restore validation recovers from session token after response loss and remount", async ({ page }) => {
  const uploadId = "00000000-0000-0000-0000-000000000987";
  const token = "restore-remount-capability";
  const session = {
    upload_id: uploadId,
    filename: "auto-gallery-backup_20260824_120000.tar.gz",
    size_bytes: 123,
    sha256: "a".repeat(64),
    chunk_size: 123,
    total_chunks: 1,
    received_chunks: 1,
    received_bytes: 123,
    next_chunk: 1,
    state: "uploaded",
    validation_task_id: null,
    request_id: null,
    created_at: "2026-08-24T12:00:00Z",
    updated_at: "2026-08-24T12:00:01Z",
  };
  let validationStarts = 0;
  let latestRequests = 0;
  let completed = false;

  await page.addInitScript(({ savedSession, savedToken }) => {
    window.localStorage.setItem(
      "auto-gallery-restore-upload-v1",
      JSON.stringify({ session: savedSession, token: savedToken }),
    );
  }, { savedSession: session, savedToken: token });
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}`, (route) => route.fulfill({
    json: {
      ...session,
      validation_task_id: validationStarts ? "restore-remount-task" : null,
      state: validationStarts ? "validating" : "uploaded",
    },
  }));
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}/validation/latest`, (route) => {
    latestRequests += 1;
    if (!validationStarts) return route.fulfill({ json: { snapshot: null, current: null } });
    if (completed) return route.fulfill({ json: {
      current: null,
      snapshot: {
        task_id: "restore-remount-task",
        job_id: "admin-restore-remount-task-attempt-1",
        status: "complete",
        operation_type: "admin-restore-validate",
        progress: { phase: "ready", label: "Ready for offline host execution" },
        result: {
          state: "ready",
          request_id: uploadId,
          host_command: "./scripts/offline-restore.py --request ready-request.json",
          manifest: { version: "0.3.0", contents: ["database"] },
          message: "Restore request is ready for offline host execution",
        },
        completed_at: "2026-08-24T12:00:02Z",
      },
    } });
    return route.fulfill({ json: {
      snapshot: null,
      current: {
        task_id: "restore-remount-task",
        job_id: "admin-restore-remount-task-attempt-1",
        status: "running",
        operation_type: "admin-restore-validate",
        progress: { phase: "validating", label: "Validating restore archive" },
      },
    } });
  });
  await page.route(`**/api/v1/admin/backup/restore/uploads/${uploadId}/validate`, async (route) => {
    validationStarts += 1;
    // PostgreSQL accepted the exact-scope TaskRun, but the HTTP/Redis handoff
    // response is lost. The client must discover current state, not POST again.
    await route.abort("connectionreset");
  });
  await page.route("**/api/v1/admin/operations/restore-remount-task", async (route) => {
    completed = true;
    await route.fulfill({ json: {
      task_id: "restore-remount-task",
      job_id: "admin-restore-remount-task-attempt-1",
      status: "complete",
      operation_type: "admin-restore-validate",
      progress: { phase: "ready", label: "Ready for offline host execution" },
      result: {
        state: "ready",
        request_id: uploadId,
        host_command: "./scripts/offline-restore.py --request ready-request.json",
        manifest: { version: "0.3.0", contents: ["database"] },
        message: "Restore request is ready for offline host execution",
      },
      error: null,
    } });
  });
  await page.route(`**/api/v1/admin/backup/restore/receipts/${uploadId}`, (route) => route.fulfill({
    json: { request_id: uploadId, status: "pending", phase: "handoff" },
  }));

  await page.goto("/admin/settings/backup");
  await expect(page.getByText("Ready for offline host execution")).toBeVisible({ timeout: 10_000 });
  expect(validationStarts).toBe(1);
  expect(latestRequests).toBeGreaterThanOrEqual(2);

  await page.reload();
  await expect(page.getByText("Ready for offline host execution")).toBeVisible({ timeout: 10_000 });
  expect(validationStarts).toBe(1);
});

test("gallery-dl connectivity saves then starts one asynchronous source test", async ({ page }) => {
  let configSaves = 0;
  let testStarts = 0;
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv", (route) => route.fulfill({
    json: { snapshot: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection", async (route) => {
    expect(route.request().method()).toBe("POST");
    testStarts += 1;
    await route.fulfill({ status: 202, json: {
      task_id: "gallery-task", job_id: "admin-gallery-task-attempt-1",
      status: "enqueued", operation_type: "admin-gallerydl-connectivity-test",
    } });
  });
  await page.route("**/api/v1/admin/gallerydl-config", async (route) => {
    if (route.request().method() === "PUT") {
      configSaves += 1;
      await route.fulfill({ json: { status: "ok", message: "saved", path: "/config/gallery-dl.conf" } });
      return;
    }
    await route.fulfill({ json: {
      pixiv: {}, twitter: {}, iwara: {}, danbooru: {}, pinterest: {}, lofter: {}, weibo: {}, bilibili: {},
      sources: { pixiv: { name: "Pixiv", supported: true, description: "Pixiv source" } },
    } });
  });
  await page.route("**/api/v1/admin/operations/gallery-task", (route) => route.fulfill({ json: {
    task_id: "gallery-task", job_id: "gallery-task", status: "complete",
    operation_type: "admin-gallerydl-connectivity-test",
    progress: { phase: "complete", label: "Connection test passed" },
    result: { source: "pixiv", success: true, message: "Connection test passed for pixiv.", details: "ok" },
  } }));

  await page.goto("/admin/settings/gallerydl");
  await page.getByRole("button", { name: "Test Connection" }).click();
  await expect(page.getByText("Connection test passed for pixiv.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Task detail" }))
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=gallery-task");
  expect(configSaves).toBe(1);
  expect(testStarts).toBe(1);

  const results = await new AxeBuilder({ page })
    .include("[data-admin-operation='admin-gallerydl-connectivity-test']")
    .analyze();
  expect(results.violations).toEqual([]);
});

test("gallery-dl connectivity never carries a completed task across source tabs", async ({ page }) => {
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=twitter", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection", (route) => route.fulfill({
    status: 202,
    json: {
      task_id: "pixiv-gallery-task",
      job_id: "admin-pixiv-gallery-task-attempt-1",
      status: "enqueued",
      operation_type: "admin-gallerydl-connectivity-test",
    },
  }));
  await page.route("**/api/v1/admin/gallerydl-config", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: { status: "ok", message: "saved", path: "/config/gallery-dl.conf" } });
      return;
    }
    await route.fulfill({ json: {
      pixiv: {}, twitter: {}, iwara: {}, danbooru: {}, pinterest: {}, lofter: {}, weibo: {}, bilibili: {},
      sources: {
        pixiv: { name: "Pixiv", supported: true, description: "Pixiv source" },
        twitter: { name: "X / Twitter", supported: true, description: "Twitter source" },
      },
    } });
  });
  await page.route("**/api/v1/admin/operations/pixiv-gallery-task", (route) => route.fulfill({ json: {
    task_id: "pixiv-gallery-task",
    job_id: "admin-pixiv-gallery-task-attempt-1",
    status: "complete",
    operation_type: "admin-gallerydl-connectivity-test",
    progress: { phase: "complete", label: "Pixiv connection complete" },
    result: { source: "pixiv", success: true, message: "Pixiv-only result", details: "ok" },
    error: null,
  } }));

  await page.goto("/admin/settings/gallerydl");
  await page.getByRole("button", { name: "Test Connection" }).click();
  await expect(page.getByText("Pixiv-only result")).toBeVisible();

  await page.getByRole("tab", { name: "X / Twitter" }).click();
  await expect(page.getByText("Pixiv-only result")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Task detail" })).toHaveCount(0);
  await expect(page.getByText("No successful result yet.")).toBeVisible();
});

test("gallery-dl connectivity serializes cross-scope retry and preserves both tasks", async ({ page }) => {
  let pixivRetried = false;
  let retryRequested = false;
  let releaseRetry!: () => void;
  const retryGate = new Promise<void>((resolve) => { releaseRetry = resolve; });

  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=twitter", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection", async (route) => {
    const body = JSON.parse(route.request().postData() || "{}");
    const taskId = body.source === "twitter" ? "twitter-scope-task" : "pixiv-scope-task";
    await route.fulfill({ status: 202, json: {
      task_id: taskId,
      job_id: `admin-${taskId}-attempt-1`,
      status: "enqueued",
      operation_type: "admin-gallerydl-connectivity-test",
    } });
  });
  await page.route("**/api/v1/admin/gallerydl-config", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: { status: "ok", message: "saved", path: "/config/gallery-dl.conf" } });
      return;
    }
    await route.fulfill({ json: {
      pixiv: {}, twitter: {}, iwara: {}, danbooru: {}, pinterest: {}, lofter: {}, weibo: {}, bilibili: {},
      sources: {
        pixiv: { name: "Pixiv", supported: true, description: "Pixiv source" },
        twitter: { name: "X / Twitter", supported: true, description: "Twitter source" },
      },
    } });
  });
  await page.route("**/api/v1/admin/operations/pixiv-scope-task", (route) => route.fulfill({ json: {
    task_id: "pixiv-scope-task",
    job_id: pixivRetried
      ? "admin-pixiv-scope-task-attempt-2"
      : "admin-pixiv-scope-task-attempt-1",
    status: pixivRetried ? "running" : "failed",
    operation_type: "admin-gallerydl-connectivity-test",
    progress: pixivRetried
      ? { phase: "testing", label: "Retrying Pixiv" }
      : { phase: "failed", label: "Pixiv failed" },
    result: null,
    error: pixivRetried ? null : "Pixiv unavailable",
  } }));
  await page.route("**/api/v1/admin/operations/twitter-scope-task", (route) => route.fulfill({ json: {
    task_id: "twitter-scope-task",
    job_id: "admin-twitter-scope-task-attempt-1",
    status: "running",
    operation_type: "admin-gallerydl-connectivity-test",
    progress: { phase: "testing", label: "Testing Twitter" },
    result: null,
    error: null,
  } }));
  await page.route("**/api/v1/admin/operations/pixiv-scope-task/retry", async (route) => {
    retryRequested = true;
    await retryGate;
    pixivRetried = true;
    await route.fulfill({ status: 202, json: {
      task_id: "pixiv-scope-task",
      job_id: "admin-pixiv-scope-task-attempt-2",
      status: "enqueued",
      operation_type: "admin-gallerydl-connectivity-test",
    } });
  });

  await page.goto("/admin/settings/gallerydl");
  await page.getByRole("button", { name: "Test Connection" }).click();
  const operation = page.locator("[data-admin-operation='admin-gallerydl-connectivity-test']");
  await expect(operation.getByRole("alert")).toContainText("Pixiv unavailable");
  await operation.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => retryRequested).toBe(true);

  await page.getByRole("tab", { name: "X / Twitter" }).click();
  const testConnection = page.getByRole("button", { name: "Test Connection" });
  await expect(testConnection).toBeDisabled();
  releaseRetry();
  await expect(testConnection).toBeEnabled();
  await testConnection.click();
  await expect(operation.getByRole("link", { name: "Task detail" }))
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=twitter-scope-task");

  await page.getByRole("tab", { name: "Pixiv" }).click();
  await expect(operation.getByRole("link", { name: "Task detail" }))
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=pixiv-scope-task");
  await expect(page.getByText("Retrying Pixiv")).toBeVisible();
});

test("gallery-dl connectivity blocks cross-scope retry while a start is pending", async ({ page }) => {
  let startRequested = false;
  let retryRequested = false;
  let releaseStart!: () => void;
  const startGate = new Promise<void>((resolve) => { releaseStart = resolve; });

  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=twitter", (route) => route.fulfill({
    json: {
      snapshot: null,
      current: {
        task_id: "twitter-failed-task",
        job_id: "admin-twitter-failed-task-attempt-1",
        status: "failed",
        operation_type: "admin-gallerydl-connectivity-test",
      },
    },
  }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection", async (route) => {
    startRequested = true;
    await startGate;
    await route.fulfill({ status: 202, json: {
      task_id: "pixiv-start-task",
      job_id: "admin-pixiv-start-task-attempt-1",
      status: "enqueued",
      operation_type: "admin-gallerydl-connectivity-test",
    } });
  });
  await page.route("**/api/v1/admin/gallerydl-config", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ json: { status: "ok", message: "saved", path: "/config/gallery-dl.conf" } });
      return;
    }
    await route.fulfill({ json: {
      pixiv: {}, twitter: {}, iwara: {}, danbooru: {}, pinterest: {}, lofter: {}, weibo: {}, bilibili: {},
      sources: {
        pixiv: { name: "Pixiv", supported: true, description: "Pixiv source" },
        twitter: { name: "X / Twitter", supported: true, description: "Twitter source" },
      },
    } });
  });
  await page.route("**/api/v1/admin/operations/twitter-failed-task", (route) => route.fulfill({ json: {
    task_id: "twitter-failed-task",
    job_id: "admin-twitter-failed-task-attempt-1",
    status: "failed",
    operation_type: "admin-gallerydl-connectivity-test",
    progress: { phase: "failed", label: "Twitter failed" },
    result: null,
    error: "Twitter unavailable",
  } }));
  await page.route("**/api/v1/admin/operations/twitter-failed-task/retry", async (route) => {
    retryRequested = true;
    await route.fulfill({ status: 202, json: {
      task_id: "twitter-failed-task",
      job_id: "admin-twitter-failed-task-attempt-2",
      status: "enqueued",
      operation_type: "admin-gallerydl-connectivity-test",
    } });
  });

  await page.goto("/admin/settings/gallerydl");
  await page.getByRole("button", { name: "Test Connection" }).click();
  await expect.poll(() => startRequested).toBe(true);

  await page.getByRole("tab", { name: "X / Twitter" }).click();
  const retry = page.locator("[data-admin-operation='admin-gallerydl-connectivity-test']")
    .getByRole("button", { name: "Retry" });
  await expect(retry).toBeDisabled();
  expect(retryRequested).toBe(false);

  releaseStart();
  await expect(retry).toBeEnabled();
  await retry.click();
  await expect.poll(() => retryRequested).toBe(true);
});

test("proxy operation discovery starts with settings and reattaches across reload", async ({ page }) => {
  let latestRequestedAt = 0;
  let settingsFulfilledAt = 0;
  let starts = 0;
  let operationState: "running" | "failed" | "complete" = "running";

  await page.context().unroute("**/api/v1/**");
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: me }));
  await page.route("**/api/v1/system/workbench", (route) => route.fulfill({ json: workbench }));
  await page.route("**/api/v1/operations/overview**", (route) => route.fulfill({ json: {
    view: "attention", total: 0,
    summary: { attention: 0, critical: 0, warning: 0, resolved: 0, active: 0, resource_limited: 0 },
    items: [],
  } }));

  await page.route("**/api/v1/admin/settings**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
    settingsFulfilledAt = Date.now();
    await route.fulfill({ json: {
      proxy: { enabled: true, http_proxy: "http://proxy.example:7890", https_proxy: "", no_proxy: "", ssl_verify: true },
    } });
  });
  await page.route("**/api/v1/admin/proxy/test/latest", async (route) => {
    latestRequestedAt ||= Date.now();
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({ json: operationState === "complete" ? {
      current: null,
      snapshot: {
        task_id: "reattached-proxy-task", job_id: "admin-reattached-proxy-task-attempt-1",
        status: "complete", operation_type: "admin-proxy-test",
        progress: { phase: "complete", label: "Proxy connectivity test complete" },
        result: {
          proxy_enabled: true, proxy_reachable: true, proxy_reachable_error: "",
          proxy_config: { http: "http://proxy.example:7890", https: "not set" }, results: [],
        },
        completed_at: "2026-08-24T12:00:00Z",
      },
    } : operationState === "running" ? {
      snapshot: null,
      current: {
        task_id: "reattached-proxy-task", job_id: "admin-reattached-proxy-task-attempt-1",
        status: "running", operation_type: "admin-proxy-test",
        progress: { phase: "testing", label: "Testing proxy connectivity" },
      },
    } : { snapshot: null, current: null } });
  });
  await page.route("**/api/v1/admin/proxy/test", async (route) => {
    if (route.request().method() === "POST") starts += 1;
    await route.fulfill({ status: 409, json: { detail: "duplicate start" } });
  });
  await page.route("**/api/v1/admin/operations/reattached-proxy-task", async (route) => {
    await route.fulfill({ json: operationState === "complete" ? {
      task_id: "reattached-proxy-task", job_id: "reattached-proxy-task", status: "complete",
      operation_type: "admin-proxy-test", progress: { phase: "complete", label: "Proxy connectivity test complete" },
      result: {
        proxy_enabled: true, proxy_reachable: true, proxy_reachable_error: "",
        proxy_config: { http: "http://proxy.example:7890", https: "not set" }, results: [],
      }, error: null,
    } : operationState === "failed" ? {
      task_id: "reattached-proxy-task", job_id: "reattached-proxy-task", status: "failed",
      operation_type: "admin-proxy-test", progress: { phase: "failed", label: "Proxy test failed" },
      result: {}, error: "Proxy endpoint unavailable", reason_code: "task_failed",
    } : {
      task_id: "reattached-proxy-task", job_id: "reattached-proxy-task", status: "running",
      operation_type: "admin-proxy-test", progress: { phase: "testing", label: "Testing proxy connectivity" },
      result: {}, error: null,
    } });
  });
  await page.route("**/api/v1/admin/operations/reattached-proxy-task/retry", async (route) => {
    operationState = "complete";
    await route.fulfill({ status: 202, json: {
      task_id: "reattached-proxy-task", job_id: "admin-reattached-proxy-task-attempt-2",
      status: "enqueued", operation_type: "admin-proxy-test",
    } });
  });

  await page.goto("/admin/settings/proxy");
  await expect.poll(() => latestRequestedAt).toBeGreaterThan(0);
  await expect.poll(() => settingsFulfilledAt).toBeGreaterThan(0);
  expect(latestRequestedAt).toBeLessThan(settingsFulfilledAt);
  const start = page.getByRole("button", { name: /Test Now|Testing/ });
  await expect(start).toBeDisabled();
  await expect(page.getByText("Testing proxy connectivity")).toBeVisible();
  await expect(page.getByRole("link", { name: "Task detail" }))
    .toHaveAttribute("href", "/admin/jobs?tab=admin&task=reattached-proxy-task");

  await page.reload();
  await expect(start).toBeDisabled();
  await expect(page.getByText("Testing proxy connectivity")).toBeVisible();
  expect(starts).toBe(0);

  operationState = "failed";
  const operation = page.locator("[data-admin-operation='admin-proxy-test']");
  await expect(operation.getByRole("alert")).toContainText("Proxy endpoint unavailable", { timeout: 10_000 });
  await page.waitForTimeout(1_500);
  await expect(operation.getByRole("button", { name: "Retry" })).toBeVisible();
  await operation.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByText("Proxy is reachable")).toBeVisible({ timeout: 10_000 });
  await expect(start).toBeEnabled();
  expect(starts).toBe(0);
});

test("gallery-dl operation discovery is concurrent with its config request", async ({ page }) => {
  let latestRequestedAt = 0;
  let configFulfilledAt = 0;
  await page.context().unroute("**/api/v1/**");
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: me }));
  await page.route("**/api/v1/system/workbench", (route) => route.fulfill({ json: workbench }));
  await page.route("**/api/v1/operations/overview**", (route) => route.fulfill({ json: {
    view: "attention", total: 0,
    summary: { attention: 0, critical: 0, warning: 0, resolved: 0, active: 0, resource_limited: 0 },
    items: [],
  } }));
  await page.route("**/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv", async (route) => {
    latestRequestedAt = Date.now();
    await route.fulfill({ json: { snapshot: null, current: null } });
  });
  await page.route("**/api/v1/admin/gallerydl-config", async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
    configFulfilledAt = Date.now();
    await route.fulfill({ json: {
      pixiv: {}, twitter: {}, iwara: {}, danbooru: {}, pinterest: {}, lofter: {}, weibo: {}, bilibili: {},
      sources: { pixiv: { name: "Pixiv", supported: true, description: "Pixiv source" } },
    } });
  });

  await page.goto("/admin/settings/gallerydl");
  await expect(page.getByRole("button", { name: "Test Connection" })).toBeVisible();
  expect(latestRequestedAt).toBeGreaterThan(0);
  expect(latestRequestedAt).toBeLessThan(configFulfilledAt);
});

test("integrity scan failure never renders All Clear and remains retryable", async ({ page }) => {
  await page.route("**/api/v1/admin/integrity-check/latest", (route) => route.fulfill({
    json: { snapshot: null, current: null },
  }));
  await page.route("**/api/v1/admin/integrity-check", (route) => route.fulfill({
    status: 202,
    json: {
      task_id: "failed-integrity-task", job_id: "admin-failed-integrity-task-attempt-1",
      status: "enqueued", operation_type: "admin-integrity-scan",
    },
  }));
  await page.route("**/api/v1/admin/operations/failed-integrity-task", (route) => route.fulfill({ json: {
    task_id: "failed-integrity-task", job_id: "failed-integrity-task", status: "failed",
    operation_type: "admin-integrity-scan", progress: { phase: "failed", label: "Operation failed" },
    result: {}, error: "Integrity database unavailable", reason_code: "task_failed",
  } }));

  await page.goto("/admin/data-mgmt");
  await page.getByRole("button", { name: "Run Check" }).click();
  await expect(page.locator("[data-admin-operation='admin-integrity-scan']").getByRole("alert"))
    .toContainText("Integrity database unavailable");
  await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
  await expect(page.getByText("All Clear")).toHaveCount(0);
});

test("profile is independent and the legacy settings URL redirects with its query", async ({ page }) => {
  await page.goto("/admin/profile");
  const breadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(breadcrumb.getByRole("link", { name: "Dashboard" })).toHaveAttribute("href", "/admin");
  await expect(breadcrumb.getByText("Profile", { exact: true })).toHaveAttribute("aria-current", "page");

  const response = await page.goto("/admin/settings/profile?from=legacy");
  expect(response?.status()).toBe(200);
  await expect(page).toHaveURL(/\/admin\/profile\?from=legacy$/);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toBeVisible();
  await page.screenshot({ path: "/tmp/auto-gallery-profile-breadcrumb-mobile.png", fullPage: false });
});

test("restricted direct access renders the standard shell permission state", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({
    json: { ...me, is_admin: false, permissions: ["library"], modules: { library: true, system: false } },
  }));
  await page.goto("/admin/settings/logs");
  await expect(page.locator("[data-page-shell]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
  await expect(page.getByRole("main")).toHaveCount(1);

  await page.goto("/admin/system");
  await expect(page.locator("[data-page-shell]")).toBeVisible();
  await expect(page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
  await expect(page.getByRole("tab")).toHaveCount(0);
});

test("route changes focus main content and dismissible menus restore trigger focus", async ({ page }) => {
  await page.goto("/admin/jobs?tab=downloads");
  const userMenu = page.getByRole("button", { name: "User menu" });
  await userMenu.click();
  await expect(userMenu).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(userMenu).toHaveAttribute("aria-expanded", "false");
  await expect(userMenu).toBeFocused();

  const notificationBell = page.locator('header button[aria-label="Notifications"]');
  await notificationBell.click();
  await page.keyboard.press("Escape");
  await expect(notificationBell).toBeFocused();

  await page.locator("#admin-sidebar").getByRole("link", { name: "Works" }).click();
  await expect(page).toHaveURL(/\/admin\/works$/);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
});

test("pathname navigation resets the viewport without hiding the page heading", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/upload");
  await expect(page.getByRole("heading", { level: 1, name: "Upload" })).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  await page.locator("#admin-sidebar").getByRole("link", { name: "Tags" }).click();
  await expect(page).toHaveURL(/\/admin\/tags$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");

  await page.locator("#admin-sidebar").getByRole("link", { name: "Upload" }).click();
  await expect(page).toHaveURL(/\/admin\/upload$/);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  await expect.poll(() => page.evaluate(() => {
    const heading = document.querySelector("h1")?.getBoundingClientRect();
    const topbar = document.querySelector("header.sticky")?.getBoundingClientRect();
    return Boolean(heading && topbar && heading.top >= topbar.bottom);
  })).toBe(true);
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
  await page.screenshot({ path: "/tmp/auto-gallery-upload-top-fixed.png", fullPage: false });
});

test("tag map loads every tag and supports ctrl-wheel zoom without pagination", async ({ page }) => {
  const consoleIssues: string[] = [];
  page.on("console", (message) => {
    const text = message.text();
    // Chromium does not expose the URL in this generic resource message and
    // can emit it for a late optional shell request. The tag API itself is
    // fulfilled below, while runtime exceptions remain covered by pageerror.
    const isAnonymousNotFound =
      text === "Failed to load resource: the server responded with a status of 404 ()";
    if (
      (message.type() === "error" || message.type() === "warning")
      && !isAnonymousNotFound
    ) {
      consoleIssues.push(text);
    }
  });
  page.on("pageerror", (error) => consoleIssues.push(error.message));
  await page.addInitScript(() => {
    const prototype = CanvasRenderingContext2D.prototype as unknown as {
      arc: (this: CanvasRenderingContext2D, x: number, y: number, radius: number, start: number, end: number, ...rest: unknown[]) => void;
      stroke: (this: CanvasRenderingContext2D, ...args: unknown[]) => void;
    };
    const originalArc = prototype.arc;
    const originalStroke = prototype.stroke;
    const lastArc = new WeakMap<CanvasRenderingContext2D, [number, number, number, number, number]>();
    const strokes: Array<{ x: number; y: number; radius: number; start: number; end: number; color: string; lineWidth: number }> = [];
    Object.assign(window, { __tagBubbleRingStrokes: strokes });
    prototype.arc = function(this: CanvasRenderingContext2D, x, y, radius, start, end, ...rest) {
      lastArc.set(this, [x, y, radius, start, end]);
      return originalArc.call(this, x, y, radius, start, end, ...rest);
    };
    prototype.stroke = function(...args) {
      const arc = lastArc.get(this);
      if (arc) {
        strokes.push({
          x: arc[0],
          y: arc[1],
          radius: arc[2],
          start: arc[3],
          end: arc[4],
          color: String(this.strokeStyle),
          lineWidth: this.lineWidth,
        });
      }
      return originalStroke.apply(this, args);
    };
  });
  const fixtureCount = Number(process.env.TAG_MAP_FIXTURE_COUNT || 240);
  const categoryFixtures = ["meta", "general", "artist", "character", "copyright", "unknown"];
  const tagFixtures = Array.from({ length: fixtureCount }, (_, index) => ({
    id: `map-tag-${index}`,
    normalized_name: `map_tag_${String(index).padStart(3, "0")}`,
    category: categoryFixtures[index] || (index % 5 === 0 ? "meta" : "general"),
    usage_count: index < categoryFixtures.length ? 999 - index : 1 + ((index * 37) % 500),
    source_usage: index === 0
      ? [{ source: "pixiv", work_count: 3 }, { source: "iwara", work_count: 1 }]
      : [{ source: "pixiv", work_count: 1 }],
    created_at: "2026-08-14T00:00:00Z",
  }));
  let includeAll = false;
  await page.route("**/api/v1/tags?*", async (route) => {
    const url = new URL(route.request().url());
    includeAll = url.searchParams.get("include_all") === "true";
    await route.fulfill({ json: tagFixtures });
  });

  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/admin/tags");

  const chart = page.getByTestId("tag-bubble-chart");
  await expect(chart).toBeVisible({ timeout: 15_000 });
  await expect(page).toHaveURL(/\/admin\/tags$/);
  await expect(page).toHaveTitle(/auto-gallery/i);
  await expect(page.getByRole("heading", { level: 1, name: "Tags" })).toBeVisible();
  await expect(page.locator("[data-nextjs-dialog-overlay]")).toHaveCount(0);
  await expect(chart).toHaveAttribute("data-tag-count", String(fixtureCount));
  expect(includeAll).toBe(true);
  await expect(page.getByText("Ctrl + wheel to zoom · Drag to pan")).toBeVisible();
  if (fixtureCount <= 1_000) {
    const metaBubble = page.getByRole("link", { name: /map_tag_000, meta, 999, pixiv 3, iwara 1/i });
    await expect(metaBubble).toBeVisible();
    await expect(metaBubble).toHaveAttribute("data-bubble-fill", "hsl(32 34% 22%)");
    await expect(metaBubble).toHaveAttribute("data-bubble-ring", "pixiv:#0066FF:3|iwara:#EC4899:1");
    await expect(metaBubble).toHaveAttribute("data-bubble-text", "hsl(32 55% 88%)");
    for (const [index, hue] of [32, 216, 0, 120, 275, 210].entries()) {
      await expect(page.getByRole("link", { name: new RegExp(`map_tag_${String(index).padStart(3, "0")}`) }))
        .toHaveAttribute("data-bubble-fill", `hsl(${hue} 34% 22%)`);
    }

    const readRenderedRing = async () => {
      const bubble = await metaBubble.boundingBox();
      expect(bubble).not.toBeNull();
      return page.evaluate((bubbleBox) => {
        const canvas = document.querySelector<HTMLCanvasElement>("[data-testid='tag-bubble-chart'] canvas");
        if (!canvas || !bubbleBox) throw new Error("tag bubble canvas is unavailable");
        const canvasBox = canvas.getBoundingClientRect();
        const scaleX = canvas.width / canvasBox.width;
        const scaleY = canvas.height / canvasBox.height;
        const centerX = (bubbleBox.x - canvasBox.x + bubbleBox.width / 2) * scaleX;
        const centerY = (bubbleBox.y - canvasBox.y + bubbleBox.height / 2) * scaleY;
        return (window as typeof window & {
          __tagBubbleRingStrokes: Array<{ x: number; y: number; radius: number; start: number; end: number; color: string; lineWidth: number }>;
        }).__tagBubbleRingStrokes.filter((stroke) => (
          Math.abs(stroke.x - centerX) < 1
          && Math.abs(stroke.y - centerY) < 1
          && Math.abs(stroke.radius - bubbleBox.width * scaleX / 2) < 1
          && ["#0066ff", "rgb(0, 102, 255)", "#ec4899", "rgb(236, 72, 153)"].includes(stroke.color.toLowerCase())
        )).slice(-2);
      }, bubble);
    };

    const initialRing = await readRenderedRing();
    expect(initialRing).toHaveLength(2);
    expect(initialRing.find((stroke) => stroke.color.toLowerCase() === "#0066ff" || stroke.color === "rgb(0, 102, 255)")?.end).toBeCloseTo(Math.PI * 1.5);
    expect(initialRing.find((stroke) => stroke.color.toLowerCase() === "#ec4899" || stroke.color === "rgb(236, 72, 153)")?.end).toBeCloseTo(Math.PI * 2);
    expect(initialRing.reduce((total, stroke) => total + (stroke.end - stroke.start), 0)).toBeCloseTo(Math.PI * 2);
    expect(initialRing.map((stroke) => stroke.lineWidth)).toEqual([1.5, 1.5]);

    await metaBubble.hover();
    await expect(page.getByText("pixiv 3, iwara 1")).toBeVisible();
    await expect.poll(async () => (await readRenderedRing()).map((stroke) => stroke.lineWidth))
      .toEqual([3, 3]);
  }

  const initialZoom = Number(await chart.getAttribute("data-zoom-level"));
  const box = await chart.boundingBox();
  expect(box).not.toBeNull();
  await chart.dispatchEvent("wheel", { deltaY: -260 });
  await expect(chart).toHaveAttribute("data-zoom-level", initialZoom.toFixed(3));
  await chart.dispatchEvent("wheel", {
    ctrlKey: true,
    deltaY: -260,
    clientX: (box?.x || 0) + (box?.width || 0) / 2,
    clientY: (box?.y || 0) + (box?.height || 0) / 2,
  });
  await expect.poll(async () => Number(await chart.getAttribute("data-zoom-level")))
    .toBeGreaterThan(initialZoom);
  await expectNoPageOverflow(page);
  expect(consoleIssues).toEqual([]);
  await page.screenshot({ path: "/tmp/auto-gallery-tag-map-zoomed.png", fullPage: false });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(chart).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-tag-map-mobile.png", fullPage: false });
});

test("query-only task navigation preserves the current viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 600 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.getByText("xianyuliangryo-with-a-very-long-creator-name")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 260));
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  const retainedY = await page.evaluate(() => window.scrollY);
  await page.getByRole("tab", { name: "Import" }).evaluate((element: HTMLElement) => element.click());
  await expect(page).toHaveURL(/\/admin\/jobs\?tab=imports$/);
  await expect.poll(() => page.evaluate((targetY) => {
    const maxScrollY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    return Math.abs(window.scrollY - Math.min(targetY, maxScrollY)) <= 1;
  }, retainedY)).toBe(true);
});

test("shared dialogs trap focus, close with Escape, and restore their trigger", async ({ page }) => {
  await page.route("**/api/v1/admin/clear/preview/all", (route) => route.fulfill({ json: {
    entity: "all",
    confirmation_phrase: "DELETE-ALL-DATA",
    counts: {},
    preserves_repository_sync_receipts: false,
    deletes_media_files: true,
  } }));
  await page.goto("/admin/data-mgmt");
  const trigger = page.getByRole("button", { name: "Delete All Data" });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("textbox", { name: "Type DELETE-ALL-DATA to confirm" }).fill("DELETE-ALL-DATA");
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  await page.keyboard.press("Shift+Tab");
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("admin creator deletion loads impact, defaults file cleanup off, and submits the chosen mode", async ({ page }) => {
  let deleteFiles: string | null = null;
  await page.route("**/api/v1/creators/fixture-creator**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/deletion-preview")) {
      await route.fulfill({ json: {
        entity_type: "creator",
        entity_ids: ["fixture-creator"],
        mode: "permanent",
        can_delete_files: true,
        active_task_count: 0,
        active_job_count: 0,
        active_task_ids: [],
        affected_work_count: 12,
        exclusive_work_count: 8,
        shared_work_count: 4,
        exclusive_asset_count: 16,
      } });
      return;
    }
    if (url.pathname === "/api/v1/creators/fixture-creator" && route.request().method() === "DELETE") {
      deleteFiles = url.searchParams.get("delete_files");
      await route.fulfill({ status: 202, json: {
        status: "enqueued",
        mode: "permanent",
        entity_type: "creator",
        entity_ids: ["fixture-creator"],
        delete_files: deleteFiles === "true",
        task_id: "fixture-delete-task",
      } });
      return;
    }
    await route.fallback();
  });

  await page.goto("/admin/creators/fixture-creator");
  await page.getByRole("button", { name: "Permanently delete" }).click();
  const dialog = page.getByRole("dialog", { name: "Permanently delete" });
  await expect(dialog.getByText("12")).toBeVisible();
  await expect(dialog.getByText("8")).toBeVisible();
  await expect(dialog.getByText("4")).toBeVisible();
  const cleanup = dialog.getByRole("checkbox", { name: /Also permanently delete exclusive work files/ });
  await expect(cleanup).not.toBeChecked();
  await cleanup.check();
  await dialog.getByRole("textbox", { name: "Type Fixture Creator to confirm" }).fill("Fixture Creator");
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect.poll(() => deleteFiles).toBe("true");
  await expect(page).toHaveURL(/\/admin\/creators$/);
});

test("curation user sees recoverable creator archive without permanent file controls", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: {
    ...me,
    is_admin: false,
    permissions: ["library", "curation"],
    modules: { library: true, curation: true },
  } }));
  let requested = false;
  await page.route("**/api/v1/creators/fixture-creator**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/deletion-preview")) {
      await route.fulfill({ json: {
        entity_type: "creator",
        entity_ids: ["fixture-creator"],
        mode: "soft",
        can_delete_files: false,
        active_task_count: 0,
        active_job_count: 0,
        active_task_ids: [],
        affected_work_count: 12,
        exclusive_work_count: 8,
        shared_work_count: 4,
        exclusive_asset_count: 16,
      } });
      return;
    }
    if (url.pathname === "/api/v1/creators/fixture-creator" && route.request().method() === "DELETE") {
      requested = true;
      expect(url.searchParams.get("delete_files")).toBe("false");
      await route.fulfill({ json: {
        status: "soft_deleted",
        mode: "soft",
        entity_type: "creator",
        entity_ids: ["fixture-creator"],
        delete_files: false,
      } });
      return;
    }
    await route.fallback();
  });

  await page.goto("/admin/creators/fixture-creator");
  await page.getByRole("button", { name: "Archive" }).click();
  const dialog = page.getByRole("dialog", { name: "Disable and hide" });
  await expect(dialog).toContainText("Configuration and files are not permanently removed");
  await expect(dialog.getByRole("checkbox")).toHaveCount(0);
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect.poll(() => requested).toBe(true);
  await expect(page).toHaveURL(/\/admin\/creators$/);
});

test("compact tablet sidebar and long job metadata do not create root overflow", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.locator("aside").first()).toHaveCSS("width", "64px");
  await expect(page.getByText("xianyuliangryo-with-a-very-long-creator-name")).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-tablet.png", fullPage: true });
});

test("completed syncs are omitted from the actionable download page", async ({ page }) => {
  let requestedVisibility: string | null = null;
  await page.route("**/api/v1/download-jobs**", async (route) => {
    const url = new URL(route.request().url());
    requestedVisibility = url.searchParams.get("visibility");
    await route.fulfill({ json: [] });
  });
  await page.goto("/admin/jobs?tab=downloads");
  await expect.poll(() => requestedVisibility).toBe("actionable");
  await expect(page.locator("span").filter({ hasText: /^Complete$/ })).toHaveCount(0);
  await expect(page.getByText("Sync completed; no new works were found to import.")).toHaveCount(0);
});

test("200 percent equivalent reflow and reduced motion keep content visible", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  // A 720 CSS-pixel viewport represents a 1440-pixel desktop at 200% zoom.
  await page.setViewportSize({ width: 720, height: 900 });
  await page.goto("/admin/jobs?tab=downloads");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
  await expect(page.locator(".page-item").first()).toHaveCSS("opacity", "1");
  await expectNoPageOverflow(page);
});

test("390 pixel mobile layout stays within the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin/upload/danbooru");
  await expect(page.getByRole("heading", { level: 1, name: "Danbooru Reference Mapping" })).toBeVisible();
  await expect(page.locator("[data-page-header] details")).toHaveCount(0);
  await expect(page.locator('[data-page-header] nav[aria-label="Related pages"]')).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-danbooru-single-active-mobile.png", fullPage: false });
});

test("Gitllery v1 settings expose safe shadow controls, CLI copy, verify, and creator navigation", async ({ context, page }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/admin/settings");
  const settingsLink = page.getByRole("link", { name: /Gitllery/ });
  await expect(settingsLink).toHaveAttribute("href", "/admin/settings/gitllery");
  await settingsLink.click();

  await expect(page.getByRole("heading", { level: 1, name: "Gitllery Settings" })).toBeVisible();
  await expect(page.getByText("Gitllery v1 is fixed to shadow mode")).toBeVisible();
  await expect(page.getByText("segment-r1-fixture")).toBeVisible();
  await expect(page.getByRole("button", { name: "Reconcile: Unavailable" })).toBeDisabled();
  await expect(page.getByText("Each commit accepts at most 25 works and 100 operations.")).toBeVisible();

  await page.getByRole("button", { name: "Copy config command" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toBe("gitllery config set url http://auto-gallery.test");

  const verifyRequest = page.waitForRequest((request) => (
    request.url().includes("/api/v1/curation/gitllery/verify")
    && request.method() === "POST"
  ));
  await page.getByRole("button", { name: "Queue verify" }).click();
  await verifyRequest;
  await expect(page.getByText("Bounded Gitllery v1 verify task queued")).toBeVisible();

  await page.goto("/admin/creators/fixture-creator");
  await expect(page.getByRole("link", { name: "Open Gitllery log" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open Gitllery settings" })).toHaveCount(0);
});

test("repository detail scopes history and opens the full work search", async ({ page }) => {
  await page.route("**/api/v1/curation/repositories/fixture-repository/gitllery/status", (route) => route.fulfill({
    json: {
      repositories: [{
        repository_id: "fixture-repository",
        source: "pixiv",
        creator_dir: "fixture-source",
        exists: true,
        behind: 0,
        object_integrity_ok: true,
        drift: [],
        clean: true,
        product_version: "v1",
        format_id: "gitllery-segment",
        format_revision: 1,
        projection_mode: "shadow",
      }],
      missing_repos: 0,
      behind_total: 0,
      product_version: "v1",
      format_id: "gitllery-segment",
      format_revision: 1,
      projection_mode: "shadow",
    },
  }));
  await page.route("**/api/v1/curation/repositories/fixture-repository/gitllery/log", (route) => route.fulfill({
    json: {
      repository_id: "fixture-repository",
      total: 1,
      entries: [{
        commit: "segment-123",
        message: "Repository-only projection",
        trigger: "source_synced",
        occurred_at: "2026-07-28T10:00:00Z",
        change_count: 2,
      }],
    },
  }));

  await page.goto("/admin/subscriptions/repositories/fixture-repository");
  await expect(page.getByRole("button", { name: /Content\s*13/ })).toBeVisible();
  await page.getByRole("button", { name: /Content\s*13/ }).click();
  await expect(page.getByRole("link", { name: "View all 13 works" })).toHaveAttribute(
    "href",
    "/admin/works?q=repo%3Afixture-repository%20sort%3Aposted-desc",
  );

  await page.getByRole("button", { name: /Sync history/ }).click();
  await expect(page.getByRole("heading", { name: "Synchronization history" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Repository curation graph" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Gitllery status" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Gitllery log" })).toBeVisible();
  await expect(page.getByText("Repository-only projection")).toBeVisible();
});

test("mobile drawer is discoverable, dismissible, and the task page stays in bounds", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/admin/jobs?tab=downloads");
  const trigger = page.locator("header button[aria-controls]").first();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator("#admin-mobile-sidebar")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#admin-mobile-sidebar")).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(page.locator("#admin-mobile-sidebar")).toBeVisible();
  await page.locator("#admin-mobile-sidebar").evaluate((element) => {
    for (const animation of element.getAnimations()) animation.finish();
  });
  const mobileBrand = page.locator("#admin-mobile-sidebar [data-sidebar-brand]");
  await expect(mobileBrand).toHaveAccessibleName("Go to dashboard");
  await page.screenshot({ path: "/tmp/auto-gallery-sidebar-brand-mobile.png", fullPage: false });
  await mobileBrand.click();
  await expect(page).toHaveURL(/\/admin$/);
  await expect(page.locator("#admin-mobile-sidebar")).toBeHidden();
  await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("main-content");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: "/tmp/auto-gallery-jobs-mobile.png", fullPage: true });
});

test("Escape ignores a queued drawer autofocus after focus restoration", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/admin/jobs?tab=downloads");
  await page.evaluate(() => {
    const testWindow = window as Window & {
      __drawerFocusFrames?: Map<number, FrameRequestCallback>;
      __flushDrawerFocusFrames?: () => void;
    };
    let nextFrame = 0;
    const frames = new Map<number, FrameRequestCallback>();
    testWindow.__drawerFocusFrames = frames;
    window.requestAnimationFrame = (callback) => {
      const frame = ++nextFrame;
      frames.set(frame, callback);
      return frame;
    };
    window.cancelAnimationFrame = (frame) => {
      frames.delete(frame);
    };
    testWindow.__flushDrawerFocusFrames = () => {
      const queued = [...frames.values()];
      frames.clear();
      for (const callback of queued) callback(performance.now());
    };
  });

  const trigger = page.locator("header button[aria-controls]").first();
  const drawer = page.locator("#admin-mobile-sidebar");
  await trigger.click();
  await expect(drawer).toBeVisible();
  await expect.poll(() => page.evaluate(() => {
    const testWindow = window as Window & { __drawerFocusFrames?: Map<number, FrameRequestCallback> };
    return testWindow.__drawerFocusFrames?.size ?? 0;
  })).toBe(1);

  await page.keyboard.press("Escape");
  await expect(drawer).toHaveClass(/drawer-left-exit/);
  await page.evaluate(() => {
    const testWindow = window as Window & { __flushDrawerFocusFrames?: () => void };
    testWindow.__flushDrawerFocusFrames?.();
  });
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
});
