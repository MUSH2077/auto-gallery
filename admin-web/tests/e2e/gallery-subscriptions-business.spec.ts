import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
const me = { id: 17, username: "subscriptions-ui", display_name: "Subscriptions UI", is_admin: true, is_active: true, permissions: ["library", "subscriptions", "tasks", "system"], preferences: { lang: "en", theme: "light" }, must_change_password: false };
const subscription = { id: "sub-1", creator_id: "creator-1", creator_name: "Aster", creator_display_name: "Aster", name: "Aster feed", is_active: true, sync_enabled: true, sync_interval_hours: 24, enabled_source_count: 1, source_count: 1, last_synced_at: "2026-10-24T08:00:00Z" };
const source = { id: "source-1", subscription_id: "sub-1", source: "pixiv", source_creator_id: "123", source_url: "https://www.pixiv.net/users/123", is_enabled: true, auth_healthy: true, auth_state: "healthy", credential_state: "ready", created_at: "2026-10-24T08:00:00Z", updated_at: "2026-10-24T08:00:00Z" };
function compareTokens(query:string){return [...query.matchAll(/(is|sort):([^ ]+)/g)].map(match=>({kind:"qualifier",key:match[1],value:match[2],negated:false,quoted:false,start:match.index,end:match.index!+match[0].length}));}
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

