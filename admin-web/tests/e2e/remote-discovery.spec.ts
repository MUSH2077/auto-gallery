import { expect, test, type BrowserContext, type Page, type Route } from "@playwright/test";

const host = new URL(process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000").hostname;
const now = "2026-08-28T08:00:00Z";

const me = {
  id: 41,
  username: "discovery-owner",
  display_name: "Discovery Owner",
  is_admin: false,
  is_active: true,
  permissions: ["subscriptions"],
  modules: { subscriptions: true },
  preferences: {},
  nsfw_visible: true,
  upload_quota_bytes: null,
  upload_used_bytes: 0,
  must_change_password: false,
};

function account(overrides: Record<string, unknown> = {}) {
  return {
    id: "acc-pixiv",
    user_id: 41,
    source: "pixiv",
    remote_user_id: "12345",
    remote_username: "pixiv_artist",
    auth_method: "refresh_token",
    scopes: [],
    collection_selectors: [{ restrict: "public" }],
    is_enabled: true,
    auth_status: "healthy",
    auth_error_reason: null,
    last_authenticated_at: now,
    last_scan_started_at: null,
    last_scan_completed_at: now,
    next_scan_at: "2026-08-29T08:00:00Z",
    scan_interval_hours: 24,
    auto_import_enabled: false,
    auto_import_min_confidence: "high",
    auto_import_limit: 25,
    has_credentials: true,
    credential_mask: { refresh_token: "••••" },
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

function candidate(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    remote_account_id: "acc-pixiv",
    user_id: 41,
    source_creator_id: `remote-${id}`,
    remote_url: `https://www.pixiv.net/users/${id}`,
    display_name: `Artist ${id}`,
    metadata: {
      username: `artist_${id}`,
      profile_image_urls: { medium: "https://images.example/avatar.png" },
      local_creator_ids: [],
    },
    confidence: "high",
    confidence_reasons: ["pixiv_illustration_preview"],
    state: "pending",
    subscription_id: null,
    user_subscription_id: null,
    dismissed_at: null,
    imported_at: null,
    last_seen_at: now,
    is_following: true,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

type FixtureOptions = {
  accounts?: Record<string, unknown>[];
  candidates?: Record<string, unknown>[];
  candidatesStatus?: number;
  onMutation?: (path: string, body: Record<string, unknown>) => void;
};

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installFixtures(context: BrowserContext, options: FixtureOptions = {}) {
  let accounts = [...(options.accounts || [])];
  let candidates = [...(options.candidates || [])];
  await context.addCookies([{ name: "ag_token", value: "fixture-token", domain: host, path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture-token");
    localStorage.setItem("auto-gallery-lang", "en");
    localStorage.setItem("auto-gallery-theme", "light");
  });
  await context.route("https://images.example/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#dbeafe"/></svg>',
    });
  });
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const body = request.postDataJSON?.() as Record<string, unknown> | null;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/sources") {
      return json(route, { sources: [
        { source_name: "pixiv", display_name: "Pixiv", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["refresh_token"], supports_collection_selectors: true } },
        { source_name: "x", display_name: "X", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["oauth2", "cookie"], supports_collection_selectors: true } },
        { source_name: "bilibili", display_name: "Bilibili", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["sessdata"], supports_collection_selectors: true } },
      ] });
    }
    if (path === "/api/v1/remote-accounts" && request.method() === "GET") return json(route, accounts);
    if (path === "/api/v1/remote-accounts" && request.method() === "POST") {
      options.onMutation?.(path, body || {});
      const created = account({
        id: `acc-${String(body?.source)}`,
        source: body?.source,
        auth_method: body?.auth_method,
        remote_username: null,
        remote_user_id: null,
        auth_status: "untested",
        last_authenticated_at: null,
        collection_selectors: body?.collection_selectors || [],
        scan_interval_hours: body?.scan_interval_hours || 24,
        auto_import_enabled: body?.auto_import_enabled || false,
        auto_import_min_confidence: body?.auto_import_min_confidence || "high",
        auto_import_limit: body?.auto_import_limit || 25,
        credential_mask: { refresh_token: "••••" },
      });
      accounts = [...accounts.filter((item) => item.source !== body?.source), created];
      return json(route, created, 201);
    }
    if (path === "/api/v1/remote-accounts/x/oauth/callback") {
      const connected = account({
        id: "acc-x",
        source: "x",
        remote_user_id: "x-user-id",
        remote_username: "x_artist",
        auth_method: "oauth2",
        scopes: ["users.read", "follows.read", "list.read", "offline.access"],
        credential_mask: { access_token: "••••", refresh_token: "••••" },
      });
      accounts = [...accounts.filter((item) => item.source !== "x"), connected];
      return json(route, connected);
    }
    const accountMatch = path.match(/^\/api\/v1\/remote-accounts\/([^/]+)$/);
    if (accountMatch && request.method() === "PATCH") {
      options.onMutation?.(path, body || {});
      const existing = accounts.find((item) => item.id === accountMatch[1]) || account();
      const updated = { ...existing, ...(body || {}), updated_at: now };
      accounts = accounts.map((item) => item.id === accountMatch[1] ? updated : item);
      return json(route, updated);
    }
    if (accountMatch && request.method() === "DELETE") {
      accounts = accounts.filter((item) => item.id !== accountMatch[1]);
      return route.fulfill({ status: 204 });
    }
    if (/\/api\/v1\/remote-accounts\/[^/]+\/test$/.test(path)) {
      const id = path.split("/").at(-2);
      const existing = accounts.find((item) => item.id === id) || account();
      const healthy = { ...existing, auth_status: "healthy", last_authenticated_at: now };
      accounts = accounts.map((item) => item.id === id ? healthy : item);
      return json(route, healthy);
    }
    if (/\/api\/v1\/remote-accounts\/[^/]+\/collections$/.test(path)) {
      return json(route, [
        { id: "public", name: "Public follows", selector: { restrict: "public" } },
        { id: "private", name: "Private follows", selector: { restrict: "private" } },
      ]);
    }
    if (path === "/api/v1/discovery/scans" && request.method() === "POST") {
      options.onMutation?.(path, body || {});
      return json(route, { id: "scan-task", kind: "discovery", operation_type: "remote-discovery-scan", status: "enqueued", progress_data: { stage: "queued", current: 0, total: 1 }, created_at: now }, 201);
    }
    if (path === "/api/v1/discovery/scans" && request.method() === "GET") {
      return json(route, { total: 1, items: [{ id: "scan-task", kind: "discovery", operation_type: "remote-discovery-scan", status: "complete", progress_data: { stage: "complete", current: 1, total: 1 }, created_at: now, updated_at: now }] });
    }
    if (path === "/api/v1/discovery/candidates" && request.method() === "GET") {
      if (options.candidatesStatus) return json(route, { detail: "not available" }, options.candidatesStatus);
      const state = url.searchParams.get("state");
      const confidence = url.searchParams.get("confidence");
      const following = url.searchParams.get("is_following");
      const accountId = url.searchParams.get("remote_account_id");
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 25);
      const filtered = candidates.filter((item) =>
        (!state || item.state === state)
        && (!confidence || item.confidence === confidence)
        && (!accountId || item.remote_account_id === accountId)
        && (following === null || String(item.is_following) === following)
      );
      return json(route, { total: filtered.length, items: filtered.slice(offset, offset + limit) });
    }
    if (path === "/api/v1/discovery/candidates/batch-actions") {
      options.onMutation?.(path, body || {});
      const ids = new Set((body?.ids as string[]) || []);
      const state = body?.action === "dismiss" ? "dismissed" : body?.action === "restore" ? "pending" : "imported";
      candidates = candidates.map((item) => ids.has(String(item.id)) ? { ...item, state } : item);
      return json(route, { items: candidates.filter((item) => ids.has(String(item.id))), immediate_sync: body?.immediate_sync || false, sync_results: [] });
    }
    const resolveMatch = path.match(/^\/api\/v1\/discovery\/candidates\/([^/]+)\/resolve$/);
    if (resolveMatch) {
      options.onMutation?.(path, body || {});
      const existing = candidates.find((item) => item.id === resolveMatch[1]) || candidate(resolveMatch[1]);
      const resolved = { ...existing, state: "imported", metadata: { ...(existing.metadata as object), identity_conflict: false } };
      candidates = candidates.map((item) => item.id === resolveMatch[1] ? resolved : item);
      return json(route, { candidate: resolved, immediate_sync: body?.immediate_sync || false, sync_result: null });
    }
    if (path === "/api/v1/creators") {
      return json(route, { total: 2, items: [
        { id: "creator-one", name: "Creator One", display_name: "Creator One", is_active: true, is_favorite: false, created_at: now, updated_at: now },
        { id: "creator-two", name: "Creator Two", display_name: "Creator Two", is_active: true, is_favorite: false, created_at: now, updated_at: now },
      ] });
    }
    return json(route, { detail: `Unhandled fixture: ${path}` }, 404);
  });
}

