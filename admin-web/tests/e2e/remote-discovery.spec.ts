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

const meB = {
  ...me,
  id: 52,
  username: "discovery-viewer-b",
  display_name: "Discovery Viewer B",
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
    avatar_url: null,
    recent_works: [],
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
  userBPrivateStatus?: number;
  collectionsFailures?: number;
  collectionsShouldFail?: () => boolean;
  candidateWait?: (url: URL) => Promise<void>;
  oauthCallbackStatus?: number;
  oauthCallbackDelayMs?: number;
  accountConnectStatus?: number;
  accountConnectDelayMs?: number;
  onOAuthCallback?: (request: { method: string; url: string; body: Record<string, unknown> | null }) => void;
  onPrivateRequest?: (user: "a" | "b", path: string) => void;
  onMutation?: (path: string, body: Record<string, unknown>) => void;
  rollout?: Partial<Record<"pixiv" | "x" | "bilibili", { manual_preview: boolean; auto_import: boolean; unavailable_reason: string | null }>>;
};

async function json(route: Route, value: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
}

async function installFixtures(context: BrowserContext, options: FixtureOptions = {}) {
  let accounts = [...(options.accounts || [])];
  let candidates = [...(options.candidates || [])];
  let collectionsAttempts = 0;
  await context.addCookies([{ name: "ag_token", value: "fixture-token-a", domain: host, path: "/" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture-token-a");
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
    const actingUser = request.headers().authorization?.includes("fixture-token-b") ? "b" : "a";
    if (path === "/api/v1/auth/login") return json(route, { access_token: "fixture-token-b" });
    if (path === "/api/v1/auth/me") return json(route, actingUser === "b" ? meB : me);
    if (path === "/api/v1/sources") {
      return json(route, { sources: [
        { source_name: "pixiv", display_name: "Pixiv", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["refresh_token"], supports_collection_selectors: true, remote_discovery_rollout: options.rollout?.pixiv || { manual_preview: true, auto_import: true, unavailable_reason: null } } },
        { source_name: "x", display_name: "X", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["oauth2", "cookie"], supports_collection_selectors: true, remote_discovery_rollout: options.rollout?.x || { manual_preview: true, auto_import: true, unavailable_reason: null } } },
        { source_name: "bilibili", display_name: "Bilibili", capabilities: { can_download: true, can_import_local: false, supports_gallerydl: true, supports_tags: true, is_reference_only: false, supports_remote_discovery: true, discovery_auth_methods: ["sessdata"], supports_collection_selectors: true, remote_discovery_rollout: options.rollout?.bilibili || { manual_preview: true, auto_import: true, unavailable_reason: null } } },
      ] });
    }
    if (path.startsWith("/api/v1/remote-accounts") || path.startsWith("/api/v1/discovery")) {
      options.onPrivateRequest?.(actingUser, path);
      if (actingUser === "b" && options.userBPrivateStatus) {
        return json(route, { detail: "not available" }, options.userBPrivateStatus);
      }
    }
    if (path === "/api/v1/remote-accounts" && request.method() === "GET") return json(route, accounts);
    if (path === "/api/v1/remote-accounts" && request.method() === "POST") {
      if (options.accountConnectDelayMs) await new Promise((resolve) => setTimeout(resolve, options.accountConnectDelayMs));
      if (options.accountConnectStatus) return json(route, { detail: "credential rejected" }, options.accountConnectStatus);
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
      options.onOAuthCallback?.({ method: request.method(), url: request.url(), body });
      if (request.method() !== "POST" || url.search) {
        return json(route, { detail: "OAuth callback must use a query-free POST" }, 405);
      }
      if (options.oauthCallbackDelayMs) await new Promise((resolve) => setTimeout(resolve, options.oauthCallbackDelayMs));
      if (options.oauthCallbackStatus) return json(route, { detail: "oauth failed" }, options.oauthCallbackStatus);
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
      collectionsAttempts += 1;
      if (options.collectionsShouldFail?.() || collectionsAttempts <= (options.collectionsFailures || 0)) {
        return json(route, { detail: "collections unavailable" }, 502);
      }
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
      await options.candidateWait?.(url);
      if (options.candidatesStatus) return json(route, { detail: "not available" }, options.candidatesStatus);
      const state = url.searchParams.get("state");
      const confidence = url.searchParams.get("confidence");
      const following = url.searchParams.get("is_following");
      const localMatch = url.searchParams.get("local_match");
      const accountId = url.searchParams.get("remote_account_id");
      const offset = Number(url.searchParams.get("offset") || 0);
      const limit = Number(url.searchParams.get("limit") || 25);
      const filtered = candidates.filter((item) =>
        (!state || item.state === state)
        && (!confidence || item.confidence === confidence)
        && (!accountId || item.remote_account_id === accountId)
        && (following === null || String(item.is_following) === following)
        && (localMatch === null || String(
          Boolean(item.subscription_id)
          || (Array.isArray((item.metadata as { local_creator_ids?: unknown } | undefined)?.local_creator_ids)
            && ((item.metadata as { local_creator_ids: unknown[] }).local_creator_ids.length > 0)),
        ) === localMatch)
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

async function mutationCacheText(page: Page) {
  return page.evaluate(() => {
    for (const element of Array.from(document.querySelectorAll("*"))) {
      const fiberKey = Object.keys(element).find((key) => key.startsWith("__reactFiber$"));
      if (!fiberKey) continue;
      let fiber = (element as unknown as Record<string, unknown>)[fiberKey] as {
        return?: unknown;
        memoizedProps?: { client?: { getMutationCache?: () => { getAll: () => Array<{ state: { variables?: unknown } }> } } };
      } | null;
      while (fiber) {
        const client = fiber.memoizedProps?.client;
        if (client?.getMutationCache) {
          return JSON.stringify(client.getMutationCache().getAll().map((mutation) => mutation.state.variables ?? null));
        }
        fiber = fiber.return as typeof fiber;
      }
    }
    throw new Error("QueryClient not found in React tree");
  });
}

async function privateQueryCacheText(page: Page) {
  return page.evaluate(() => {
    for (const element of Array.from(document.querySelectorAll("*"))) {
      const fiberKey = Object.keys(element).find((key) => key.startsWith("__reactFiber$"));
      if (!fiberKey) continue;
      let fiber = (element as unknown as Record<string, unknown>)[fiberKey] as {
        return?: unknown;
        memoizedProps?: { client?: { getQueryCache?: () => { getAll: () => Array<{ queryKey: unknown; state: { data?: unknown } }> } } };
      } | null;
      while (fiber) {
        const client = fiber.memoizedProps?.client;
        if (client?.getQueryCache) {
          const queries = client.getQueryCache().getAll()
            .filter((query) => Array.isArray(query.queryKey) && query.queryKey[0] === "remote-discovery-private")
            .map((query) => ({ queryKey: query.queryKey, data: query.state.data }));
          return JSON.stringify(queries);
        }
        fiber = fiber.return as typeof fiber;
      }
    }
    throw new Error("QueryClient not found in React tree");
  });
}

async function privateMutationOptions(page: Page) {
  return page.evaluate(() => {
    for (const element of Array.from(document.querySelectorAll("*"))) {
      const fiberKey = Object.keys(element).find((key) => key.startsWith("__reactFiber$"));
      if (!fiberKey) continue;
      let fiber = (element as unknown as Record<string, unknown>)[fiberKey] as {
        return?: unknown;
        memoizedProps?: { client?: { getMutationCache?: () => { getAll: () => Array<{ options: { mutationKey?: unknown; mutationFn?: unknown }; state: { status: string } }> } } };
      } | null;
      while (fiber) {
        const client = fiber.memoizedProps?.client;
        if (client?.getMutationCache) {
          return client.getMutationCache().getAll()
            .filter((mutation) => Array.isArray(mutation.options.mutationKey) && mutation.options.mutationKey[0] === "remote-discovery-private")
            .map((mutation) => ({
              mutationKey: mutation.options.mutationKey,
              status: mutation.state.status,
              retainsMutationFn: typeof mutation.options.mutationFn === "function",
            }));
        }
        fiber = fiber.return as typeof fiber;
      }
    }
    throw new Error("QueryClient not found in React tree");
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

test("does not retain a failed credential submission function in MutationCache", async ({ context, page }) => {
  const credentialCanary = "failed-refresh-token-canary";
  await installFixtures(context, { accountConnectStatus: 400 });
  await page.goto("/admin/discovery");
  await page.getByRole("button", { name: "Connect Pixiv" }).click();
  await page.getByLabel("Pixiv refresh token").fill(credentialCanary);
  await page.getByRole("button", { name: "Connect account" }).click();
  await expect(page.getByText("Could not connect the account. Check the credentials and try again.")).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();

  expect(await privateMutationOptions(page)).not.toEqual(expect.arrayContaining([
    expect.objectContaining({ mutationKey: expect.arrayContaining(["account-connect"]), retainsMutationFn: true }),
  ]));
  expect(await mutationCacheText(page)).not.toContain(credentialCanary);
  await expect(page.getByText(credentialCanary)).toHaveCount(0);
});

test("cancels a pending credential submission without retaining its function in MutationCache", async ({ context, page }) => {
  const credentialCanary = "pending-refresh-token-canary";
  await installFixtures(context, { accountConnectDelayMs: 2000 });
  await page.goto("/admin/discovery");
  await page.getByRole("button", { name: "Connect Pixiv" }).click();
  await page.getByLabel("Pixiv refresh token").fill(credentialCanary);
  await page.getByRole("button", { name: "Connect account" }).click();
  await expect(page.getByRole("button", { name: "Connect account" })).toBeDisabled();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();

  expect(await privateMutationOptions(page)).not.toEqual(expect.arrayContaining([
    expect.objectContaining({ mutationKey: expect.arrayContaining(["account-connect"]), retainsMutationFn: true }),
  ]));
  expect(await mutationCacheText(page)).not.toContain(credentialCanary);
  await expect(page.getByText(credentialCanary)).toHaveCount(0);
});

test("never clears selectors when collection enumeration fails, then saves after retry", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  let collectionsAvailable = false;
  await installFixtures(context, {
    accounts: [account({ collection_selectors: [{ restrict: "public" }] })],
    collectionsShouldFail: () => !collectionsAvailable,
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  await page.getByRole("button", { name: "Configure Pixiv" }).click();

  await expect(page.getByText("Could not load follow collections.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save settings" })).toBeDisabled();
  expect(mutations.filter((mutation) => mutation.path.includes("remote-accounts/acc-pixiv"))).toEqual([]);

  collectionsAvailable = true;
  await page.getByRole("button", { name: "Retry" }).click();
  await page.getByRole("checkbox", { name: "Private follows" }).check();
  await page.getByRole("button", { name: "Save settings" }).click();
  expect(mutations.at(-1)?.body).toMatchObject({
    collection_selectors: [{ restrict: "public" }, { restrict: "private" }],
  });
});

test("scans, filters, imports without immediate sync, dismisses and restores visible candidates", async ({ context, page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      consoleErrors.push(message.text());
    }
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

test("dismisses conflict candidates individually and through visible batch selection", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installFixtures(context, {
    accounts: [account()],
    candidates: [
      candidate("conflict-one", { state: "conflict", metadata: { identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] } }),
      candidate("conflict-two", { state: "conflict", metadata: { identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] } }),
      candidate("conflict-three", { state: "conflict", metadata: { identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] } }),
    ],
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  const table = page.getByRole("table");

  await table.getByRole("button", { name: "Ignore Artist conflict-one" }).click();
  expect(mutations.at(-1)?.body).toMatchObject({ action: "dismiss", ids: ["conflict-one"] });

  await table.getByRole("checkbox", { name: "Select Artist conflict-two" }).check();
  await table.getByRole("checkbox", { name: "Select Artist conflict-three" }).check();
  await page.getByRole("button", { name: "Ignore selected" }).click();
  expect(mutations.at(-1)?.body).toMatchObject({ action: "dismiss", ids: ["conflict-two", "conflict-three"] });
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

test("clears selection and makes placeholder rows inert while a server filter changes", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  let releaseMediumResponse!: () => void;
  const mediumResponse = new Promise<void>((resolve) => { releaseMediumResponse = resolve; });
  await installFixtures(context, {
    accounts: [account()],
    candidates: [
      candidate("stale-high"),
      candidate("fresh-medium", { confidence: "medium", confidence_reasons: ["single_creator_evidence"] }),
    ],
    candidateWait: (url) => url.searchParams.get("confidence") === "medium" ? mediumResponse : Promise.resolve(),
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  const table = page.getByRole("table");
  await table.getByRole("checkbox", { name: "Select Artist stale-high" }).check();
  await expect(page.getByText("1 selected")).toBeVisible();

  await page.getByLabel("Confidence filter").selectOption("medium");
  await expect(page.getByText("1 selected")).toHaveCount(0);
  await expect(table).toHaveAttribute("aria-busy", "true");
  await expect(table.getByRole("button", { name: "Import Artist stale-high" })).toBeDisabled();
  expect(mutations.filter((mutation) => mutation.path.endsWith("batch-actions"))).toEqual([]);

  releaseMediumResponse();
  await expect(table.getByText("Artist fresh-medium")).toBeVisible();
  await expect(table.getByText("Artist stale-high")).toHaveCount(0);
  await expect(table).toHaveAttribute("aria-busy", "false");
});

test("filters local matches before server pagination and keeps conflict independent", async ({ context, page }) => {
  const candidates = Array.from({ length: 28 }, (_, index) => candidate(`local-page-${index}`));
  candidates[25] = candidate("local-page-25", { metadata: { username: "matched_page_two", local_creator_ids: ["creator-one"] } });
  candidates[26] = candidate("local-page-26", { subscription_id: "subscription-one" });
  candidates[27] = candidate("local-page-conflict", {
    state: "conflict",
    metadata: { identity_conflict: true, local_creator_ids: ["creator-one", "creator-two"] },
  });
  const candidateRequests: URL[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/discovery/candidates") candidateRequests.push(url);
  });
  await installFixtures(context, { accounts: [account()], candidates });
  await page.goto("/admin/discovery");

  await page.getByRole("table").getByRole("checkbox", { name: "Select Artist local-page-0" }).check();
  await expect(page.getByText("1 selected")).toBeVisible();
  await page.getByLabel("Local match filter").selectOption("matched");
  await expect(page.getByText("1 selected")).toHaveCount(0);
  await expect(page.getByRole("table").getByText("Artist local-page-25")).toBeVisible();
  await expect(page.getByRole("table").getByText("Artist local-page-26")).toBeVisible();
  await expect(page.getByRole("table").getByText("Artist local-page-conflict")).toBeVisible();
  await expect(page.getByText("Total: 3")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Pagination" })).toHaveCount(0);
  expect(candidateRequests.at(-1)?.searchParams.get("local_match")).toBe("true");

  await page.getByLabel("Local match filter").selectOption("unmatched");
  await expect(page.getByRole("table").getByText("Artist local-page-0")).toBeVisible();
  await expect(page.getByRole("table").getByText("Artist local-page-25")).toHaveCount(0);
  await expect(page.getByText("Total: 25")).toBeVisible();
  expect(candidateRequests.at(-1)?.searchParams.get("local_match")).toBe("false");

  await page.getByLabel("Local match filter").selectOption("conflict");
  await expect(page.getByRole("table").getByText("Artist local-page-conflict")).toBeVisible();
  await expect(page.getByText("Total: 1")).toBeVisible();
  expect(candidateRequests.at(-1)?.searchParams.get("state")).toBe("conflict");
  expect(candidateRequests.at(-1)?.searchParams.has("local_match")).toBe(false);
});

for (const callbackStatus of [200, 400]) {
  test(`consumes X OAuth secrets once and leaves no cached canaries after ${callbackStatus}`, async ({ context, page }) => {
    const stateCanary = `state-canary-${callbackStatus}-long-enough`;
    const codeCanary = `code-canary-${callbackStatus}`;
    const consoleText: string[] = [];
    const callbackRequests: Array<{ method: string; url: string; body: Record<string, unknown> | null }> = [];
    const callbackResponses: string[] = [];
    page.on("console", (message) => consoleText.push(message.text()));
    page.on("response", async (response) => {
      if (new URL(response.url()).pathname === "/api/v1/remote-accounts/x/oauth/callback") {
        callbackResponses.push(await response.text());
      }
    });
    await installFixtures(context, {
      oauthCallbackStatus: callbackStatus === 200 ? undefined : callbackStatus,
      oauthCallbackDelayMs: 500,
      onOAuthCallback: (request) => callbackRequests.push(request),
    });
    await page.goto(`/admin/discovery?state=${stateCanary}&code=${codeCanary}`);

    await expect(page).toHaveURL(/\/admin\/discovery$/);
    await expect(page.getByText(callbackStatus === 200 ? "X OAuth account connected." : "X OAuth authorization did not complete.")).toBeVisible();
    expect(await mutationCacheText(page)).not.toContain(stateCanary);
    expect(await mutationCacheText(page)).not.toContain(codeCanary);
    expect(callbackRequests).toEqual([{
      method: "POST",
      url: expect.not.stringContaining("?"),
      body: { state: stateCanary, code: codeCanary },
    }]);
    expect(callbackResponses).toHaveLength(1);
    expect(callbackResponses[0]).not.toContain(stateCanary);
    expect(callbackResponses[0]).not.toContain(codeCanary);
    expect(await page.evaluate(() => typeof window.__consumeAutoGalleryXOAuthCallback)).toBe("undefined");
    await expect.poll(() => page.evaluate(() => JSON.stringify(window.history.state))).not.toContain(stateCanary);
    await expect.poll(() => page.evaluate(() => JSON.stringify(window.history.state))).not.toContain(codeCanary);

    const retained = await page.evaluate(() => JSON.stringify({
      url: window.location.href,
      history: window.history.state,
      localStorage: { ...window.localStorage },
      sessionStorage: { ...window.sessionStorage },
      text: document.body.textContent,
      html: document.body.innerHTML,
    }));
    expect(retained).not.toContain(stateCanary);
    expect(retained).not.toContain(codeCanary);
    expect(consoleText.join("\n")).not.toContain(stateCanary);
    expect(consoleText.join("\n")).not.toContain(codeCanary);
  });
}

test("renders a private 403 state without leaking discovery rows", async ({ context, page }) => {
  await installFixtures(context, { accounts: [account()], candidatesStatus: 403 });
  await page.goto("/admin/discovery");
  await expect(page.getByText("You do not have access to this discovery data.").first()).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
});

test("switches users in one session without flashing or reusing private discovery data", async ({ context, page }) => {
  const privateRequests: Array<{ user: "a" | "b"; path: string }> = [];
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().includes("Failed to load resource")) {
      consoleErrors.push(message.text());
    }
  });
  await installFixtures(context, {
    accounts: [account({ remote_username: "private_account_a" })],
    candidates: [candidate("private-a", { display_name: "Private Artist A" })],
    userBPrivateStatus: 403,
    onPrivateRequest: (user, path) => privateRequests.push({ user, path }),
  });
  await page.goto("/admin/discovery");
  await expect(page.getByText("private_account_a")).toBeVisible();
  await expect(page.getByRole("table").getByText("Private Artist A")).toBeVisible();
  expect(await privateQueryCacheText(page)).toContain("private_account_a");

  await page.getByRole("button", { name: "User menu" }).click();
  await page.getByRole("menuitem", { name: "Sign Out" }).click();
  await expect(page.getByRole("heading", { name: "auto-gallery" })).toBeVisible();
  await page.getByLabel("Username").fill("discovery-viewer-b");
  await page.getByLabel("Password").fill("fixture-password");
  await page.getByRole("button", { name: "Sign In" }).click();
  await expect(page.getByRole("button", { name: "User menu" })).toHaveAttribute("title", "Discovery Viewer B");

  await page.evaluate(() => {
    const target = window as typeof window & { __privateDiscoveryLeaks?: string[] };
    target.__privateDiscoveryLeaks = [];
    new MutationObserver(() => {
      const isUserB = document.querySelector('button[aria-label="User menu"]')?.getAttribute("title") === "Discovery Viewer B";
      const body = document.body.textContent || "";
      if (isUserB && (body.includes("private_account_a") || body.includes("Private Artist A"))) {
        target.__privateDiscoveryLeaks?.push(body);
      }
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  await page.getByRole("link", { name: "Remote Discovery" }).click();

  await expect(page.getByText("You do not have access to this discovery data.").first()).toBeVisible();
  await expect(page.getByText("private_account_a")).toHaveCount(0);
  await expect(page.getByText("Private Artist A")).toHaveCount(0);
  expect(await privateQueryCacheText(page)).not.toContain("private_account_a");
  expect(await privateQueryCacheText(page)).not.toContain("Private Artist A");
  await expect.poll(() => privateRequests.filter((request) => request.user === "b").map((request) => request.path)).toEqual(expect.arrayContaining([
    "/api/v1/remote-accounts",
    "/api/v1/discovery/candidates",
  ]));
  expect(await page.evaluate(() => (window as typeof window & { __privateDiscoveryLeaks?: string[] }).__privateDiscoveryLeaks)).toEqual([]);
  expect(consoleErrors).toEqual([]);
});

test("backend rollout disables provider execution but keeps cleanup actions", async ({ context, page }) => {
  await installFixtures(context, {
    accounts: [account()],
    candidates: [candidate("rollout")],
    rollout: {
      pixiv: {
        manual_preview: false,
        auto_import: false,
        unavailable_reason: "pixiv_preview_disabled",
      },
    },
  });
  await page.goto("/admin/discovery");

  const pixivCard = page.locator("article").filter({ has: page.getByRole("heading", { name: "Pixiv" }) }).first();
  await expect(pixivCard.getByRole("button", { name: /Scan Pixiv/i })).toBeDisabled();
  await expect(pixivCard.getByRole("button", { name: /Configure Pixiv/i })).toBeDisabled();
  await expect(pixivCard.getByRole("button", { name: /Delete Pixiv/i })).toBeEnabled();

  await expect(page.getByRole("button", { name: /Import Artist rollout/i }).first()).toBeDisabled();
  await expect(page.getByRole("button", { name: /Ignore Artist rollout/i }).first()).toBeEnabled();
});

test("manual preview rollout hides disabled provider cards at desktop and mobile", async ({ context, page }) => {
  const providerRequests: string[] = [];
  await context.route(/https:\/\/(?:www\.pixiv\.net|api\.x\.com|api\.bilibili\.com)\//, async (route) => {
    providerRequests.push(route.request().url());
    await route.abort();
  });
  await installFixtures(context, {
    accounts: [account()],
    rollout: {
      pixiv: { manual_preview: true, auto_import: false, unavailable_reason: "auto_import_disabled" },
      x: { manual_preview: false, auto_import: false, unavailable_reason: "manual_preview_disabled" },
      bilibili: { manual_preview: false, auto_import: false, unavailable_reason: "manual_preview_disabled" },
    },
  });

  for (const viewport of [{ width: 1440, height: 960 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.goto("/admin/discovery");
    await expect(page.getByRole("heading", { name: "Pixiv" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "X", exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Bilibili" })).toHaveCount(0);
  }
  expect(providerRequests).toEqual([]);
});

test("candidate avatar is requested once across responsive layouts", async ({ context, page }) => {
  const avatarRequests: string[] = [];
  await installFixtures(context, { accounts: [account()], candidates: [candidate("avatar")] });
  await context.route("https://images.example/avatar.png", async (route) => {
    avatarRequests.push(route.request().url());
    await route.fulfill({ status: 404, contentType: "image/png", body: "" });
  });

  await page.goto("/admin/discovery");
  await expect(page.getByText("Artist avatar", { exact: true }).first()).toBeVisible();
  await expect(page.locator('img[alt="Avatar for Artist avatar"]:visible')).toHaveCount(0);
  expect(avatarRequests).toEqual(["https://images.example/avatar.png"]);
});

test("browses, reveals and imports paginated Pixiv works without losing workbench state", async ({ context, page }) => {
  test.setTimeout(60_000);
  const detailCandidate = candidate("detail", {
    avatar_url: "https://images.example/avatar-signed.svg",
    recent_works: [
      {
        source_work_id: "work-manga",
        title: "Two page manga",
        work_url: "https://www.pixiv.net/artworks/100",
        created_at: now,
        work_type: "manga",
        page_count: 2,
        x_restrict: 0,
        thumbnail_url: "https://images.example/manga-thumb.svg",
      },
      {
        source_work_id: "work-sensitive",
        title: "Sensitive Ugoira",
        work_url: "https://www.pixiv.net/artworks/101",
        created_at: now,
        work_type: "ugoira",
        page_count: 1,
        x_restrict: 1,
        thumbnail_url: "https://images.example/sensitive-thumb.svg",
      },
    ],
  });
  await installFixtures(context, {
    accounts: [account()],
    candidates: [
      ...Array.from({ length: 25 }, (_, index) => candidate(`detail-page-${index}`)),
      detailCandidate,
    ],
  });

  let detailAvailable = false;
  const worksCursors: string[] = [];
  const imports: Record<string, unknown>[] = [];
  const work = (
    sourceWorkId: string,
    title: string,
    overrides: Record<string, unknown> = {},
  ) => ({
    source_work_id: sourceWorkId,
    source_creator_id: "remote-detail",
    title,
    work_url: `https://www.pixiv.net/artworks/${sourceWorkId}`,
    created_at: now,
    work_type: "illust",
    page_count: 1,
    x_restrict: 0,
    thumbnail_url: `https://images.example/${sourceWorkId}-thumb.svg`,
    preview_urls: [`https://images.example/${sourceWorkId}-preview.svg`],
    local_work_id: null,
    download_job_id: null,
    import_status: "available",
    work_token: `token-${sourceWorkId}`,
    ...overrides,
  });
  await page.route("**/api/v1/discovery/candidates/detail/remote-detail?*", async (route) => {
    if (!detailAvailable) return json(route, { detail: "temporary detail failure" }, 502);
    return json(route, {
      profile: {
        source: "pixiv",
        source_creator_id: "remote-detail",
        display_name: "Drawer Artist",
        username: "drawer_artist",
        profile_url: "https://www.pixiv.net/users/detail",
        avatar_url: "https://images.example/avatar-detail.svg",
        comment: "Live Pixiv profile",
        work_counts: { total: 3 },
        is_followed: true,
        fetched_at: now,
      },
      works: {
        items: [
          work("100", "Two page manga", {
            work_type: "manga",
            page_count: 2,
            preview_urls: [
              "https://images.example/manga-page-1.svg",
              "https://images.example/manga-page-2.svg",
            ],
          }),
          work("101", "Sensitive Ugoira", {
            work_type: "ugoira",
            x_restrict: 1,
            preview_urls: ["https://images.example/sensitive-preview.svg"],
          }),
        ],
        next_cursor: "cursor-page-2",
      },
    });
  });
  await page.route("**/api/v1/discovery/candidates/detail/remote-works?*", async (route) => {
    worksCursors.push(new URL(route.request().url()).searchParams.get("cursor") || "");
    return json(route, {
      items: [work("102", "Later illustration")],
      next_cursor: null,
    });
  });
  await page.route("**/api/v1/discovery/candidates/detail/remote-work-imports", async (route) => {
    imports.push(route.request().postDataJSON() as Record<string, unknown>);
    return json(route, {
      status: "queued",
      local_work_id: null,
      download_job_id: "download-sensitive",
      candidate: null,
    }, 201);
  });

  await page.goto("/admin/discovery");
  const pagination = page.getByRole("navigation", { name: "Pagination" });
  await pagination.getByRole("button", { name: "Next" }).click();
  const table = page.getByRole("table");
  const row = table.getByRole("row").filter({ hasText: "Artist detail" });
  await expect(row.getByLabel("Recent work snapshots").locator("img")).toHaveCount(2);
  await row.getByRole("checkbox", { name: "Select Artist detail" }).check();
  const trigger = row.getByRole("button", { name: "View remote details for Artist detail" }).last();
  await trigger.focus();
  await trigger.click();

  const drawer = page.getByRole("dialog", { name: "Creator details" });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("Could not load this creator's Pixiv details.")).toBeVisible();
  detailAvailable = true;
  await drawer.getByRole("button", { name: "Retry" }).click();
  await expect(drawer.getByRole("heading", { name: "Drawer Artist" })).toBeVisible();
  await expect(drawer.getByText("@drawer_artist")).toBeVisible();
  await expect(drawer.getByText("Live Pixiv profile")).toBeVisible();
  await expect(drawer.getByText("2 loaded")).toBeVisible();

  const manga = drawer.getByRole("article").filter({ hasText: "Two page manga" });
  await manga.getByRole("button", { name: "Preview Two page manga" }).click();
  const mangaLightbox = page.getByRole("dialog", { name: "Two page manga" });
  await expect(mangaLightbox.getByText("Page 1 of 2")).toBeVisible();
  await mangaLightbox.getByRole("button", { name: "Next" }).click();
  await expect(mangaLightbox.getByText("Page 2 of 2")).toBeVisible();
  await expect(mangaLightbox.getByRole("img", { name: "Page 2 of Two page manga" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(mangaLightbox).toBeHidden();
  await expect(drawer).toBeVisible();

  const sensitive = drawer.getByRole("article").filter({ hasText: "Sensitive Ugoira" });
  await expect(sensitive.getByRole("button", { name: "Import this work" })).toBeDisabled();
  await sensitive.getByRole("button", { name: "Click to reveal R-18 content" }).click();
  await expect(sensitive.getByRole("button", { name: "Preview Sensitive Ugoira" })).toBeVisible();
  await sensitive.getByRole("button", { name: "Import this work" }).click();
  await expect(sensitive.getByText("Queued for download")).toBeVisible();
  expect(imports).toEqual([{ work_token: "token-101", sensitive_content_confirmed: true }]);

  await drawer.getByRole("button", { name: "Load more works" }).click();
  await expect(drawer.getByText("Later illustration")).toBeVisible();
  await expect(drawer.getByText("3 loaded")).toBeVisible();
  expect(worksCursors).toEqual(["cursor-page-2"]);

  await drawer.getByRole("button", { name: "Close dialog" }).click();
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(row.getByRole("checkbox", { name: "Select Artist detail" })).toBeChecked();
  await expect(page.getByText("1 selected")).toBeVisible();
  await expect(row).toBeVisible();
});

test("closed auto gate shows a configured policy as paused and preserves it on save", async ({ context, page }) => {
  const mutations: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installFixtures(context, {
    accounts: [account({
      auto_import_enabled: true,
      auto_import_min_confidence: "medium",
      auto_import_limit: 50,
    })],
    rollout: {
      pixiv: {
        manual_preview: true,
        auto_import: false,
        unavailable_reason: "auto_import_disabled",
      },
    },
    onMutation: (path, body) => mutations.push({ path, body }),
  });
  await page.goto("/admin/discovery");
  const pixivCard = page.locator("article").filter({ has: page.getByRole("heading", { name: "Pixiv" }) }).first();
  await expect(pixivCard.getByText("Auto import configured, paused by rollout")).toBeVisible();
  await page.getByRole("button", { name: "Configure Pixiv" }).click();

  await expect(page.getByLabel("Automatic import")).toBeChecked();
  await expect(page.getByLabel("Automatic import")).toBeDisabled();
  await expect(page.getByText("Automatic import is configured on, but paused by the current rollout gate. The saved setting is preserved.")).toBeVisible();

  await page.getByLabel("Scan interval (hours)").fill("12");
  await page.getByRole("button", { name: "Save settings" }).click();
  const settingsMutation = mutations.find((mutation) => mutation.path === "/api/v1/remote-accounts/acc-pixiv");
  expect(settingsMutation?.body.scan_interval_hours).toBe(12);
  expect(settingsMutation?.body).not.toHaveProperty("auto_import_enabled");
  expect(settingsMutation?.body).not.toHaveProperty("auto_import_min_confidence");
  expect(settingsMutation?.body).not.toHaveProperty("auto_import_limit");
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
