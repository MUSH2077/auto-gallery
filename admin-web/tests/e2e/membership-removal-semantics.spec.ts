import { expect, test, type Browser, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 90_000 });

const SUBSCRIPTION_ID = "subscription-0001";
const REPOSITORY_ID = "repository-0001";
const NOW = "2026-09-13T00:00:00Z";

const subscription = {
  id: SUBSCRIPTION_ID,
  creator_id: "creator-0001",
  creator_name: "Fixture Creator",
  creator_display_name: "Fixture Creator",
  name: "Fixture Subscription",
  is_active: true,
  sync_enabled: true,
  sync_interval_hours: 6,
  schedule_mode: "inherit",
  scheduled_times: null,
  last_synced_at: null,
  source_count: 1,
  enabled_source_count: 1,
  running_job_count: 0,
  failed_job_count: 0,
  created_at: NOW,
  updated_at: NOW,
};

const source = {
  id: REPOSITORY_ID,
  subscription_id: SUBSCRIPTION_ID,
  source: "pixiv",
  source_display_name: "Pixiv",
  source_creator_id: "42",
  source_url: "https://www.pixiv.net/users/42",
  is_enabled: true,
  auth_healthy: true,
  last_successful_auth: null,
  last_synced_at: null,
  created_at: NOW,
  updated_at: NOW,
};

