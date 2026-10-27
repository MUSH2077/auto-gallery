import { expect, test, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

const me = {
  id: 1,
  username: "admin",
  display_name: "Admin",
  is_admin: true,
  is_active: true,
  permissions: ["system", "tasks"],
  modules: {},
  preferences: {},
  nsfw_visible: true,
  upload_used_bytes: 0,
  must_change_password: false,
};

const settings = {
  dedup: { similarity_threshold: 0.95, perceptual_hash_threshold: 8, quarantine_days: 30 },
  subscription_defaults: {
    default_sync_interval_hours: 6,
    scheduler_scan_interval_minutes: 60,
    scheduler_enabled: true,
    schedule_mode: "interval",
    schedule_rule: null,
    timezone: "UTC",
  },
  download_defaults: {
    timeout_seconds: 1800,
    stall_timeout_seconds: 300,
    max_retries: 3,
    retry_backoff_base_seconds: 60,
    max_posts: 200,
    skip_ai_generated: false,
    gallerydl_retries: 3,
    gallerydl_timeout: 30,
    gallerydl_abort: 300,
    download_concurrency: 2,
  },
  proxy: { http_proxy: "", https_proxy: "", no_proxy: "", enabled: false },
};

const workbench = {
  updated_at: "2026-09-08T00:00:00Z",
  queue: { active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_count: 0 },
  scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 },
  storage: {},
  health: {},
  attention: {},
  recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] },
};

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

async function installShell(page: import("@playwright/test").Page) {
  await page.context().addCookies([
    { name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" },
    { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" },
  ]);
  await page.context().addInitScript(() => {
    localStorage.setItem("ag_token", "fixture");
    localStorage.setItem("auto-gallery-lang", "en");
  });
}

test("scheduler defaults save the selected calendar and timezone in one real payload and retain retry", async ({ page }) => {
  await installShell(page);
  let saves = 0;
  let payload: Record<string, unknown> | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/settings" && request.method() === "GET") return json(route, settings);
    if (path === "/api/v1/system/health") return json(route, { resource_pressure: { download_concurrency: { effective: 2, desired_effective: 2, restart_required: false } } });
    if (path === "/api/v1/admin/settings" && request.method() === "PUT") {
      payload = JSON.parse(request.postData() || "{}");
      saves += 1;
      return saves === 1 ? json(route, { detail: "temporary settings outage" }, 503) : json(route, { status: "ok", message: "saved" });
    }
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path.startsWith("/api/v1/tasks")) return json(route, { items: [], total: 0 });
    return json(route, {});
  });

  await page.goto("/admin/settings/scheduler-defaults");
  await page.getByRole("spinbutton", { name: /sync interval/i }).fill("12");
  const mode = page.getByRole("combobox", { name: "Scheduling Mode", exact: true });
  await mode.click();
  await page.getByRole("option", { name: /calendar/i }).click();
  const timezone = page.getByRole("combobox", { name: /timezone/i });
  await timezone.click();
  await page.getByRole("option", { name: "Asia/Shanghai", exact: true }).click();

  const save = page.getByRole("button", { name: /save/i }).last();
  await save.click();
  await expect(page.getByText("temporary settings outage")).toBeVisible();
  await save.click();
  await expect.poll(() => saves).toBe(2);
  expect(payload).toMatchObject({
    subscription_defaults: {
      schedule_mode: "calendar",
      timezone: "Asia/Shanghai",
      default_sync_interval_hours: 12,
    },
  });
});

