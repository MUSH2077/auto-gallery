import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

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
    } else if (path === "/api/v1/creators/fixture-creator/timeline") {
      const year = Number((url.searchParams.get("from_date") || "2026").slice(0, 4));
      const days = year === 2025
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
          sources: ["pixiv", "x"],
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
        },
      });
    } else if (path === "/api/v1/admin/storage-breakdown") {
      await route.fulfill({ json: { sources: {}, creator_tree: [], unlinked_repositories: [] } });
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
  await expect(sidebar.locator("nav a")).toHaveCount(11);
  await expect(sidebar.locator("nav").getByRole("link", { name: "Dashboard", exact: true })).toHaveCount(0);
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAttribute("href", "/admin");
  await expect(sidebar.locator("[data-sidebar-brand]")).toHaveAccessibleName("Go to dashboard");
  await expect(sidebar.getByRole("heading", { name: "Upload & Import" })).toBeVisible();
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

test("creator charts share the data contract and year selection reloads the requested range", async ({ page }) => {
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
  await activityGrid.focus();
  const firstActiveDay = await activityGrid.getAttribute("aria-activedescendant");
  await page.keyboard.press("ArrowRight");
  const nextActiveDay = await activityGrid.getAttribute("aria-activedescendant");
  expect(nextActiveDay).not.toBe(firstActiveDay);
  await page.keyboard.press("Enter");
  await expect(page.locator('.activity-desktop [aria-live="polite"]')).toBeVisible();
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
  await page.getByLabel("Year", { exact: true }).selectOption("2025");
  const request = await request2025;
  const requestUrl = new URL(request.url());
  expect(requestUrl.searchParams.get("to_date")).toBe("2026-01-01");
  await expect(page.getByTestId("creator-activity-chart")).toContainText("Activity peaked on");

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
  await expect(page.locator(".activity-mobile button")).toHaveCount(12);
  await page.locator(".activity-mobile button").nth(1).click();
  await expect(page.locator(".activity-mobile")).toContainText("February publishing details");
  await expect(page.locator(".activity-mobile")).toContainText("pixiv: 2 works");
  await page.screenshot({ path: "/tmp/auto-gallery-creator-charts-mobile.png", fullPage: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await expect(page.getByTestId("creator-activity-chart")).toBeVisible();
  await expect(page.locator(".chart-dot-enter")).toHaveCount(0);
  await expectNoPageOverflow(page);
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
          effective_mode: "fixed_time",
          inherited: true,
          timezone: "Asia/Shanghai",
          scheduled_times: "22:00",
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
    await expect(page.getByText("System default · Daily at 22:00")).toBeVisible();
    await expectNoPageOverflow(page);
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
    effective_mode: inherited ? "fixed_time" : "manual",
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
          effective_mode: inherited ? "fixed_time" : "manual",
          inherited,
          timezone: "Asia/Shanghai",
          scheduled_times: inherited ? "22:00" : null,
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
  await expect(page.locator("dl").getByText("System default · Fixed time · Daily at 22:00")).toBeVisible();
  await expect(page.locator("dl").getByText("Manual Only", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.locator("dl").getByText("System default · Fixed time · Daily at 22:00")).toBeVisible();
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
  const fixtureCount = Number(process.env.TAG_MAP_FIXTURE_COUNT || 240);
  const tagFixtures = Array.from({ length: fixtureCount }, (_, index) => ({
    id: `map-tag-${index}`,
    normalized_name: `map_tag_${String(index).padStart(3, "0")}`,
    category: index % 5 === 0 ? "meta" : "general",
    usage_count: 1 + ((index * 37) % 500),
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
  await expect(page.getByRole("button", { name: "Previous" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Next" })).toHaveCount(0);

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
  if (fixtureCount <= 1_000) {
    const firstBubble = page.getByRole("link", { name: /map_tag_/ }).first();
    await expect(firstBubble).toBeVisible();
    await expect(firstBubble).toHaveAttribute("data-bubble-fill", / 34% 22%\)$/);
    await expect(firstBubble).toHaveAttribute("data-bubble-border", / 36% 38%\)$/);
    await expect(firstBubble).toHaveAttribute("data-bubble-text", / 55% 88%\)$/);
  }
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
  await expect(page.getByRole("link", { name: "Open Gitllery log" })).toHaveAttribute(
    "href",
    "/admin/data-mgmt/curation?subject_type=creator&subject_id=fixture-creator",
  );
  const creatorSettingsLink = page.getByRole("link", { name: "Open Gitllery settings" });
  await expect(creatorSettingsLink).toHaveAttribute("href", "/admin/settings/gitllery");
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