const repositoryDetail = {
  repository: {
    ...source,
    is_repository: true,
    can_download: true,
    supports_gallerydl: true,
    url_valid: true,
    latest_job: null,
  },
  creator: { id: "creator-0001", name: "Fixture Creator", display_name: "Fixture Creator" },
  subscription,
  provider: {
    source: "pixiv",
    display_name: "Pixiv",
    normalized_url: source.source_url,
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
  recent_works: [],
  work_total: 0,
};

const creator = {
  id: "creator-0001",
  name: "Fixture Creator",
  display_name: "Fixture Creator",
  description: null,
  is_active: true,
  is_favorite: false,
  danbooru_artist_id: null,
  last_synced_at: null,
  repository_count: 1,
  source_count: 1,
  subscription_count: 1,
  thumbnail_url: null,
  created_at: NOW,
  updated_at: NOW,
};

const preview = (entityType: "subscription" | "repository") => ({
  entity_type: entityType,
  entity_ids: [entityType === "subscription" ? SUBSCRIPTION_ID : REPOSITORY_ID],
  mode: "soft",
  can_delete_files: false,
  affected_work_count: 3,
  exclusive_work_count: 0,
  shared_work_count: 3,
  exclusive_asset_count: 0,
  active_task_count: 0,
});

const principal = (isAdmin: boolean) => ({
  id: isAdmin ? 1 : 22,
  username: isAdmin ? "admin" : "member",
  display_name: isAdmin ? "Admin" : "Member",
  is_admin: isAdmin,
  is_active: true,
  permissions: ["library", "subscriptions"],
  modules: { library: "Library", subscriptions: "Subscriptions" },
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
});

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

type Fixture = {
  context: BrowserContext;
  page: Awaited<ReturnType<BrowserContext["newPage"]>>;
  writes: string[];
  unhandled: string[];
};

async function openFixture(browser: Browser, pathname: string, isAdmin: boolean, subscriptionActive = true): Promise<Fixture> {
  const context = await browser.newContext();
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
  const writes: string[] = [];
  const unhandled: string[] = [];
  let sourceRemoved = false;
  let subscriptionRemoved = false;
  const subscriptionFixture = { ...subscription, is_active: subscriptionActive };
  await context.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const observation = `${method} ${path}${url.search}`;

    if (path === "/api/v1/auth/me") return json(route, principal(isAdmin));
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/system/workbench") return json(route, { updated_at: NOW, queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { total: 0, items: [], summary: { blocked_count: 0 } });
    if (path === "/api/v1/operations/overview") return json(route, { summary: { attention: 0 }, items: [] });
    if (path.includes("/notifications")) return json(route, { total: 0, items: [], unread_count: 0 });
    if (path === "/api/v1/download-jobs") return json(route, []);
    if (path === "/api/v1/sources") return json(route, []);
    if (path === "/api/v1/creators") return json(route, []);
    if (path === `/api/v1/creators/${creator.id}`) return json(route, creator);
    if (path === "/api/v1/search/name-anchors") return json(route, { scope: "subscriptions", direction: "asc", total: 1, items: [] });

    if (path === "/api/v1/search" && method === "GET") {
      const items = subscriptionRemoved ? [] : [subscriptionFixture];
      return json(route, {
        query: "",
        canonical_query: "",
        parsed: { raw: "", canonical: "", scope: "subscriptions", targets: ["subscriptions"], tokens: [] },
        groups: { subscriptions: { total: items.length, items } },
        total: items.length,
        results: [], creators: [], tags: [], repositories: [], subscriptions: items,
        execution: { winner: "postgresql", hedged: false, consistency: "authoritative", index_status: "not_needed", elapsed_ms: 1 },
      });
    }
    if (path === "/api/v1/subscriptions/summaries") {
      return json(route, {
        updated_at: NOW,
        items: [{
          subscription_id: SUBSCRIPTION_ID,
          latest_state: { state: "never_synced", status: null },
          active_count: 0,
          attention_count: 0,
          source_count: sourceRemoved ? 0 : 1,
          enabled_source_count: sourceRemoved ? 0 : 1,
          schedule: { configured_mode: "inherit", effective_mode: "interval", sync_interval_hours: 6, inherited: true, due_sources: 0, blocked_sources: 0 },
        }],
      });
    }
    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}` && method === "GET") return json(route, subscriptionFixture);
    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}/sources` && method === "GET") return json(route, sourceRemoved ? [] : [source]);
    if (path === "/api/v1/subscriptions/batch-deletion-preview" && method === "POST") {
      expect(request.postDataJSON()).toEqual({ ids: [SUBSCRIPTION_ID] });
      return json(route, preview("subscription"));
    }
    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}/deletion-preview`) return json(route, preview("subscription"));
    if (path === `/api/v1/repositories/${REPOSITORY_ID}/deletion-preview`) return json(route, preview("repository"));
    if (path === `/api/v1/repositories/${REPOSITORY_ID}` && method === "GET") return json(route, repositoryDetail);
    if (path === `/api/v1/repositories/${REPOSITORY_ID}/tags`) return json(route, { items: [], total: 0 });
    if (path === `/api/v1/repositories/${REPOSITORY_ID}/curation-graph`) return json(route, { repository_id: REPOSITORY_ID, nodes: [], edges: [], total: 0, offset: 0, limit: 100 });

    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}` && method === "DELETE") {
      writes.push(observation);
      subscriptionRemoved = true;
      return json(route, { status: "soft_deleted", mode: "soft", entity_type: "subscription", entity_ids: [SUBSCRIPTION_ID], delete_files: false });
    }
    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}` && method === "PATCH") {
      expect(request.postDataJSON()).toEqual({ is_active: true });
      writes.push(observation);
      return json(route, { ...subscriptionFixture, is_active: true });
    }
    if (path === `/api/v1/subscriptions/${SUBSCRIPTION_ID}/sources/${REPOSITORY_ID}` && method === "DELETE") {
      writes.push(observation);
      sourceRemoved = true;
      return json(route, { status: "soft_deleted", mode: "soft", entity_type: "repository", entity_ids: [REPOSITORY_ID], delete_files: false });
    }
    if (path === `/api/v1/repositories/${REPOSITORY_ID}` && method === "DELETE") {
      writes.push(observation);
      sourceRemoved = true;
      return json(route, { status: "soft_deleted", mode: "soft", entity_type: "repository", entity_ids: [REPOSITORY_ID], delete_files: false });
    }

    unhandled.push(observation);
    return json(route, { detail: `Unhandled fixture request: ${observation}` }, 501);
  });
  const page = await context.newPage();
  await page.goto(pathname);
  return { context, page, writes, unhandled };
}

test("inactive admin membership keeps its Restore action", async ({ browser }) => {
  const fixture = await openFixture(browser, "/admin/subscriptions", true, false);
  try {
    await expect(fixture.page.getByText("Fixture Subscription", { exact: true })).toBeVisible();
    await fixture.page.getByRole("button", { name: "More actions", exact: true }).click();
    const restore = fixture.page.getByRole("menuitem", { name: "Restore", exact: true });
    await expect(restore).toBeVisible();
    await expect(fixture.page.getByRole("menuitem", { name: "Remove from my subscriptions", exact: true })).toHaveCount(0);
    await restore.click();
    await expect.poll(() => fixture.writes).toEqual([`PATCH /api/v1/subscriptions/${SUBSCRIPTION_ID}`]);
    expect(fixture.unhandled).toEqual([]);
  } finally {
    await fixture.context.close();
  }
});

async function assertMembershipDialog(page: Fixture["page"], title: string, message: string) {
  const dialog = page.getByRole("dialog", { name: title });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(message);
  await expect(dialog.getByText("Related works", { exact: true }).locator("..")).toContainText("3");
  await expect(dialog.getByText("Shared works", { exact: true }).locator("..")).toContainText("3");
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  await expect(dialog.getByRole("checkbox")).toHaveCount(0);
  await expect(dialog).not.toContainText(/permanent|disable|archive/i);
  return dialog;
}

for (const role of [{ name: "admin", isAdmin: true }, { name: "member", isAdmin: false }]) {
  test(`${role.name} subscription list describes single and batch actions as membership removal`, async ({ browser }) => {
    const fixture = await openFixture(browser, "/admin/subscriptions", role.isAdmin);
    try {
      await expect(fixture.page.getByText("Fixture Subscription", { exact: true })).toBeVisible();
      await fixture.page.getByRole("checkbox", { name: "Select Fixture Subscription" }).click();
      await fixture.page.getByRole("button", { name: "Remove selected (1)", exact: true }).click();
      let dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my subscriptions",
        "Shared subscription data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
      expect(fixture.writes).toEqual([]);
      await fixture.page.getByRole("button", { name: "Clear", exact: true }).click();

      await fixture.page.getByRole("button", { name: "More actions", exact: true }).click();
      const remove = fixture.page.getByRole("menuitem", { name: "Remove from my subscriptions", exact: true });
      await expect(remove).toBeVisible();
      await remove.click();
      dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my subscriptions",
        "Shared subscription data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
      expect(fixture.writes).toEqual([]);

      await fixture.page.getByRole("button", { name: "More actions", exact: true }).click();
      await fixture.page.getByRole("menuitem", { name: "Remove from my subscriptions", exact: true }).click();
      dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my subscriptions",
        "Shared subscription data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Confirm" }).click();
      await expect.poll(() => fixture.writes).toEqual([`DELETE /api/v1/subscriptions/${SUBSCRIPTION_ID}?delete_files=false`]);
      expect(fixture.unhandled).toEqual([]);
    } finally {
      await fixture.context.close();
    }
  });

  test(`${role.name} subscription detail removes only the selected membership and source binding`, async ({ browser }) => {
    const fixture = await openFixture(browser, `/admin/subscriptions/${SUBSCRIPTION_ID}`, role.isAdmin);
    try {
      const removeSubscription = fixture.page.getByRole("button", { name: "Remove from my subscriptions", exact: true });
      await expect(removeSubscription).toBeVisible();
      await removeSubscription.click();
      let dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my subscriptions",
        "Shared subscription data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
      expect(fixture.writes).toEqual([]);

      const removeSource = fixture.page.getByRole("button", { name: "Remove from my sources", exact: true });
      await expect(removeSource).toBeVisible();
      await removeSource.click();
      dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my sources",
        "Shared source data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Confirm" }).click();
      await expect.poll(() => fixture.writes).toEqual([`DELETE /api/v1/subscriptions/${SUBSCRIPTION_ID}/sources/${REPOSITORY_ID}?delete_files=false`]);
      expect(fixture.unhandled).toEqual([]);
    } finally {
      await fixture.context.close();
    }
  });

  test(`${role.name} repository detail removes the private source binding after cancel is harmless`, async ({ browser }) => {
    const fixture = await openFixture(browser, `/admin/subscriptions/repositories/${REPOSITORY_ID}`, role.isAdmin);
    try {
      const removeSource = fixture.page.getByRole("button", { name: "Remove from my sources", exact: true });
      await expect(removeSource).toBeVisible();
      await removeSource.click();
      let dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my sources",
        "Shared source data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
      expect(fixture.writes).toEqual([]);

      await removeSource.click();
      dialog = await assertMembershipDialog(
        fixture.page,
        "Remove from my sources",
        "Shared source data, works, and files will not be deleted.",
      );
      await dialog.getByRole("button", { name: "Confirm" }).click();
      await expect.poll(() => fixture.writes).toEqual([`DELETE /api/v1/repositories/${REPOSITORY_ID}?delete_files=false`]);
      expect(fixture.unhandled).toEqual([]);
    } finally {
      await fixture.context.close();
    }
  });
}
