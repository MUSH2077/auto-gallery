import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 60_000 });

const me = { id: 42, username: "creator-ui", display_name: "Creator UI", is_admin: true, is_active: true, permissions: ["curation"], modules: { library: true }, preferences: { lang: "en", theme: "light" }, nsfw_visible: true, must_change_password: false };
const workbench = { updated_at: "2026-10-08T00:00:00Z", queue: { default: 0, scheduled: 0, failed: 0, active_download_count: 0, active_import_count: 0, failed_download_count: 0, failed_import_count: 0, stale_download_count: 0, stale_import_count: 0, stale_count: 0 }, scheduler: { enabled: true, mode: "interval", timezone: "UTC", scan_interval_minutes: 60 }, storage: { disk_total_bytes: 1, disk_free_bytes: 1, disk_used_bytes: 0, risk_level: "ok" }, health: {}, attention: { auth_unhealthy_count: 0, failed_download_count: 0, failed_import_count: 0, stale_job_count: 0, low_disk_warning: false, scheduler_disabled_warning: false }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] } };

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setup(context: BrowserContext, extra?: (route: Route, path: string) => boolean | void | Promise<boolean | void>) {
  const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_session", value: "creator-ui-session", url: origin }, { name: "ag_csrf", value: "creator-ui-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/auth/me") return json(route, me);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/notifications") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/workbench") return json(route, workbench);
    if (extra && (await extra(route, path)) !== false) return;
    if (path === "/api/v1/creators/duplicates") return json(route, { total: 1, duplicates: [{ reason: "same_identity", description: "Synthetic duplicate", creator_ids: ["creator-a", "creator-b"], creator_names: ["Atlas", "Atlas Alias"] }] });
    return json(route, { items: [], total: 0 });
  });
}