async function setup(context: BrowserContext, extra?: (route: Route, path: string) => boolean | Promise<boolean>) {
  const calls: Array<{ path: string; method: string; query: string; body: unknown }> = [];
  await context.addCookies([{ name: "ag_session", value: "subscriptions-ui", url: origin }, { name: "ag_csrf", value: "subscriptions-ui-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
    calls.push({ path, method, query: url.search, body: route.request().postDataJSON() });
    if (path === "/api/v1/auth/me") return void await json(route, me);
    if (path === "/api/v1/notifications") return void await json(route, { total: 0, items: [] });
    if (path === "/api/v1/auth/ws-ticket") return void await json(route, { detail: "fixture websocket unavailable" }, 503);
    if (extra && await extra(route, path)) return;
    if (path === "/api/v1/search/name-anchors") return void await json(route, { scope: "subscriptions", direction: "asc", total: 0, items: [] });
    if (path === "/api/v1/system/scheduler-decisions") return void await json(route,{items:[],total:0,next_offset:null,summary:{}});
    if (path === "/api/v1/search/assist") {
      const body=route.request().postDataJSON() || {};
      const composes=body.composes || (body.compose?[body.compose]:[]);
      const query=[body.before_cursor || "",...composes.filter((c:{value?:string})=>c.value).map((c:{key:string;value:string})=>c.key+":"+c.value)].join(" ").trim();
      return void await json(route,{query,canonical_query:query,parsed:{tokens:compareTokens(query)},diagnostics:[],suggestions:[],catalog:[]});
    }
    if (path === "/api/v1/search") return void await json(route, { query: url.searchParams.get("q") || "", parsed: { tokens: compareTokens(url.searchParams.get("q") || "") }, groups: { subscriptions: { total: 1, items: [subscription] } }, total: 1 });
    if (path === "/api/v1/subscriptions/summaries") return void await json(route, { items: [{ subscription_id: "sub-1", enabled_source_count: 1, source_count: 1, schedule: { effective_mode: "interval", sync_interval_hours: 24, due_sources: 0, blocked_sources: 0 } }] });
    if (path === "/api/v1/subscriptions/sub-1" && method === "GET") return void await json(route, subscription);
    if (path === "/api/v1/subscriptions/sub-1/sources" && method === "GET") return void await json(route, [source]);
    if (path === "/api/v1/subscriptions/sub-1/scheduler-decisions") return void await json(route, { items: [], total: 0, next_offset: null });
    if (path === "/api/v1/download-jobs") return void await json(route, []);
    if (path === "/api/v1/sources") return void await json(route, { sources: [{ source_name: "pixiv", display_name: "Pixiv", capabilities: { can_download: true, can_import_local: false } }] });
    if (path === "/api/v1/creators" && method === "GET") return void await json(route, { items: [{ id: "creator-1", name: "aster", display_name: "Aster" }], total: 1 });
    if (path === "/api/v1/creators/creator-1") return void await json(route, { id: "creator-1", name: "aster", display_name: "Aster" });
    return void await json(route, {});
  });
  return calls;
}

test.describe.configure({ timeout: 60_000 });test.use({actionTimeout:10000});

test("subscription list filters, selects, creates, and runs a sync command", async ({ page, context }) => {
  const calls = await setup(context, async (route, path) => {
    const method = route.request().method();
    if (path === "/api/v1/subscriptions/sub-1/sync-now" && method === "POST") {
      await json(route, { status: "enqueued", job_ids: ["job-1"], skipped_count: 0 });
      return true;
    }
    if (path === "/api/v1/subscriptions" && method === "POST") {
      await json(route, { ...subscription, id: "sub-created" }, 201);
      return true;
    }
    return false;
  });
  await page.goto("/admin/subscriptions");
  await expect(page.getByRole("heading", { name: "Subscriptions", exact: true })).toBeVisible();
  await expect(page.getByText("Aster feed", { exact: true })).toBeVisible();
  const search = page.getByRole("combobox").first();
  await search.fill("Aster");
  await expect.poll(() => calls.some(call => call.path === "/api/v1/search" && call.query.includes("q=Aster"))).toBe(true);
  const filter = page.getByRole("combobox", { name: "Filter" });
  await filter.click();
  await page.getByRole("option", { name: "Active", exact: true }).click();
  await expect.poll(() => calls.some(call => call.path === "/api/v1/search" && call.query.includes("is%3Aactive"))).toBe(true);
  await page.getByRole("combobox",{name:"Sort",exact:true}).click();
  await page.getByRole("option",{name:/^Updated.*Descending$/}).click();
  await expect.poll(()=>calls.some(call=>call.path==="/api/v1/search"&&call.query.includes("sort%3Aupdated-desc"))).toBe(true);
  await page.getByRole("checkbox", { name: "Select all", exact: true }).check();
  await expect(page.getByRole("checkbox", { name: "Select all", exact: true })).toBeChecked();
  await page.getByRole("button", { name: "Sync all", exact: true }).click();
  await expect.poll(() => calls.some(call => call.path === "/api/v1/subscriptions/sub-1/sync-now" && call.method === "POST")).toBe(true);
  await page.getByRole("button", { name: "+ New", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox",{name:"Creator *",exact:true}).click();
  await page.getByRole("option",{name:"Aster",exact:true}).click();
  await dialog.locator("#new-subscription-label").fill("Created feed");
  await dialog.getByRole("button", { name: "Subscribe", exact: true }).click();
  await expect.poll(() => calls.some(call => call.path === "/api/v1/subscriptions" && call.method === "POST" && JSON.stringify(call.body).includes("creator-1"))).toBe(true);
});

test("subscription detail proves edit, source CRUD, and deletion confirmation behavior", async ({ page, context }) => {
  let patchBody: unknown = null;
  let sourceCreated = false;
  let sourceDeleted = false;
  let deletionPreviewCalls = 0;
  const calls = await setup(context, async (route, path) => {
    const method = route.request().method();
    if (path === "/api/v1/subscriptions/sub-1" && method === "PATCH") {
      patchBody = route.request().postDataJSON();
      await json(route, { ...subscription, name: "Aster edited" });
      return true;
    }
    if (path === "/api/v1/subscriptions/sub-1/sources" && method === "POST") {
      sourceCreated = true;
      await json(route, source, 201);
      return true;
    }
    if (path === "/api/v1/subscriptions/sub-1/sources/source-1" && method === "DELETE") {
      sourceDeleted = true;
      await json(route, { status: "deleted" });
      return true;
    }
    if (path === "/api/v1/repositories/source-1/deletion-preview") {
      deletionPreviewCalls += 1;
      await json(route, { mode: "permanent", active_task_count: 0, can_delete_files: true, affected_work_count: 1, exclusive_work_count: 1, shared_work_count: 0, exclusive_asset_count: 1 });
      return true;
    }
    return false;
  });
  await page.goto("/admin/subscriptions/sub-1");
  await expect(page.getByRole("heading", { name: "Aster feed", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog");
  await edit.locator("input").first().fill("Aster edited");
  await edit.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => patchBody).toEqual(expect.objectContaining({ name: "Aster edited" }));
  await page.getByRole("button", { name: "+ Add Source", exact: true }).click();
  const add = page.getByRole("dialog");
  await add.locator("input").first().fill("456");
  await add.getByRole("button", { name: "Add Source", exact: true }).click();
  await expect.poll(() => sourceCreated).toBe(true);
  await page.getByRole("button", { name: "Remove from my sources", exact: true }).click();
  const remove = page.getByRole("dialog");
  await expect.poll(() => deletionPreviewCalls).toBeGreaterThan(0);
  await remove.getByRole("textbox").fill("pixiv/123");
  await remove.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => sourceDeleted).toBe(true);
  expect(calls.some(call => call.path.includes("/deletion-preview"))).toBe(true);
});



test("subscription batch sync preserves failed selection and retries a rejected command",async({page,context})=>{
 let attempts=0;
 await setup(context,async(route,path)=>{
  if(path!=="/api/v1/subscriptions/batch-toggle-sync")return false;
  attempts++;expect(route.request().postDataJSON()).toEqual({ids:["sub-1"],sync_enabled:false});
  if(attempts===1)await json(route,{status:"ok",results:[{id:"sub-1",status:"error",error:"source denied"}]});
  else if(attempts===2)await json(route,{detail:"sync temporarily unavailable"},503);
  else await json(route,{status:"ok",results:[{id:"sub-1",status:"updated",sync_enabled:false}]});
  return true;
 });
 await page.goto("/admin/subscriptions");await page.getByRole("checkbox",{name:"Select all",exact:true}).check();const disable=page.getByRole("button",{name:"Disable Sync",exact:true});await disable.click();await expect(page.getByRole("log")).toContainText("source denied");await expect(page.getByRole("checkbox",{name:"Select all",exact:true})).toBeChecked();await disable.click();await expect(page.getByRole("log")).toContainText("sync temporarily unavailable");await expect(page.getByRole("checkbox",{name:"Select all",exact:true})).toBeChecked();await disable.click();await expect(page.getByRole("checkbox",{name:"Select all",exact:true})).not.toBeChecked();expect(attempts).toBe(3);
});


test("subscription-only readers do not query forbidden task library or system data",async({page,context})=>{
 const calls=await setup(context);
 await context.route("**/api/v1/auth/me",route=>json(route,{...me,is_admin:false,permissions:["subscriptions"]}));
 await page.goto("/admin/subscriptions/sub-1");await expect(page.getByRole("heading",{name:"Aster feed",exact:true})).toBeVisible();await expect(page.getByRole("button",{name:"Edit",exact:true})).toBeVisible();await expect(page.getByRole("button",{name:"+ Add Source",exact:true})).toBeVisible();expect(calls.some(c=>c.path==="/api/v1/system/scheduler-decisions"||c.path==="/api/v1/download-jobs"||c.path==="/api/v1/creators/creator-1")).toBe(false);
});


test("subscription batch results do not restore old selections after changing filters",async({page,context})=>{
 let release:(()=>void)|undefined;
 await setup(context,async(route,path)=>{if(path!=="/api/v1/subscriptions/batch-toggle-sync")return false;await new Promise<void>(resolve=>{release=resolve;});await json(route,{status:"ok",results:[{id:"sub-1",status:"error",error:"source denied"}]});return true;});
 await page.goto("/admin/subscriptions");await page.getByRole("checkbox",{name:"Select all",exact:true}).check();await page.getByRole("button",{name:"Disable Sync",exact:true}).click();await expect.poll(()=>Boolean(release)).toBe(true);await page.getByRole("combobox",{name:"Filter",exact:true}).click();await page.getByRole("option",{name:"Active",exact:true}).click();await expect(page.getByRole("checkbox",{name:"Select all",exact:true})).not.toBeChecked();release!();await expect(page.getByRole("log")).toContainText("source denied");await expect(page.getByRole("checkbox",{name:"Select all",exact:true})).not.toBeChecked();
});
