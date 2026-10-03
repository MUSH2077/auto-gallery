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
