import { expect, test } from "@playwright/test";
test.describe.configure({ timeout: 60_000 });

for (const terminal of ["cancelled", "stale", "missing"] as const) {
  test(`restored operation stops after ${terminal} without an effect loop`, async ({ context, page }) => {
    const jobId = "55555555-0000-4000-8000-000000000001";
    await context.addCookies([
      { name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" },
      { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" },
    ]);
    await context.addInitScript(({ jobId }) => {
      localStorage.setItem("auto-gallery-lang", "en");
      sessionStorage.setItem("admin_operation_job", JSON.stringify({
        jobId, kind: "admin-clear", title: "Restored operation", startedAt: Date.now(),
      }));
    }, { jobId });
    let statusCalls = 0;
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await context.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      const reply = (body: unknown, status = 200) => route.fulfill({ status, json: body });
      if (path === "/api/v1/auth/me") return reply({
        id: 1, username: "monitor", display_name: "Monitor", is_admin: true,
        is_active: true, permissions: ["tasks", "system"], modules: { tasks: true, system: true },
        preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false,
      });
      if (path === "/api/v1/auth/ws-ticket") return reply({ detail: "No fixture websocket" }, 503);
      if (path === `/api/v1/admin/operations/${jobId}`) {
        statusCalls++;
        if (statusCalls === 1) return reply({ task_id: jobId, status: "running", progress: { phase: "working", current: 1, total: 2 } });
        if (terminal === "missing") return reply({ detail: "Operation not found" }, 404);
        return reply({ task_id: jobId, status: terminal, error: `Operation ${terminal}` });
      }
      if (path === "/api/v1/tasks" || path === "/api/v1/import-jobs") return reply({ total: 0, items: [] });
      if (path === "/api/v1/operations/overview") return reply({ summary: { attention: 0 }, items: [] });
      return reply({});
    });
    await page.goto("/admin/jobs");
    await expect.poll(() => statusCalls, { timeout: 20_000 }).toBeGreaterThanOrEqual(2);
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem("admin_operation_job")), { timeout: 15_000 }).toBeNull();
    const settledCalls = statusCalls;
    // Longer than the active interval: a terminal monitor must remain quiet.
    await page.waitForTimeout(11000);
    expect(statusCalls).toBe(settledCalls);
    expect(errors).toEqual([]);
  });
}

for (const importType of ["pixiv", "url"] as const) {
  for (const transport of ["legacy", "task"] as const) {
    for (const outcome of ["completed", "missing-result", "failed"] as const) {
      test(`${importType} batch ${transport} monitor handles ${outcome}`, async ({ context, page }) => {
        const jobId = "55555555-0000-4000-8000-000000000002";
        const result = { total: 1, imported_count: 1, low_confidence_count: 0, not_found_count: 0, error_count: 0,
          imported: [{ artist_id: 901, artist_name: "artist_tag", pixiv_id: "101", url: "https://www.pixiv.net/users/101", links_imported: 1, sources_created: 1 }],
          low_confidence: [], not_found: [], errors: [] };
        await context.addCookies([
          { name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" },
          { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" },
        ]);
        await context.addInitScript(({ jobId, importType, transport }) => {
          localStorage.setItem("auto-gallery-lang", "en");
          sessionStorage.setItem("danbooru_batch_job", JSON.stringify({ jobId, importType, total: 1,
            taskId: transport === "task" ? jobId : undefined, startedAt: Date.now() }));
        }, { jobId, importType, transport });
        let release!: () => void;
        const held = new Promise<void>(resolve => { release = resolve; });
        let statusCalls = 0;
        let resourceCalls = 0;
        const errors: string[] = [];
        page.on("pageerror", error => errors.push(error.message));
        await context.route("**/api/v1/**", async route => {
          const path = new URL(route.request().url()).pathname;
          const reply = (body: unknown, status = 200) => route.fulfill({ status, json: body });
          if (path === "/api/v1/auth/me") return reply({ id: 1, username: "monitor", display_name: "Monitor", is_admin: true,
            is_active: true, permissions: ["curation", "subscriptions", "tasks", "system"], modules: {},
            preferences: {}, nsfw_visible: true, upload_used_bytes: 0, must_change_password: false });
          if (path === "/api/v1/auth/ws-ticket") return reply({ detail: "No fixture websocket" }, 503);
          if (path === `/api/v1/admin/operations/${jobId}` || path === "/api/v1/reference/danbooru/artist/batch-import/status") {
            statusCalls++;
            await held;
            return reply({ task_id: jobId,
              status: outcome === "failed" ? "failed" : transport === "task" ? "completed" : "complete",
              result: outcome === "completed" ? result : {}, progress: null, error: outcome === "failed" ? "Import failed" : null });
          }
          if (path === "/api/v1/system/workbench") {
            resourceCalls++;
            return reply({ queue: {}, scheduler: {}, storage: {}, health: {}, attention: {}, recent: {} });
          }
          if (path === "/api/v1/tasks" || path === "/api/v1/import-jobs") return reply({ total: 0, items: [] });
          if (path === "/api/v1/operations/overview") return reply({ summary: { attention: 0 }, items: [] });
          return reply({});
        });
        try {
          await page.goto("/admin/upload/danbooru");
          await expect.poll(() => statusCalls).toBeGreaterThan(0);
          await expect.poll(() => resourceCalls).toBeGreaterThan(0);
          const beforeCompletion = resourceCalls;
          release();
          await expect.poll(() => page.evaluate(() => sessionStorage.getItem("danbooru_batch_job")), { timeout: 15_000 }).toBeNull();
          await page.getByRole("button", { name: "Notifications", exact: true }).click();
          if (outcome === "completed") {
            await expect(page.getByText("Done: 1 imported", { exact: true })).toBeVisible();
            await expect.poll(() => resourceCalls).toBeGreaterThan(beforeCompletion);
            await page.getByRole("button", { name: "Notifications", exact: true }).click();
            await page.getByRole("button", { name: "Expand", exact: true }).last().click();
            await expect(page.getByText("1 imported", { exact: true })).toBeVisible();
            await expect(page.getByText(result.imported[0].artist_name, { exact: true })).toBeVisible();
          } else {
            await expect(page.getByText("Job expired or failed", { exact: true })).toBeVisible();
            await expect(page.getByText("Done: 1 imported", { exact: true })).toHaveCount(0);
            expect(resourceCalls).toBe(beforeCompletion);
          }
          expect(errors).toEqual([]);
        } finally { release(); }
      });
    }
  }
}