test("creator duplicates render the business surface and keep merge controls actionable", async ({ page, context }) => {
  await setup(context);
  await page.goto("/admin/creators/duplicates");
  await expect(page.locator(".gallery-creators-duplicates")).toBeVisible();
  await expect(page.getByText("Synthetic duplicate")).toBeVisible();
  await page.getByRole("checkbox", { name: /Atlas Alias/ }).check();
  await expect(page.getByRole("button", { name: /Merge 1/ })).toBeVisible();
  await page.getByRole("button", { name: /Merge 1/ }).click();
  await expect(page.getByRole("dialog")).toContainText("Atlas Alias");
});
test("creator list keeps selection, favorite, filter, and create actions usable", async ({ page, context }) => {
  let favoriteCalls = 0;
  let createBody: unknown;
  const creator = { id: "creator-list", name: "atlas-list", display_name: "Atlas List", description: "Neutral list fixture", is_active: true, is_favorite: false, subscription_count: 0, source_count: 1, repository_count: 1, created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z" };
  await setup(context, async (route, path) => {
    const request = route.request();
    if (path === "/api/v1/search/assist") {
      return json(route, { query: "is:favorite", canonical_query: "is:favorite", parsed: { tokens: [{ kind: "qualifier", key: "is", value: "favorite", negated: false, quoted: false, start: 0, end: 12 }] }, diagnostics: [], suggestions: [], catalog: [] });
    }
    if (path === "/api/v1/search") {
      return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, groups: { creators: { total: 1, items: [creator] } }, total: 1, results: [], creators: [creator] });
    }
    if (path === "/api/v1/creators/creator-list/favorite" && request.method() === "POST") {
      favoriteCalls += 1;
      return json(route, { ...creator, is_favorite: true });
    }
    if (path === "/api/v1/creators" && request.method() === "POST") {
      createBody = request.postDataJSON();
      return json(route, { ...creator, id: "creator-created", name: "created-name" }, 201);
    }
    return false;
  });
  await page.goto("/admin/creators");
  await expect(page.getByText("Atlas List", { exact: true })).toBeVisible();
  await expect(page.locator(".entity-list")).toHaveCSS("border-radius", "0px");
  await expect(page.locator(".entity-list")).toHaveCSS("border-top-width", "0px");
  await page.getByRole("checkbox", { name: "Select all" }).check();
  await expect(page.getByRole("checkbox", { name: "Select all" })).toBeChecked();
  await page.locator("button[aria-label=\"Favorite\"]").click();
  await expect.poll(() => favoriteCalls).toBe(1);
  await page.getByRole("combobox",{name:"Filter",exact:true}).click();
  await page.getByRole("option",{name:"Favorites",exact:true}).click();
  await expect(page).toHaveURL(/q=/);
  await page.getByRole("button", { name: /New/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name *").fill("created-name");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect.poll(() => createBody).toEqual({ name: "created-name" });
});
test("creator detail keeps profile editing actionable with neutral data", async ({ page, context }) => {
  let updateBody: unknown;
  const creator = { id: "creator-detail", name: "atlas-detail", display_name: "Atlas Detail", description: "Before edit", is_active: true, is_favorite: false, subscription_count: 0, source_count: 0, repository_count: 0, created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z", curation_state: { visibility: "visible" } };
  await setup(context, async (route, path) => {
    const request = route.request();
    if (path === "/api/v1/creators/creator-detail" && request.method() === "GET") return json(route, creator);
    if (path === "/api/v1/creators/creator-detail" && request.method() === "PATCH") { const body = request.postDataJSON() as Record<string, unknown>; updateBody = body; return json(route, { ...creator, ...body }); }
    if (path === "/api/v1/creators/creator-detail/links") return json(route, []);
    if (path === "/api/v1/creators/creator-detail/references") return json(route, { pixiv: [], danbooru: null });
    if (path === "/api/v1/creators/creator-detail/stats") return json(route, { creator_id: "creator-detail", total_works: 0, total_assets: 0, total_tags: 0, source_breakdown: [], tag_distribution: [], monthly_frequency: [] });
    if (path === "/api/v1/creators/creator-detail/timeline") return json(route, { creator_id: "creator-detail", sources: [], days: [], total: 0 });
    if (path === "/api/v1/creators/creator-detail/subscription-overview") return json(route, { summary: { repository_count: 0, running_job_count: 0 }, repositories: [], subscriptions: [] });
    if (path === "/api/v1/system/scheduler-decisions") return json(route, { items: [] });
    if (path === "/api/v1/search") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, groups: { works: { total: 0, items: [] } }, total: 0, results: [] });
    return false;
  });
  await page.goto("/admin/creators/creator-detail");
  await expect(page.getByRole("heading", { name: "Atlas Detail" })).toBeVisible();
  await page.getByRole("button", { name: "Edit profile" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Description").fill("After edit");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => updateBody).toEqual({ name: "atlas-detail", display_name: "Atlas Detail", description: "After edit" });
});

test("creator deletion blocks active tasks and requires an exact phrase before file cleanup", async ({ page, context }) => {
  let activeTasks = 1;
  let deletes = 0;
  const queries: string[] = [];
  const creator = { id: "creator-delete", name: "atlas-delete", display_name: "Atlas Archive", is_active: true, is_favorite: false, source_count: 0, repository_count: 0 };
  await setup(context, async (route, path) => {
    if (path === "/api/v1/search") return json(route, { groups: { creators: { total: 1, items: [creator] } }, total: 1, parsed: { tokens: [] } });
    if (path === "/api/v1/creators/creator-delete/deletion-preview") return json(route, { mode: "permanent", active_task_count: activeTasks, can_delete_files: true, affected_work_count: 3, exclusive_work_count: 2, shared_work_count: 1, exclusive_asset_count: 2 });
    if (path === "/api/v1/creators/creator-delete" && route.request().method() === "DELETE") {
      deletes += 1;
      queries.push(new URL(route.request().url()).search);
      await new Promise(resolve => setTimeout(resolve, 150));
      return deletes === 1 ? json(route, { detail: "fixture cleanup unavailable" }, 503) : json(route, { entity_ids: [creator.id] });
    }
    return false;
  });
  const openDelete = async () => {
    await page.goto("/admin/creators");
    await page.getByRole("button", { name: "More actions", exact: true }).click();
    await page.getByRole("menuitem", { name: "Permanently delete", exact: true }).click();
    return page.getByRole("dialog");
  };
  let dialog = await openDelete();
  await expect(dialog.getByRole("alert")).toContainText("1 active task");
  await dialog.getByRole("textbox").fill("Atlas Archive");
  await expect(dialog.getByRole("button", { name: "Confirm", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(deletes).toBe(0);
  activeTasks = 0;
  dialog = await openDelete();
  const confirm = dialog.getByRole("button", { name: "Confirm", exact: true });
  await dialog.getByRole("textbox").fill("atlas archive");
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("textbox").fill("Atlas Archive");
  await dialog.getByRole("checkbox").check();
  await expect(confirm).toBeEnabled();
  await confirm.evaluate(button => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect(dialog.getByRole("alert")).toContainText("fixture cleanup unavailable");
  expect(deletes).toBe(1);
  await confirm.click();
  await expect(dialog).toHaveCount(0);
  expect(deletes).toBe(2);
  expect(queries).toEqual(["?delete_files=true", "?delete_files=true"]);
});


test("creator favorites respond immediately and serialize rapid clicks", async ({ page, context }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let calls = 0;
  const creator = { id: "creator-instant", name: "atlas", display_name: "Atlas Instant", is_active: true, is_favorite: false, source_count: 0, repository_count: 0 };
  await setup(context, async (route, path) => {
    if (path === "/api/v1/search") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, groups: { creators: { total: 1, items: [creator] } }, total: 1, results: [], creators: [creator] });
    if (path === "/api/v1/creators/creator-instant") return json(route, creator);
    if (path === "/api/v1/creators/creator-instant/favorite") {
      calls++;
      if (calls === 1) await gate;
      creator.is_favorite = !creator.is_favorite;
      return json(route, creator);
    }
    return false;
  });
  try {
    await page.goto("/admin/creators");
    const button = page.getByRole("button", { name: "Favorite", exact: true });
    await button.focus();
    await page.keyboard.press("Enter");
    const active = page.getByRole("button", { name: "Unfavorite", exact: true });
    await expect(active).toHaveAttribute("aria-pressed", "true");
    await active.click();
    await expect(page.getByRole("button", { name: "Favorite", exact: true })).toHaveAttribute("aria-pressed", "false");
    expect(calls).toBe(1);
    release();
    await expect.poll(() => calls).toBe(2);
    await expect(page.getByRole("button", { name: "Favorite", exact: true })).toBeVisible();
  } finally { release(); }
});

test("creator favorite failure restores the visible state", async ({ page, context }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const creator = { id: "creator-failure", name: "atlas", display_name: "Atlas Failure", is_active: true, is_favorite: false, source_count: 0, repository_count: 0 };
  await setup(context, async (route, path) => {
    if (path === "/api/v1/search") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, groups: { creators: { total: 1, items: [creator] } }, total: 1, results: [], creators: [creator] });
    if (path === "/api/v1/creators/creator-failure") return json(route, creator);
    if (path === "/api/v1/creators/creator-failure/favorite") { await gate; return json(route, { detail: "Favorite failed" }, 500); }
    return false;
  });
  try {
    await page.goto("/admin/creators");
    await page.getByRole("button", { name: "Favorite", exact: true }).click();
    await expect(page.getByRole("button", { name: "Unfavorite", exact: true })).toBeVisible();
    release();
    await expect(page.getByRole("button", { name: "Favorite", exact: true })).toBeVisible();
    await expect(page.getByText(/Favorite failed/)).toBeVisible();
  } finally { release(); }
});


test("creator favorite remains selected when opening detail while pending", async ({ page, context }) => {
 let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 const creator={id:"creator-cross",name:"atlas",display_name:"Atlas Cross",is_active:true,is_favorite:false,source_count:0,repository_count:0,curation_state:{visibility:"visible"}};
 await setup(context,async(route,path)=>{
  if(path==="/api/v1/search")return json(route,{query:"",canonical_query:"",parsed:{tokens:[]},groups:{creators:{total:1,items:[creator]},works:{total:0,items:[]}},total:1,results:[],creators:[creator]});
  if(path==="/api/v1/creators/creator-cross")return json(route,creator);
  if(path==="/api/v1/creators/creator-cross/favorite"){await gate;creator.is_favorite=true;return json(route,creator);}
  if(path==="/api/v1/creators/creator-cross/links")return json(route,[]);
  if(path==="/api/v1/creators/creator-cross/references")return json(route,{pixiv:[],danbooru:null});
  if(path.endsWith('/stats'))return json(route,{creator_id:creator.id,total_works:0,total_assets:0,total_tags:0,source_breakdown:[],tag_distribution:[],monthly_frequency:[]});
  if(path.endsWith('/timeline'))return json(route,{creator_id:creator.id,sources:[],days:[],total:0});
  if(path.endsWith('/subscription-overview'))return json(route,{summary:{repository_count:0,running_job_count:0},repositories:[],subscriptions:[]});
  return false;
 });
 try{
  await page.goto('/admin/creators');await page.getByRole('button',{name:'Favorite',exact:true}).click();
  await page.getByText('Atlas Cross',{exact:true}).click();
  await expect(page).toHaveURL(/creator-cross/);
  await expect(page.getByRole('button',{name:/Unfavorite|Unstar/i}).first()).toHaveAttribute('aria-pressed','true');
 }finally{release();}
});


test("creator favorites filter reflects removal", async ({ page, context }) => {
  const creator = { id:"creator-filter",name:"atlas",display_name:"Atlas Filter",is_active:true,is_favorite:true,source_count:0,repository_count:0 };
  await setup(context, async (route, path) => {
    if(path === "/api/v1/search") { const items=creator.is_favorite?[creator]:[];return json(route,{query:"is:favorite",canonical_query:"is:favorite",parsed:{tokens:[{kind:"qualifier",key:"is",value:"favorite",negated:false}]},groups:{creators:{total:items.length,items}},total:items.length,results:[],creators:items}); }
    if(path === "/api/v1/creators/creator-filter/favorite"){creator.is_favorite=false;return json(route,creator);}
    return false;
  });
  await page.goto("/admin/creators?q=is%3Afavorite");
  await page.getByRole("button",{name:"Unfavorite",exact:true}).click();
  await expect(page.getByText("Atlas Filter",{exact:true})).toHaveCount(0);
});


test("creator lost response reconciles the server state without a second toggle",async({page,context})=>{
 const creator={id:"creator-timeout",name:"atlas",display_name:"Atlas Timeout",is_active:true,is_favorite:false,source_count:0,repository_count:0};let calls=0;
 await setup(context,async(route,path)=>{
  if(path==='/api/v1/search')return json(route,{query:'',canonical_query:'',parsed:{tokens:[]},groups:{creators:{total:1,items:[creator]}},total:1,results:[],creators:[creator]});
  if(path==='/api/v1/creators/creator-timeout')return json(route,creator);
  if(path==='/api/v1/creators/creator-timeout/favorite'){calls++;creator.is_favorite=true;return route.abort('timedout');}
  return false;
 });
 await page.goto('/admin/creators');await page.getByRole('button',{name:'Favorite',exact:true}).click();
 await expect(page.getByRole('button',{name:'Unfavorite',exact:true})).toBeVisible();
 await expect.poll(()=>calls).toBe(1);await expect(page.getByRole('log')).toBeVisible();
});


test("creator edit responds immediately and restores draft on failure", async ({page,context}) => {
 let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 const creator={id:"creator-edit-instant",name:"atlas",display_name:"Before",description:"Old",is_active:true,is_favorite:false,source_count:0,repository_count:0,curation_state:{visibility:"visible"}};
 await setup(context,async(route,path)=>{
  if(path==='/api/v1/creators/creator-edit-instant' && route.request().method()==='PATCH'){await gate;return json(route,{detail:'Save failed'},500);}
  if(path==='/api/v1/creators/creator-edit-instant')return json(route,creator);
  if(path.endsWith('/links'))return json(route,[]);
  if(path.endsWith('/references'))return json(route,{pixiv:[],danbooru:null});
  if(path.endsWith('/stats'))return json(route,{source_breakdown:[],tag_distribution:[],monthly_frequency:[]});
  if(path.endsWith('/timeline'))return json(route,{sources:[],days:[],total:0});
  if(path.endsWith('/subscription-overview'))return json(route,{summary:{repository_count:0,running_job_count:0},repositories:[],subscriptions:[]});
  if(path==='/api/v1/search')return json(route,{query:'',parsed:{tokens:[]},groups:{works:{total:0,items:[]}}});return false;
 });
 try{
  await page.goto('/admin/creators/creator-edit-instant');await page.getByRole('button',{name:'Edit profile'}).click();
  const dialog=page.getByRole('dialog');await dialog.getByLabel('Display name').fill('After');await dialog.getByRole('button',{name:'Save',exact:true}).click();
  await expect(dialog).toHaveCount(0);await expect(page.getByRole('heading',{name:'After'})).toBeVisible();await expect(page.getByRole('status').filter({hasText:/saving/i})).toBeVisible();
  release();await expect(page.getByRole('dialog').getByLabel('Display name')).toHaveValue('After');await expect(page.getByText('Save failed').first()).toBeVisible();
 }finally{release();}
});


test("creator profile save uses returned detail without rereading profile or links",async({page,context})=>{
 let detailReads=0,linkReads=0;
 const creator={id:"creator-edit-scope",name:"atlas",display_name:"Before",description:"Old",is_active:true,is_favorite:false,source_count:0,repository_count:0,curation_state:{visibility:"visible"}};
 await setup(context,async(route,path)=>{
  if(path==='/api/v1/creators/creator-edit-scope' && route.request().method()==='PATCH'){creator.display_name='After';return json(route,creator);}
  if(path==='/api/v1/creators/creator-edit-scope'){detailReads++;return json(route,creator);}
  if(path.endsWith('/links')){linkReads++;return json(route,[]);}
  if(path.endsWith('/references'))return json(route,{pixiv:[],danbooru:null});
  if(path.endsWith('/stats'))return json(route,{source_breakdown:[],tag_distribution:[],monthly_frequency:[]});
  if(path.endsWith('/timeline'))return json(route,{sources:[],days:[],total:0});
  if(path.endsWith('/subscription-overview'))return json(route,{summary:{repository_count:0,running_job_count:0},repositories:[],subscriptions:[]});
  if(path==='/api/v1/search')return json(route,{query:'',parsed:{tokens:[]},groups:{works:{total:0,items:[]}}});return false;
 });
 await page.goto('/admin/creators/creator-edit-scope');await page.getByRole('button',{name:'Edit profile'}).click();
 const dialog=page.getByRole('dialog');await dialog.getByLabel('Display name').fill('After');await dialog.getByRole('button',{name:'Save',exact:true}).click();
 await expect(page.getByRole('heading',{name:'After'})).toBeVisible();await expect(page.getByRole('button',{name:'Edit profile'})).toBeEnabled();
 expect(detailReads).toBe(1);expect(linkReads).toBe(1);
});


test('favoriting a creator keeps its in-flight external links query alive',async({page,context})=>{
 let release!:()=>void,linkReads=0;const pending=new Promise<void>(resolve=>{release=resolve;});
 const creator={id:'favorite-loading',name:'atlas',display_name:'Atlas Loading',is_active:true,is_favorite:false,source_count:0,repository_count:0,curation_state:{visibility:'visible'}};
 await setup(context,async(route,path)=>{
  if(path==='/api/v1/creators/favorite-loading')return json(route,creator);
  if(path.endsWith('/favorite')){creator.is_favorite=true;return json(route,creator);}
  if(path.endsWith('/links')){linkReads++;await pending;return json(route,[{id:'link-one',link_type:'pixiv',url:'https://www.pixiv.net/users/7324626'}]);}
  if(path.endsWith('/references'))return json(route,{pixiv:[],danbooru:null});
  if(path.endsWith('/stats'))return json(route,{source_breakdown:[],tag_distribution:[],monthly_frequency:[]});
  if(path.endsWith('/timeline'))return json(route,{sources:[],days:[],total:0});
  if(path.endsWith('/subscription-overview'))return json(route,{summary:{repository_count:0,running_job_count:0},repositories:[],subscriptions:[]});
  if(path==='/api/v1/search')return json(route,{query:'',parsed:{tokens:[]},groups:{works:{total:0,items:[]}}});return false;
 });
 try{
  await page.goto('/admin/creators/favorite-loading');await expect.poll(()=>linkReads).toBe(1);
  await page.getByRole('button',{name:'Star',exact:true}).click();await expect(page.getByRole('button',{name:'Unstar',exact:true})).toBeVisible();
  release();await expect(page.locator('a[href="https://www.pixiv.net/users/7324626"]')).toBeVisible();expect(linkReads).toBe(1);
 }finally{release();}
});