test("backup estimate/create and restore upload preserve operation payloads and receipt handoff", async ({ page }) => {
  await installShell(page);
  let estimateStarts = 0;
  let creates = 0;
  let restoreCreates = 0;
  let restoreChunks = 0;
  let restoreValidations = 0;
  let createPayload: unknown = null;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (path === "/api/v1/admin/backup/list") return json(route, { backups: [] });
    if (path === "/api/v1/admin/backup/estimate/latest") return json(route, { current: null, snapshot: null });
    if (path === "/api/v1/admin/backup/latest") return json(route, { current: null, snapshot: null });
    if (path === "/api/v1/admin/backup/estimate" && request.method() === "POST") {
      estimateStarts += 1;
      return json(route, { task_id: "estimate-task", job_id: "estimate-job", operation_type: "admin-backup-estimate", status: "queued" });
    }
    if (path === "/api/v1/admin/backup" && request.method() === "POST") {
      creates += 1;
      createPayload = JSON.parse(request.postData() || "{}");
      return json(route, { task_id: "create-task", job_id: "create-job", operation_type: "admin-backup-create", status: "queued" });
    }
    if (path === "/api/v1/admin/operations/estimate-task") return json(route, { task_id: "estimate-task", job_id: "estimate-job", operation_type: "admin-backup-estimate", status: "complete", progress: {}, result: { components: { database: 10, "app-config": 4 } } });
    if (path === "/api/v1/admin/operations/create-task") return json(route, { task_id: "create-task", job_id: "create-job", operation_type: "admin-backup-create", status: "complete", progress: {}, result: { filename: "fixture.tar.gz", size_bytes: 14, size_mb: 0.01, contents: ["database"], restorable: true, component_sizes: { database: 10 } } });
    if (path === "/api/v1/admin/backup/restore/uploads" && request.method() === "POST") {
      restoreCreates += 1;
      return json(route, { upload_id: "upload-1", upload_token: "restore-token", filename: "fixture.tar.gz", size_bytes: 4, sha256: "fixture-hash", chunk_size: 1024 * 1024, total_chunks: 1, next_chunk: 0, received_chunks: 0, state: "uploading" });
    }
    if (path === "/api/v1/admin/backup/restore/uploads/upload-1/chunks/0" && request.method() === "PUT") {
      restoreChunks += 1;
      return json(route, { upload_id: "upload-1", filename: "fixture.tar.gz", size_bytes: 4, sha256: "fixture-hash", chunk_size: 1024 * 1024, total_chunks: 1, next_chunk: 1, received_chunks: 1, state: "uploaded" });
    }
    if (path === "/api/v1/admin/backup/restore/uploads/upload-1/validate" && request.method() === "POST") {
      restoreValidations += 1;
      return json(route, { task_id: "restore-validate-task", job_id: "restore-validate-job", operation_type: "admin-restore-validate", status: "queued" });
    }
    if (path === "/api/v1/admin/operations/restore-validate-task") return json(route, { task_id: "restore-validate-task", job_id: "restore-validate-job", operation_type: "admin-restore-validate", status: "complete", progress: {}, result: { request_id: "restore-request", host_command: "auto-gallery restore --request restore-request" } });
    if (path === "/api/v1/admin/backup/restore/receipts/restore-request") return json(route, { request_id: "restore-request", status: "success", phase: "validated" });
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path.startsWith("/api/v1/tasks")) return json(route, { items: [], total: 0 });
    return json(route, {});
  });

  await page.goto("/admin/settings/backup");
  await page.getByRole("button", { name: /refresh estimate/i }).click();
  await expect.poll(() => estimateStarts).toBe(1);
  await page.getByRole("button", { name: /^create backup$/i }).click();
  await expect.poll(() => creates).toBe(1);
  expect(createPayload).toMatchObject({ contents: expect.arrayContaining(["database", "library-metadata"]) });

  await page.locator('input[type="file"]').setInputFiles({ name: "fixture.tar.gz", mimeType: "application/gzip", buffer: Buffer.from("test") });
  await page.getByRole("button", { name: /^confirm$/i }).click();
  await expect.poll(() => restoreCreates).toBe(1);
  await expect.poll(() => restoreChunks).toBe(1);
  await expect.poll(() => restoreValidations).toBe(1);
  await expect(page.locator("[data-restore-flow] code").filter({ hasText: /^restore-request$/ })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Restore succeeded and all services restarted in phases.", {exact:true})).toBeVisible();
});




test("gallery-dl preserves typed values and retries the same config after rejection",async({page})=>{
 await installShell(page);
 const config={pixiv:{max_posts:12,filename:"original",metadata:false},twitter:{},iwara:{},danbooru:{},pinterest:{},lofter:{},weibo:{},bilibili:{},sources:{}};
 const writes:Array<{pixiv:{max_posts:number;filename:string;metadata:boolean}}>=[];
 await page.route("**/api/v1/**",async route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==="/api/v1/auth/me")return json(route,me);
  if(path==="/api/v1/auth/ws-ticket")return json(route,{detail:"unavailable"},503);
  if(path==="/api/v1/admin/gallerydl-config"){
   if(route.request().method()==="GET")return json(route,config);
   writes.push(route.request().postDataJSON());
   return json(route,writes.length===1?{detail:"config temporarily unavailable"}:{ok:true},writes.length===1?503:200);
  }
  if(path.endsWith("/latest"))return json(route,{current:null,snapshot:null});
  if(path==="/api/v1/notifications")return json(route,{total:0,items:[]});
  return json(route,{});
 });
 await page.goto("/admin/settings/gallerydl");
 await page.getByRole("switch",{name:"Fetch Metadata",exact:true}).click();
 await page.getByLabel("Max Posts",{exact:true}).fill("25");
 await page.getByLabel("Filename Pattern",{exact:true}).fill("archive-{id}");
 const save=page.getByRole("button",{name:/^Save Config/});
 await save.click();await expect(page.getByText("config temporarily unavailable",{exact:true})).toBeVisible();
 await save.click();await expect.poll(()=>writes.length).toBe(2);
 expect(writes[0].pixiv).toMatchObject({max_posts:25,filename:"archive-{id}",metadata:true});
 expect(writes[1]).toEqual(writes[0]);
 await page.getByRole("tab",{name:/Twitter|X/,exact:false}).click();
 await expect(page.getByRole("tabpanel")).toBeVisible();
});
