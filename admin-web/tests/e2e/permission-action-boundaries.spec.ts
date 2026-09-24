import { expect, test, type Browser, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

type Principal = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  is_active: boolean;
  permissions: string[];
  modules: Record<string, string>;
  preferences: Record<string, never>;
  nsfw_visible: boolean;
  upload_used_bytes: number;
  must_change_password: boolean;
};

const principal = (permissions: string[], isAdmin = false): Principal => ({
  id: isAdmin ? 1 : permissions.join("").length + 10,
  username: isAdmin ? "admin" : permissions.join("-") || "denied",
  display_name: isAdmin ? "Admin" : "Operator",
  is_admin: isAdmin,
  is_active: true,
  permissions,
  modules: {
    library: "Library",
    curation: "Curation",
    upload: "Upload",
    subscriptions: "Subscriptions",
    tasks: "Tasks",
    system: "System",
  },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
});

const creator = {
  id: "creator-1",
  name: "Creator One",
  display_name: "Creator One",
  created_at: "2026-09-13T00:00:00Z",
  updated_at: "2026-09-13T00:00:00Z",
};

const links = [
  { id: "website-link", creator_id: "creator-1", link_type: "website", url: "https://creator.example", confidence: 0.8, is_verified: false, source: "manual", created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z" },
  { id: "pixiv-link", creator_id: "creator-1", link_type: "pixiv", url: "https://pixiv.net/users/7", confidence: 0.8, is_verified: false, source: "pixiv", created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z" },
  { id: "verified-link", creator_id: "creator-1", link_type: "website", url: "https://verified.example", confidence: 1, is_verified: true, source: "manual", created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z" },
];

const repositoryDetail = {
  repository: {
    id: "repo-1", subscription_id: "sub-1", source: "pixiv", source_url: "https://pixiv.net/users/7",
    source_creator_id: "7", is_repository: true, is_enabled: true, auth_healthy: true, url_valid: true,
    last_synced_at: null, last_attempted_at: null, latest_job: null, created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z",
  },
  creator,
  subscription: { id: "sub-1", creator_id: "creator-1", name: "Creator One", is_active: true, sync_enabled: true, sync_interval_hours: 6, schedule_mode: "inherit", scheduled_times: null, created_at: "2026-09-13T00:00:00Z", updated_at: "2026-09-13T00:00:00Z" },
  provider: { source: "pixiv", display_name: "Pixiv", normalized_url: "https://www.pixiv.net/users/7", url_valid: true, capabilities: { can_download: true, can_import_local: true, supports_gallerydl: true, supports_tags: true, is_reference_only: false } },
  recent_jobs: [], sync_history: [], recent_works: [], work_total: 0,
};

const gitllerySettings = {
  product_name: "Gitllery", product_version: "v1", format_id: "gitllery-segment", format_revision: 1,
  projection_mode: "shadow", build_generation: "permission-fixture", managed_by: "deployment_environment", read_only: true,
  capabilities: Object.fromEntries(["automatic_projection", "reconcile", "backfill", "rebuild", "push", "pull", "verify", "commit"].map((name) => [name, { enabled: name === "verify" || name === "commit", reason: name === "verify" || name === "commit" ? null : "gitllery_shadow_only" }])),
  cli: { max_works_per_commit: 25, max_operations_per_commit: 100, token_storage: "client_only", server_stores_cli_token: false, examples: { config: "config", login: "login", status: "status", log: "log", verify: "verify", commit: "commit" } },
  governance_scope: { observation: "host_and_auto_gallery", enforcement: "auto_gallery_only", modifies_other_projects: false, modifies_host_configuration: false },
  status: { repositories: [{ repository_id: "repo-1", source: "pixiv", creator_dir: "Creator One", exists: true, behind: 0, object_integrity_ok: true, drift: [], clean: true, product_version: "v1", format_id: "gitllery-segment", format_revision: 1, projection_mode: "shadow", head_segment: "segment-head", last_complete_commit_id: "commit-1" }], missing_repos: 0, behind_total: 0, needs_reconcile: false, product_version: "v1", format_id: "gitllery-segment", format_revision: 1, projection_mode: "shadow" },
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

type ApiHandler = (route: Route, path: string, method: string) => Promise<boolean>;

async function openFixture(browser: Browser, pathname: string, me: Principal, handler: ApiHandler, init?: (context: BrowserContext) => Promise<void>) {
  const context = await browser.newContext();
  const fixtureOrigin = new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").origin;
  await context.addCookies([{ name: "ag_session", value: "fixture", url: fixtureOrigin }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => { localStorage.setItem("ag_token", "fixture"); localStorage.setItem("auto-gallery-lang", "en"); });
  await init?.(context);
  const unhandled: string[] = [];
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (await handler(route, path, method)) return;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, { updated_at: "2026-09-13T00:00:00Z", queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    if (path.includes("/notifications")) return json(route, { total: 0, items: [], unread_count: 0 });
    unhandled.push(`${method} ${path}`);
    return json(route, { detail: `Unhandled fixture request: ${method} ${path}` }, 501);
  });
  const page = await context.newPage();
  await page.goto(pathname);
  return { context, page, unhandled };
}

async function creatorsFixture(route: Route, path: string, method: string, writes: string[]) {
  if (path === "/api/v1/creators/creator-1" && method === "GET") { await json(route, creator); return true; }
  if (path === "/api/v1/creators/creator-1/links" && method === "GET") { await json(route, links); return true; }
  if (path.startsWith("/api/v1/creators/creator-1/links/") && method === "PATCH") { writes.push(`${method} ${path}`); await json(route, { ...links[0], is_verified: true, confidence: 1 }); return true; }
  if (path === "/api/v1/subscriptions" || path.includes("/sources")) { writes.push(`${method} ${path}`); await json(route, []); return true; }
  return false;
}

async function creatorDetailFixture(route: Route, path: string, method: string, writes: string[]) {
  if (await creatorsFixture(route, path, method, writes)) return true;
  if (path === "/api/v1/creators/creator-1/stats" && method === "GET") {
    await json(route, {
      creator_id: "creator-1", total_works: 0, total_assets: 0, total_tags: 0,
      source_breakdown: [], tag_distribution: [], monthly_frequency: [{ month: "2026-09", count: 0 }],
    });
    return true;
  }
  if (path === "/api/v1/creators/creator-1/timeline" && method === "GET") {
    await json(route, { creator_id: "creator-1", sources: [], days: [], total: 0 });
    return true;
  }
  if (path === "/api/v1/creators/creator-1/subscription-overview" && method === "GET") {
    await json(route, {
      creator_id: "creator-1",
      subscriptions: [repositoryDetail.subscription],
      repositories: [repositoryDetail.repository],
      summary: { subscription_count: 1, repository_count: 1, enabled_repository_count: 1, running_job_count: 0 },
    });
    return true;
  }
  if (path === "/api/v1/creators/creator-1/references" && method === "GET") {
    await json(route, {
      pixiv: [{
        source_creator_id: "7", display_name: "Reference Alias", username: "reference_alias",
        profile_url: "https://www.pixiv.net/users/7", avatar_url: null, status: "remote",
      }],
      danbooru: null,
    });
    return true;
  }
  if (path === "/api/v1/search" && method === "GET") {
    await json(route, { groups: { works: { total: 0, items: [] } } });
    return true;
  }
  if (path === "/api/v1/repositories/repo-1/sync-now" && method === "POST") {
    writes.push(`${method} ${path}`);
    await json(route, { status: "enqueued", job_id: "job-1" });
    return true;
  }
  if (path === "/api/v1/download-jobs" && method === "POST") {
    writes.push(`${method} ${path}`);
    await json(route, { id: "job-1", status: "pending" }, 201);
    return true;
  }
  return false;
}

test("creator read pages stop denied hooks and library-only users cannot mount curation actions", async ({ browser }) => {
  for (const pathname of ["/admin/creators/creator-1/mapping", "/admin/creators/duplicates"]) {
    const protectedRequests: string[] = [];
    const denied = await openFixture(browser, pathname, principal([]), async (route, path, method) => {
      if (path.startsWith("/api/v1/creators")) { protectedRequests.push(`${method} ${path}`); await json(route, { detail: "Library permission required" }, 403); return true; }
      return false;
    });
    try {
      await expect(denied.page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
      expect(protectedRequests).toEqual([]);
      expect(denied.unhandled).toEqual([]);
    } finally { await denied.context.close(); }
  }

  const mappingWrites: string[] = [];
  const mapping = await openFixture(browser, "/admin/creators/creator-1/mapping", principal(["library"]), (route, path, method) => creatorsFixture(route, path, method, mappingWrites));
  try {
    await expect(mapping.page.getByRole("heading", { name: "Mapping: Creator One" })).toBeVisible();
    await expect(mapping.page.getByRole("button", { name: "+ Add Link" })).toHaveCount(0);
    await expect(mapping.page.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(mapping.page.getByRole("button", { name: "Unverify" })).toHaveCount(0);
    expect(mappingWrites).toEqual([]);
    expect(mapping.unhandled).toEqual([]);
  } finally { await mapping.context.close(); }

  const duplicateWrites: string[] = [];
  const duplicates = await openFixture(browser, "/admin/creators/duplicates", principal(["library"]), async (route, path, method) => {
    if (path === "/api/v1/creators/duplicates") { await json(route, { total: 2, duplicates: [{ reason: "same_identity", description: "Fixture group", creator_ids: ["target", "source"], creator_names: ["Target", "Source"] }] }); return true; }
    if (path === "/api/v1/creators/merge") { duplicateWrites.push(`${method} ${path}`); await json(route, { status: "ok", results: [] }); return true; }
    return false;
  });
  try {
    await expect(duplicates.page.getByText("Fixture group", { exact: true })).toBeVisible();
    await expect(duplicates.page.getByRole("checkbox")).toHaveCount(0);
    await expect(duplicates.page.getByRole("button", { name: /Merge \d/ })).toHaveCount(0);
    expect(duplicateWrites).toEqual([]);
    expect(duplicates.unhandled).toEqual([]);
  } finally { await duplicates.context.close(); }
});

test("system logs stop the protected query behind the permission guard", async ({ browser }) => {
  const protectedRequests: string[] = [];
  const denied = await openFixture(
    browser,
    "/admin/settings/logs",
    principal([]),
    async (route, path, method) => {
      if (path === "/api/v1/system/logs") {
        protectedRequests.push(`${method} ${path}`);
        await json(route, { detail: "System permission required" }, 403);
        return true;
      }
      return false;
    },
  );
  try {
    await expect(denied.page.getByRole("heading", { name: "You don't have permission to access this page" })).toBeVisible();
    expect(protectedRequests).toEqual([]);
    expect(denied.unhandled).toEqual([]);
  } finally {
    await denied.context.close();
  }
});

test("curation actions remain available while repository setup requires subscriptions permission", async ({ browser }) => {
  const writes: string[] = [];
  const opened = await openFixture(
    browser,
    "/admin/creators/creator-1/mapping",
    principal(["library", "curation"]),
    (route, path, method) => creatorsFixture(route, path, method, writes),
    async (context) => {
      await context.addInitScript(() => {
        localStorage.setItem("auto-gallery-setup-recovery:25:creator-1", JSON.stringify({
          "pixiv-link": { link: { id: "pixiv-link", creator_id: "creator-1", link_type: "pixiv", url: "https://pixiv.net/users/7", confidence: 1, is_verified: true }, stage: "subscription", message: "setup failed" },
        }));
      });
    },
  );
  try {
    await expect(opened.page.getByRole("button", { name: "+ Add Link" })).toBeVisible();
    await expect(opened.page.getByRole("button", { name: "Unverify" })).toBeVisible();
    const websiteCard = opened.page.locator(".card").filter({ hasText: "https://creator.example" });
    const pixivCard = opened.page.locator(".card").filter({ hasText: "https://pixiv.net/users/7" });
    await expect(websiteCard.getByRole("button", { name: "Approve" })).toBeEnabled();
    await expect(pixivCard.getByRole("button", { name: "Approve" })).toBeDisabled();
    await expect(opened.page.getByText("Subscriptions permission is required to verify and set up repository links.", { exact: true }).first()).toBeVisible();
    await expect(opened.page.getByRole("button", { name: "Retry repository setup" })).toHaveCount(0);
    await websiteCard.getByRole("button", { name: "Approve" }).click();
    await opened.page.getByRole("button", { name: "Confirm" }).click();
    await expect.poll(() => writes).toEqual(["PATCH /api/v1/creators/creator-1/links/website-link"]);
    expect(opened.unhandled).toEqual([]);
  } finally { await opened.context.close(); }
});

test("creator merge and repository enable remain available only to their write roles", async ({ browser }) => {
  const mergeWrites: string[] = [];
  const duplicates = await openFixture(browser, "/admin/creators/duplicates", principal(["library", "curation"]), async (route, path, method) => {
    if (path === "/api/v1/creators/duplicates") { await json(route, { total: 2, duplicates: [{ reason: "same_identity", description: "Fixture group", creator_ids: ["target", "source"], creator_names: ["Target", "Source"] }] }); return true; }
    if (path === "/api/v1/creators/merge") { mergeWrites.push(`${method} ${path}`); await json(route, { status: "ok", results: [{ source_id: "source", status: "merged" }] }); return true; }
    return false;
  });
  try {
    await duplicates.page.getByRole("checkbox", { name: /Source/ }).click();
    await duplicates.page.getByRole("button", { name: "Merge 1 → Target" }).click();
    await duplicates.page.getByRole("button", { name: "Confirm" }).click();
    await expect.poll(() => mergeWrites).toEqual(["POST /api/v1/creators/merge"]);
    expect(duplicates.unhandled).toEqual([]);
  } finally { await duplicates.context.close(); }

  for (const role of [
    { permissions: ["library"], canManage: false },
    { permissions: ["library", "subscriptions"], canManage: true },
  ]) {
    const patchWrites: string[] = [];
    const repository = await openFixture(browser, "/admin/subscriptions/repositories/repo-1", principal(role.permissions), async (route, path, method) => {
      if (path === "/api/v1/repositories/repo-1" && method === "GET") { await json(route, repositoryDetail); return true; }
      if (path === "/api/v1/subscriptions/sub-1/sources/repo-1" && method === "PATCH") { patchWrites.push(`${method} ${path}`); await json(route, { ...repositoryDetail.repository, is_enabled: false }); return true; }
      return false;
    });
    try {
      await expect(repository.page.getByRole("button", { name: "Sync now", exact: true }).first()).toBeVisible();
      const toggle = repository.page.getByRole("button", { name: "Disable", exact: true });
      if (role.canManage) {
        await expect(toggle).toBeVisible();
        await toggle.click();
        await expect.poll(() => patchWrites).toEqual(["PATCH /api/v1/subscriptions/sub-1/sources/repo-1"]);
      } else {
        await expect(toggle).toHaveCount(0);
        expect(patchWrites).toEqual([]);
      }
      expect(repository.unhandled).toEqual([]);
    } finally { await repository.context.close(); }
  }
});

test("creator detail exposes each action only to the permission accepted by its backend", async ({ browser }) => {
  for (const role of [
    { permissions: ["library"], canCurate: false, canManageRepositories: false },
    { permissions: ["library", "curation"], canCurate: true, canManageRepositories: false },
    { permissions: ["library", "subscriptions"], canCurate: false, canManageRepositories: true },
  ]) {
    const writes: string[] = [];
    const opened = await openFixture(
      browser,
      "/admin/creators/creator-1",
      principal(role.permissions),
      (route, path, method) => creatorDetailFixture(route, path, method, writes),
    );
    try {
      await expect(opened.page.getByRole("heading", { level: 1, name: "Creator One" })).toBeVisible();
      await expect(opened.page.getByRole("button", { name: "Star", exact: true })).toHaveCount(role.canCurate ? 1 : 0);
      await expect(opened.page.getByRole("button", { name: "Edit profile", exact: true })).toHaveCount(role.canCurate ? 1 : 0);
      await expect(opened.page.getByRole("button", { name: "Add", exact: true })).toHaveCount(role.canCurate ? 1 : 0);
      await expect(opened.page.getByRole("button", { name: "Reference Alias", exact: true })).toHaveCount(role.canCurate ? 1 : 0);
      await expect(opened.page.getByRole("link", { name: "Open the Pixiv profile for Reference Alias" })).toBeVisible();
      await expect(opened.page.getByRole("link", { name: "Subscription", exact: true })).toHaveCount(role.canManageRepositories ? 1 : 0);

      await opened.page.getByRole("button", { name: /Repositories/ }).click();
      const sync = opened.page.getByRole("button", { name: "Sync now", exact: true });
      await expect(sync).toBeVisible();
      await expect(opened.page.getByRole("button", { name: "Disable", exact: true })).toHaveCount(role.canManageRepositories ? 1 : 0);
      await expect(opened.page.getByRole("link", { name: "Manage subscription", exact: true })).toHaveCount(role.canManageRepositories ? 1 : 0);

      if (role.permissions.length === 1) {
        await sync.click();
        await expect.poll(() => writes).toEqual(["POST /api/v1/repositories/repo-1/sync-now"]);
      } else {
        expect(writes).toEqual([]);
      }
      expect(opened.unhandled).toEqual([]);
    } finally { await opened.context.close(); }
  }
});

test("creator detail link creation sends the creator required by the API contract", async ({ browser }) => {
  let submittedBody: Record<string, unknown> | undefined;
  const writes: string[] = [];
  const opened = await openFixture(
    browser,
    "/admin/creators/creator-1",
    principal(["library", "curation"]),
    async (route, path, method) => {
      if (path === "/api/v1/creators/creator-1/links" && method === "POST") {
        submittedBody = route.request().postDataJSON() as Record<string, unknown>;
        const valid = submittedBody.creator_id === "creator-1";
        await json(route, valid
          ? { id: "new-link", ...submittedBody, confidence: 1, is_verified: false, source: "manual" }
          : { detail: [{ loc: ["body", "creator_id"], msg: "Field required" }] }, valid ? 201 : 422);
        return true;
      }
      return creatorDetailFixture(route, path, method, writes);
    },
  );
  try {
    await expect(opened.page.getByRole("heading", { level: 1, name: "Creator One" })).toBeVisible();
    await opened.page.getByRole("button", { name: /^Links/ }).click();
    await opened.page.getByRole("button", { name: "Add link", exact: true }).click();
    const dialog = opened.page.getByRole("dialog");
    await dialog.locator("input").fill("https://new.example/profile");
    await dialog.getByRole("button", { name: "Add", exact: true }).click();
    await expect.poll(() => submittedBody).toEqual({
      creator_id: "creator-1",
      url: "https://new.example/profile",
      link_type: "website",
    });
    await expect(dialog).toBeHidden();
    expect(opened.unhandled).toEqual([]);
  } finally { await opened.context.close(); }
});

test("Gitllery verify is admin-only while system readers retain the settings page", async ({ browser }) => {
  for (const role of [
    { me: principal(["system"]), canVerify: false },
    { me: principal([], true), canVerify: true },
  ]) {
    const verifyWrites: string[] = [];
    const opened = await openFixture(browser, "/admin/settings/gitllery", role.me, async (route, path, method) => {
      if (path === "/api/v1/admin/gitllery/settings") { await json(route, gitllerySettings); return true; }
      if (path === "/api/v1/curation/gitllery/verify") { verifyWrites.push(`${method} ${path}`); await json(route, { status: "enqueued", job_id: "verify-1" }, 202); return true; }
      return false;
    });
    try {
      await expect(opened.page.getByRole("heading", { level: 1, name: "Gitllery Settings" })).toBeVisible();
      const verify = opened.page.getByRole("button", { name: "Queue verify", exact: true });
      if (role.canVerify) {
        await expect(verify).toBeVisible();
        await verify.click();
        await expect.poll(() => verifyWrites).toEqual(["POST /api/v1/curation/gitllery/verify"]);
      } else {
        await expect(verify).toHaveCount(0);
        expect(verifyWrites).toEqual([]);
      }
      expect(opened.unhandled).toEqual([]);
    } finally { await opened.context.close(); }
  }
});

test("tasks-only sidebar keeps the failed and stale jobs badge without system access", async ({ browser }) => {
  const requested: string[] = [];
  const opened = await openFixture(browser, "/admin/profile", principal(["tasks"]), async (route, path) => {
    requested.push(path);
    if (path === "/api/v1/tasks") {
      await json(route, { total: 3, items: [] });
      return true;
    }
    return false;
  });
  try {
    await expect(opened.page.locator('#admin-sidebar a[href="/admin/jobs"]')).toContainText("3");
    expect(requested).toContain("/api/v1/tasks");
    expect(requested).not.toContain("/api/v1/system/workbench");
    expect(opened.unhandled).toEqual([]);
  } finally { await opened.context.close(); }
});

test("scheduler sidebar badge uses the full actionable attention count", async ({ browser }) => {
  const requested: string[] = [];
  const opened = await openFixture(browser, "/admin/profile", principal(["system"]), async (route, path) => {
    requested.push(route.request().url());
    if (path === "/api/v1/system/scheduler-decisions") {
      await json(route, { total: 4, items: [], summary: { blocked_count: 4 } });
      return true;
    }
    return false;
  });
  try {
    await expect(opened.page.locator('#admin-sidebar a[href="/admin/scheduler"]')).toContainText("4");
    expect(requested.some((url) => url.includes("/api/v1/system/scheduler-decisions") && url.includes("view=attention"))).toBe(true);
    expect(opened.unhandled).toEqual([]);
  } finally { await opened.context.close(); }
});
