import {expect,test,type BrowserContext,type Route} from "@playwright/test";

const principal={id:81,username:"gallery-review",display_name:"Review",is_admin:true,is_active:true,permissions:["library","tasks"],modules:{},preferences:{lang:"en",theme:"light"},nsfw_visible:true,must_change_password:false};
const work={id:"work-route",title:"Harbor light",creator_name:"Review",is_nsfw:false,is_ai_generated:false,is_favorite:false,asset_count:0,created_at:"2026-10-08T00:00:00Z"};
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
async function session(context:BrowserContext,options:{authenticated?:boolean;user?:typeof principal}={}){
 let authenticated=options.authenticated??true;const user=options.user??principal;let reads=0;
 if(authenticated)await context.addCookies([{name:"ag_session",value:"review-session",url:process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000"}]);
 await context.addInitScript(()=>{localStorage.setItem("auto-gallery-lang","en");localStorage.setItem("auto-gallery-theme","light");});
 await context.route("**/api/v1/**",route=>{
  const path=new URL(route.request().url()).pathname;
  if(path==="/api/v1/auth/me"){reads++;return json(route,authenticated?user:{detail:"Session expired"},authenticated?200:401);}
  if(path==="/api/v1/auth/browser/login"){authenticated=true;return route.fulfill({status:200,headers:{"Content-Type":"application/json","Set-Cookie":"ag_session=review-session; Path=/; HttpOnly; SameSite=Lax"},body:JSON.stringify({ok:true})});}
  if(path==="/api/v1/auth/browser/logout"){authenticated=false;return json(route,{ok:true});}
  if(path==="/api/v1/search")return json(route,{parsed:{tokens:[],diagnostics:[]},groups:{works:{total:0,items:[]}},total:0,items:[],next_cursor:null});
  if(path==="/api/v1/system/workbench")return json(route,{updated_at:"2026-10-24T00:00:00Z",queue:{default:0,scheduled:0,failed:0,active_download_count:0,active_import_count:0,failed_download_count:0,failed_import_count:0,stale_download_count:0,stale_import_count:0,stale_count:0},scheduler:{enabled:true},storage:{},health:{},attention:{},recent:{download_jobs:[],import_jobs:[],works:[],successful_syncs:[]}});
  if(path==="/api/v1/tags/page")return json(route,{items:[],total:0,offset:0,limit:100});
  if(path==="/api/v1/curation/commits")return json(route,{items:[],total:0});
  if(path==="/api/v1/tasks")return json(route,{total:0,items:[],summary:{all:0,attention:0,running:0,waiting:0}});
  if(path==="/api/v1/works/derivative-progress")return json(route,{total:0,remaining:0,status:"idle"});
  if(path==="/api/v1/works/work-route")return json(route,work);
  if(path.match(/^\/api\/v1\/works\/work-route\/(sources|assets|tags)$/))return json(route,[]);
  if(path==="/api/v1/download-jobs")return json(route,[]);
  if(path==="/api/v1/notifications"||path==="/api/v1/import-jobs")return json(route,{total:0,items:[]});
  return json(route,{});
 });
 return {reads:()=>reads,expire:()=>{authenticated=false;}};
}

test("one session and one shell survive primary and account navigation",async({page,context})=>{
 const state=await session(context);await page.goto("/admin/works?q=harbor#collection");
 await expect(page.locator(".layout-bottom-nav a[aria-current=page]")).toHaveText("Works");
 await expect(page.locator("main#main-content")).toHaveCount(1);await expect(page.locator("#admin-sidebar")).toHaveCount(0);
 const before=page.url();await page.getByRole("link",{name:"Auto-Gallery",exact:true}).click();expect(page.url()).toBe(before);
 await page.locator(".layout-bottom-nav").getByRole("link",{name:"Jobs",exact:true}).click();await expect(page).toHaveURL(/\/admin\/jobs$/);
 await expect(page.locator(".layout-bottom-nav a[aria-current=page]")).toHaveText("Jobs");
 await page.locator(".gallery-account-trigger").click();await page.locator(".gallery-account-menu a[href='/admin/profile']").click();
 await expect(page).toHaveURL(/\/admin\/profile$/);await expect(page.locator(".layout-bottom-nav a[aria-current]")).toHaveCount(0);
 await page.locator(".gallery-account-trigger").click();await expect(page.locator(".gallery-account-menu a[href='/admin/profile']")).toHaveAttribute("aria-current","page");
 await page.keyboard.press("Escape");await expect(page.locator(".gallery-account-trigger")).toBeFocused();
 expect(state.reads()).toBe(1);await expect(page.locator("main#main-content")).toHaveCount(1);
});

test("limited access retains permitted navigation",async({page,context})=>{
 await session(context,{user:{...principal,is_admin:false,permissions:[]}});await page.goto("/admin/profile");
 await expect(page.locator(".layout-brand-wordmark")).toBeVisible();await expect(page.locator(".layout-bottom-nav .gallery-more-trigger")).toBeVisible();
 await page.locator(".gallery-account-trigger").click();await expect(page.locator(".gallery-account-menu a[href='/admin/profile']")).toBeVisible();
 await expect(page.locator(".gallery-account-menu a[href='/admin/settings/users']")).toHaveCount(0);
});

test("a bookmarked detail retains filters and closes inside the gallery",async({page,context})=>{
 await session(context);await page.goto("/admin/works/work-route?q=harbor&view=list");
 await expect(page).toHaveURL(/\/admin\/works\?q=harbor&view=list&work=work-route$/);
 const detail=page.getByRole("dialog");await expect(detail).toContainText(work.title);
 await detail.locator(".dialog-top button").click();await expect(detail).toHaveCount(0);
 const url=new URL(page.url());expect(url.pathname).toBe("/admin/works");expect(url.searchParams.get("q")).toBe("harbor");expect(url.searchParams.get("view")).toBe("list");expect(url.searchParams.has("work")).toBe(false);
});

test("login restores the original fragment after an anonymous redirect",async({page,context})=>{
 await session(context,{authenticated:false});await page.goto("/admin/works?q=harbor#collection");
 await expect(page.getByLabel("Username",{exact:true})).toBeVisible();
 await page.getByLabel("Username",{exact:true}).fill("review");await page.getByLabel("Password",{exact:true}).fill("test-password");
 await page.locator("button[type=submit]").click();await expect(page).toHaveURL(/\/admin\/works\?q=harbor#collection$/);
});

test("password visibility and the wordmark keep credentials and return state",async({page,context})=>{
 await session(context,{authenticated:false});await page.goto("/admin/login?redirect=%2Fadmin%2Fjobs%3Fscope%3Dattention");
 const password=page.getByLabel("Password",{exact:true});await password.fill("review-secret");
 await page.getByRole("button",{name:"Show password",exact:true}).click();await expect(password).toHaveAttribute("type","text");
 await page.getByRole("button",{name:"Hide password",exact:true}).click();await expect(password).toHaveAttribute("type","password");
 const before=page.url();await page.getByRole("link",{name:"Auto-Gallery",exact:true}).click();expect(page.url()).toBe(before);await expect(password).toHaveValue("review-secret");
});

test("expired business requests return to the exact protected URL",async({page,context})=>{
 const state=await session(context);
 await page.route("**/api/v1/search?**",route=>{state.expire();return json(route,{detail:"Expired"},401);});
 await page.goto("/admin/works?q=harbor#collection");await expect(page.getByLabel("Username",{exact:true})).toBeVisible();
 expect(new URL(page.url()).searchParams.get("redirect")).toBe("/admin/works?q=harbor#collection");
});


test("sign-in without a return URL lands on an available module",async({page,context})=>{
 await session(context,{authenticated:false,user:{...principal,is_admin:false,permissions:[]}});
 await page.goto("/admin/login");await page.getByLabel("Username",{exact:true}).fill("review");await page.getByLabel("Password",{exact:true}).fill("test-password");
 await page.locator("button[type=submit]").click();await expect(page).toHaveURL(/\/admin\/profile$/);
 await expect(page.locator(".layout-bottom-nav .gallery-more-trigger")).toBeVisible();
});


test("account popover closes on outside click and Escape with focus restored",async({page,context})=>{
 await session(context);await page.goto("/admin/works");
 const trigger=page.locator(".gallery-account-trigger");const menu=page.locator(".gallery-account-menu");
 await trigger.focus();await page.keyboard.press("Enter");await expect(menu).toBeVisible();await expect(trigger).toHaveAttribute("aria-expanded","true");
 await page.keyboard.press("Escape");await expect(menu).toHaveCount(0);await expect(trigger).toBeFocused();
 await trigger.click();await expect(menu).toBeVisible();await page.locator("main").click({position:{x:10,y:10}});await expect(menu).toHaveCount(0);
});


test("account popover stays within a narrow viewport",async({page,context})=>{
 await session(context);await page.setViewportSize({width:390,height:700});await page.goto("/admin/works");
 await page.locator(".gallery-account-trigger").click();const menu=page.locator(".gallery-account-menu");await expect(menu).toBeVisible();
 const bounds=await menu.boundingBox();expect(bounds).not.toBeNull();expect(bounds!.x).toBeGreaterThanOrEqual(0);expect(bounds!.x+bounds!.width).toBeLessThanOrEqual(390);
});


test("quick jump opened from more restores focus to more",async({page,context})=>{
 await session(context);await page.goto("/admin/works");const trigger=page.locator(".gallery-more-trigger");await trigger.click();
 await page.locator(".gallery-more-menu").getByRole("button",{name:"Quick jump",exact:true}).click();
 await expect(page.locator(".command-palette[open]")).toBeVisible();await page.keyboard.press("Escape");await expect(page.locator(".command-palette[open]")).toHaveCount(0);await expect(trigger).toBeFocused();
});


test("more navigation groups the permitted pages and account keeps only personal controls",async({page,context})=>{
 await session(context);await page.goto("/admin/works");
 await page.locator(".gallery-account-trigger").click();
 const account=page.locator(".gallery-account-menu");
 await expect(account.locator("a")).toHaveCount(1);await expect(account.locator("a")).toHaveAttribute("href","/admin/profile");
 await page.keyboard.press("Escape");
 const trigger=page.locator(".gallery-more-trigger"),menu=page.locator(".gallery-more-menu");
 await trigger.focus();await page.keyboard.press("Enter");await expect(menu).toBeVisible();
 for(const href of ["/admin","/admin/tags","/admin/upload","/admin/upload/danbooru","/admin/creators","/admin/subscriptions","/admin/discovery","/admin/scheduler","/admin/data-mgmt","/admin/system","/admin/settings","/admin/search","/admin/data-mgmt/curation","/admin/data-mgmt/dedup","/admin/settings/users"]) await expect(menu.locator(`a[href="${href}"]`)).toHaveCount(1);
 await expect(menu.getByRole("button",{name:"Quick jump"})).toBeVisible();
 await menu.getByRole("button",{name:"Notifications"}).click();
 await expect(page.getByRole("dialog",{name:"Recent Activity"})).toBeVisible();
 await page.keyboard.press("Escape");await expect(page.getByRole("dialog",{name:"Recent Activity"})).toHaveCount(0);await expect(menu).toBeVisible();
 await page.keyboard.press("Escape");await expect(trigger).toBeFocused();
});

test("more navigation respects permissions and fits a narrow screen",async({page,context})=>{
 await session(context,{user:{...principal,is_admin:false,permissions:["library"]}});await page.setViewportSize({width:390,height:700});await page.goto("/admin/works");
 await page.locator(".gallery-more-trigger").click();const menu=page.locator(".gallery-more-menu");await expect(menu).toBeVisible();
 await expect(menu.locator('a[href="/admin/tags"]')).toHaveCount(1);
 await expect(menu.locator('a[href="/admin/settings/users"]')).toHaveCount(0);await expect(menu.locator('a[href="/admin/upload"]')).toHaveCount(0);
 const bounds=await menu.boundingBox();expect(bounds).not.toBeNull();expect(bounds!.x).toBeGreaterThanOrEqual(0);expect(bounds!.x+bounds!.width).toBeLessThanOrEqual(390);
 await menu.locator('a[href="/admin/tags"]').click();await expect(page).toHaveURL(/\/admin\/tags$/);await expect(menu).toHaveCount(0);
 await page.locator(".gallery-more-trigger").click();await expect(page.locator('.gallery-more-menu a[href="/admin/tags"]')).toHaveAttribute("aria-current","page");
});


test("switching task scope from an empty result shows loading until the new result arrives",async({page,context})=>{
 await session(context);await page.goto("/admin/jobs");
 const tabs=page.locator(".layout-task-tabs");await expect(tabs.locator("a").first()).toContainText("0");
 let release!:()=>void;const pending=new Promise<void>(resolve=>{release=resolve;});
 await page.route("**/api/v1/tasks?**",async route=>{await pending;await route.fallback();});
 await tabs.locator("a").nth(1).click();
 await expect(page.locator(".layout-jobs [role=status]")).toContainText(/loading/i);
 release();await expect(page.locator(".layout-jobs [role=status]")).toHaveCount(0);
});
