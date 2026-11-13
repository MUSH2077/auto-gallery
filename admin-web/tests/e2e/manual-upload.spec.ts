import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page, context, baseURL }) => {
  await context.addCookies([
    { name: "ag_session", value: "upload-ui-fixture", url: baseURL! },
    { name: "ag_csrf", value: "upload-ui-csrf", url: baseURL! },
  ]);
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const body = url.pathname === "/api/v1/auth/me" ? {
      id: 1, username: "fixture", display_name: "Fixture", is_admin: true,
      is_active: true, nsfw_visible: false, permissions: [], must_change_password: false,
      modules: { upload: "write", library: "write", tasks: "write", curation: "write", system: "write" },
      preferences: { lang: "zh" }, upload_quota_bytes: null, upload_used_bytes: 0,
    } : url.pathname === "/api/v1/system/workbench" ? {
      queue: { active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_count: 0 },
      attention: {}, storage: {}, scheduler: {}, health: {},
      recent: { download_jobs: [], import_jobs: [], tasks: [], works: [], successful_syncs: [] },
    } : { items: [], total: 0, groups: {}, decisions: [], queues: [], ok: true };
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/admin/upload");
  await expect(page.getByText("上传配额", { exact: true })).toBeVisible();
});

const files = (count: number) => Array.from({ length: count }, (_, index) => ({
  name: "fixture-" + index + ".jpg", mimeType: "image/jpeg", buffer: Buffer.from([255, 216, 255, 217]),
}));

test("a combined upload over 20 files focuses an inline error without sending", async ({ page }) => {
  let sent = 0;
  await page.route("**/api/upload", async route => {
    sent++;
    await route.fulfill({ status: 500, body: "Must not submit" });
  });
  await page.getByLabel("合并为一个作品（多页）", { exact: true }).check();
  await page.locator('input[type="file"]').setInputFiles(files(21));
  await page.getByRole("button", { name: "开始上传（21）", exact: true }).click();
  const error = page.locator("#upload-batch-error");
  await expect(error).toContainText("20 个文件");
  await expect(error).toBeFocused();
  expect(sent).toBe(0);
});

test("a file-size 413 displays its reason and continues the next file", async ({ page }) => {
  let sent = 0;
  await page.route("**/api/upload", async route => {
    sent++;
    await route.fulfill({ status: sent === 1 ? 413 : 200, contentType: "application/json",
      body: JSON.stringify(sent === 1 ? {
        detail: { code: "upload_file_too_large", message: "File too large" },
      } : {
        work_id: "fixture-work", import_job_id: "fixture-import", download_job_id: "fixture-download",
        used_bytes: 4, quota_bytes: null,
      }) });
  });
  await page.locator('input[type="file"]').setInputFiles(files(2));
  await page.getByRole("button", { name: "开始上传（2）", exact: true }).click();
  await expect(page.getByText("已接受", { exact: true })).toBeVisible();
  await expect(page.getByText("文件超过 500MiB 限制，请选择较小的文件。", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("已取消", { exact: true })).toHaveCount(0);
  expect(sent).toBe(2);
});

test("a quota 413 cancels later files and explains remaining capacity", async ({ page }) => {
  let sent = 0;
  await page.route("**/api/upload", async route => {
    sent++;
    await route.fulfill({ status: 413, contentType: "application/json",
      body: JSON.stringify({ detail: { code: "upload_quota_exceeded", message: "Quota exceeded" } }) });
  });
  await page.locator('input[type="file"]').setInputFiles(files(2));
  await page.getByRole("button", { name: "开始上传（2）", exact: true }).click();
  await expect(page.getByText("已取消", { exact: true })).toBeVisible();
  await expect(page.getByText("所选文件超过剩余配额，请减少文件或调整配额后重试。", { exact: true }).first()).toBeVisible();
  expect(sent).toBe(1);
});

test("the keyboard file chooser adds a file to the upload queue", async ({ page }) => {
  const button = page.getByRole("button", { name: /拖拽文件到此处，或点击选择/ });
  await button.focus();
  const chooser = page.waitForEvent("filechooser");
  await button.press("Enter");
  await (await chooser).setFiles(files(1));
  await expect(page.getByRole("button", { name: "开始上传（1）", exact: true })).toBeEnabled();
});
