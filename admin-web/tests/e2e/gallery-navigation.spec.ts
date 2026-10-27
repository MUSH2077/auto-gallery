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
 await page.locator(".gallery-account summary").click();await page.locator(".gallery-account-menu a[href='/admin/profile']").click();
 await expect(page).toHaveURL(/\/admin\/profile$/);await expect(page.locator(".layout-bottom-nav a[aria-current]")).toHaveCount(0);
 await page.locator(".gallery-account summary").click();await expect(page.locator(".gallery-account-menu a[href='/admin/profile']")).toHaveAttribute("aria-current","page");
 await page.keyboard.press("Escape");await expect(page.locator(".gallery-account summary")).toBeFocused();
 expect(state.reads()).toBe(1);await expect(page.locator("main#main-content")).toHaveCount(1);
});

test("account-only access has no empty primary navigation",async({page,context})=>{
 await session(context,{user:{...principal,is_admin:false,permissions:[]}});await page.goto("/admin/profile");
 await expect(page.locator(".layout-brand-wordmark")).toBeVisible();await expect(page.locator(".layout-bottom-nav")).toHaveCount(0);
 await page.locator(".gallery-account summary").click();await expect(page.locator(".gallery-account-menu a[href='/admin/profile']")).toBeVisible();
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
 await expect(page.locator(".layout-bottom-nav")).toHaveCount(0);
});