test("connects Pixiv, saves collections and never re-renders the refresh token", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installFixtures(context, { onMutation: (path, body) => mutations.push({ path, body }) });
  await page.goto("/admin/discovery");

  await expect(page.getByRole("heading", { name: "Remote discovery" })).toBeVisible();
  await page.getByRole("button", { name: "Connect Pixiv" }).click();
  await page.getByLabel("Pixiv refresh token").fill("fixture-refresh-secret");
  await page.getByRole("button", { name: "Connect account" }).click();
  await expect(page.getByText("Account connected")).toBeVisible();
  expect(mutations[0].body).toMatchObject({
    source: "pixiv",
    auth_method: "refresh_token",
    credentials: { refresh_token: "fixture-refresh-secret" },
  });
  await expect(page.getByText("fixture-refresh-secret")).toHaveCount(0);

  await page.getByRole("button", { name: "Configure Pixiv" }).click();
  await page.getByRole("checkbox", { name: "Public follows" }).check();
  await page.getByRole("checkbox", { name: "Private follows" }).check();
  await page.getByLabel("Scan interval (hours)").fill("6");
  await page.getByLabel("Automatic import").check();
  await page.getByLabel("Minimum confidence").selectOption("medium");
  await page.getByLabel("Maximum imports per scan").fill("50");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Settings saved")).toBeVisible();
  expect(mutations.at(-1)?.body).toMatchObject({
    scan_interval_hours: 6,
    auto_import_enabled: true,
    auto_import_min_confidence: "medium",
    auto_import_limit: 50,
    collection_selectors: [{ restrict: "public" }, { restrict: "private" }],
  });

  await page.getByRole("button", { name: "Reconnect Pixiv" }).click();
  await expect(page.getByLabel("Pixiv refresh token")).toHaveValue("");
});

