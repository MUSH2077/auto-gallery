import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
const me = { id: 7, username: "upload-reviewer", display_name: "Upload reviewer", is_admin: false, is_active: true, permissions: ["upload", "library", "tasks", "curation", "subscriptions"], modules: { library: true }, preferences: { lang: "en", theme: "light" }, upload_used_bytes: 0, upload_quota_bytes: 100000000, must_change_password: false };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function install(context: BrowserContext, extra?: (route: Route, path: string) => Promise<boolean | void> | boolean | void) {
  await context.addCookies([{ name: "ag_session", value: "upload-review", url: origin }, { name: "ag_csrf", value: "upload-review-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/notifications") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (extra && (await extra(route, path)) !== false) return;
    return json(route, {});
  });
}

test.describe.configure({ timeout: 60_000 });test.use({actionTimeout:10000});

test("manual upload keeps metadata, queue progress, task link, and invalid-file feedback", async ({ page, context }) => {
  let uploadCalls = 0;
  await install(context, async (route, path) => {
    if (path === "/api/upload") {
      uploadCalls += 1;
      return json(route, { work_id: "work-uploaded", import_job_id: "import-uploaded", download_job_id: null });
    }
    return false;
  });
  await page.goto("/admin/upload");
  await expect(page.getByRole("heading", { name: /Upload/, level: 1 })).toBeVisible();
  await page.getByLabel("Title").fill("Harbor study");
  await page.getByLabel("Tags").fill("harbor, study");
  await page.locator('input[type="file"]').setInputFiles({ name: "harbor.png", mimeType: "image/png", buffer: Buffer.from("png") });
  await expect(page.getByText("harbor.png", { exact: true })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles({ name: "notes.txt", mimeType: "text/plain", buffer: Buffer.from("bad") });
  await expect(page.getByText(/Unsupported|不支持/i)).toBeVisible();
  await page.getByRole("button", { name: "Start Upload (1)", exact:true }).click();
  await expect.poll(() => uploadCalls).toBe(1);
  await expect(page.getByText("Accepted",{exact:true})).toBeVisible();
  await expect(page.getByRole("link", { name: /Track|任务/i })).toHaveAttribute("href", /import_job=import-uploaded/);
});

test("Danbooru reference keeps lookup, preview, and batch controls on the business surface", async ({ page, context }) => {
  await install(context, async (route, path) => {
    const request = route.request();
    if (path === "/api/v1/reference/danbooru/artist/preview" && request.method() === "POST") {
      return json(route, { status: "ok", found: true, artist: { id: 42, name: "harbor_artist", other_names: ["harbor"], post_count: 12, urls: [] }, suggested_links: [] });
    }
    return false;
  });
  await page.goto("/admin/upload/danbooru");
  await expect(page.getByRole("heading", { name: /Danbooru/i, level: 1 })).toBeVisible();
  await page.getByPlaceholder("ask (askzy)").fill("harbor_artist");
  await page.getByRole("button", { name: /Search|搜索/i }).last().click();
  await expect(page.getByText(/harbor_artist/)).toBeVisible();
  await expect(page.getByRole("heading",{name:"Batch Import by URL",exact:true})).toBeVisible();
});

test("Danbooru restored results keep all typed result groups and their details",async({page,context})=>{
 const result={total:4,imported_count:1,low_confidence_count:1,not_found_count:1,error_count:1,
 imported:[{pixiv_id:"111",creator_id:"creator-1",artist_name:"Imported Artist",artist_id:11,links_imported:2,sources_created:1,downloadable_urls:["https://example.invalid/imported"]}],
 low_confidence:[{pixiv_id:"222",artist_name:"Tentative Artist",artist_id:22,url_count:1,message:"review fixture"}],
 not_found:[{pixiv_id:"333",message:"missing fixture"}],errors:[{pixiv_id:"444",error:"failed fixture"}]};
 await install(context,async(route,path)=>{
  if(path==="/api/v1/reference/danbooru/artist/batch-import/status")return json(route,{status:"completed",progress:null,result,job_status:"finished"});
  return false;
 });
 await context.addInitScript(()=>sessionStorage.setItem("danbooru_batch_job",JSON.stringify({jobId:"typed-batch",importType:"pixiv",total:4,startedAt:Date.now()})));
 await page.goto("/admin/upload/danbooru");
 await page.locator("section").filter({has:page.getByRole("heading",{name:"Batch Import by Pixiv User ID",exact:true})}).last().getByRole("button",{name:"Expand",exact:true}).click();
 await expect(page.getByText("Imported Artist",{exact:true})).toBeVisible();
 await expect(page.getByText("Tentative Artist",{exact:true})).toBeVisible();
 await page.locator("summary").filter({hasText:"Tentative Artist"}).click();
 await expect(page.getByText("review fixture",{exact:true})).toBeVisible();
 for(const text of ["missing fixture","failed fixture"]){
  const group=page.locator("details").filter({hasText:text}).last();await group.locator("summary").click();
  await expect(group).toContainText(text);
 }
});