test("scans, filters, imports without immediate sync, dismisses and restores visible candidates", async ({ context, page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installFixtures(context, {
    accounts: [account()],
    candidates: [
      candidate("one"),
      candidate("two", { confidence: "medium", confidence_reasons: ["single_creator_evidence"] }),
      candidate("three", { is_following: false }),
    ],
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  await expect(page.getByRole("heading", { name: "Candidate workbench" })).toBeVisible();
  await page.screenshot({ path: "/tmp/auto-gallery-remote-discovery-desktop.png", fullPage: true });

  await page.getByRole("button", { name: "Scan Pixiv" }).click();
  await expect(page.getByText("Scan complete")).toBeVisible();

  await page.getByLabel("Confidence filter").selectOption("medium");
  await expect(page.getByRole("table").getByText("Artist two")).toBeVisible();
  await expect(page.getByRole("table").getByText("Artist one")).toHaveCount(0);
  await page.getByLabel("Confidence filter").selectOption("");

  await page.getByRole("table").getByRole("checkbox", { name: "Select Artist one" }).check();
  await page.getByRole("button", { name: "Import selected" }).click();
  await expect(page.getByLabel("Sync immediately")).not.toBeChecked();
  await page.getByRole("button", { name: "Confirm import" }).click();
  expect(mutations.find((item) => item.path.endsWith("batch-actions") && item.body.action === "import")?.body)
    .toMatchObject({ action: "import", immediate_sync: false });

  await page.getByRole("table").getByRole("button", { name: "Ignore Artist two" }).click();
  await page.getByLabel("Status filter").selectOption("dismissed");
  await expect(page.getByRole("table").getByText("Artist two")).toBeVisible();
  await page.getByRole("table").getByRole("button", { name: "Restore Artist two" }).click();
  expect(mutations.some((item) => item.body.action === "dismiss")).toBe(true);
  expect(mutations.some((item) => item.body.action === "restore")).toBe(true);
  await expect(page.getByRole("dialog", { name: "Build Error" })).toHaveCount(0);
  expect(consoleErrors).toEqual([]);
});

test("resolves a conflict by attaching an existing creator and by creating a new one", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installFixtures(context, {
    accounts: [account()],
    candidates: [candidate("conflict", {
      state: "conflict",
      confidence_reasons: ["multiple_local_identity_matches"],
      metadata: { username: "conflicted_artist", identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] },
    })],
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  await page.getByRole("button", { name: "Resolve Artist conflict" }).click();
  await page.getByRole("radio", { name: "Attach existing creator" }).check();
  await page.getByLabel("Creator lookup").fill("Creator");
  await page.getByRole("button", { name: "Choose Creator One" }).click();
  await page.getByRole("button", { name: "Resolve conflict" }).click();
  expect(mutations.find((item) => item.path.endsWith("/resolve"))?.body).toMatchObject({
    creator_id: "creator-one",
    immediate_sync: false,
  });

  await installFixtures(context, {
    accounts: [account()],
    candidates: [candidate("conflict-new", { state: "conflict", metadata: { identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] } })],
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.reload();
  await page.getByRole("button", { name: "Resolve Artist conflict-new" }).click();
  await page.getByRole("radio", { name: "Create new creator" }).check();
  await page.getByLabel("New creator name").fill("Resolved Artist");
  await page.getByRole("button", { name: "Resolve conflict" }).click();
  expect(mutations.at(-1)?.body).toMatchObject({ creator_name: "Resolved Artist", immediate_sync: false });
});

test("paginates candidates without carrying selection across pages", async ({ context, page }) => {
  await installFixtures(context, {
    accounts: [account()],
    candidates: Array.from({ length: 27 }, (_, index) => candidate(`page-${index}`)),
  });
  await page.goto("/admin/discovery");
  const table = page.getByRole("table");
  await table.getByRole("checkbox", { name: "Select Artist page-0" }).check();
  await page.getByRole("navigation", { name: "Pagination" }).getByRole("button", { name: "Next" }).click();
  await expect(table.getByText("Artist page-25")).toBeVisible();
  await expect(table.getByRole("checkbox", { name: "Select Artist page-25" })).not.toBeChecked();
  await expect(page.getByText("1 selected")).toHaveCount(0);
});

test("finishes an X OAuth callback and removes authorization parameters from the page URL", async ({ context, page }) => {
  await installFixtures(context);
  await page.goto("/admin/discovery?state=fixture-state-value-long-enough&code=fixture-code");
  await expect(page.getByText("X OAuth account connected.")).toBeVisible();
  await expect(page).toHaveURL(/\/admin\/discovery$/);
  await expect(page.getByText("x_artist")).toBeVisible();
});

test("renders a private 403 state without leaking discovery rows", async ({ context, page }) => {
  await installFixtures(context, { accounts: [account()], candidatesStatus: 403 });
  await page.goto("/admin/discovery");
  await expect(page.getByText("You do not have access to this discovery data.")).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
});

test("keeps the account and candidate workbench usable on mobile", async ({ context, page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installFixtures(context, { accounts: [account()], candidates: [candidate("mobile")] });
  await page.goto("/admin/discovery");
  await expect(page.getByRole("heading", { name: "Remote discovery" })).toBeVisible();
  await expect(page.locator("article").filter({ hasText: "Artist mobile" }).getByText("Artist mobile")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: "/tmp/auto-gallery-remote-discovery-mobile.png", fullPage: true });
});
