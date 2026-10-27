import {expect,test,type BrowserContext,type Route} from "@playwright/test";
const origin=process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000";
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
const when="2026-10-24T08:00:00Z";
const source={id:"repository-harbor",subscription_id:"subscription-harbor",source:"pixiv",source_creator_id:"408",source_url:"https://www.pixiv.net/users/408",is_enabled:true,is_repository:true,can_download:true,url_valid:true,auth_state:"healthy",auth_healthy:true,credential_state:"ready",latest_job:{id:"download-harbor",status:"complete",created_at:when}};
const job={id:"download-harbor",status:"complete",retry_count:0,recovered:false,created_at:when,updated_at:when};
async function setup(context:BrowserContext,options:{libraryOnly?:boolean;failDetail?:boolean;failGraph?:boolean}={}){
 const calls:Array<{path:string;method:string;query:string;body:any}>=[];let enabled=true,failedDetail=Boolean(options.failDetail),failedGraph=Boolean(options.failGraph);
 await context.addCookies([{name:"ag_session",value:"repository-fixture",url:origin},{name:"ag_csrf",value:"repository-csrf",url:origin}]);
 await context.addInitScript(()=>{localStorage.setItem("auto-gallery-lang","en");localStorage.setItem("auto-gallery-theme","light");});
 await context.route("**/api/v1/**",async route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();calls.push({path,method,query:url.search,body:request.postDataJSON()});
  if(path==="/api/v1/auth/me")return json(route,{id:31,username:"repository-fixture",is_admin:!options.libraryOnly,is_active:true,permissions:options.libraryOnly?["library"]:["library","subscriptions","system","tasks","curation"],preferences:{lang:"en",theme:"light"},must_change_password:false});
  if(path==="/api/v1/auth/ws-ticket")return json(route,{detail:"fixture websocket unavailable"},503);
  if(path==="/api/v1/notifications")return json(route,{items:[],total:0});
  if(path==="/api/v1/system/workbench")return json(route,{queue:{},scheduler:{},storage:{},attention:{},recent:{}});
  if(path==="/api/v1/system/scheduler-decisions")return json(route,{items:[],total:0});
  if(path==="/api/v1/repositories/repository-harbor")return failedDetail?json(route,{detail:"repository temporarily unavailable"},503):method==="DELETE"?json(route,{entity_ids:[source.id]}):json(route,{repository:{...source,is_enabled:enabled},creator:{id:"creator-harbor",name:"Harbor Archive"},subscription:{id:"subscription-harbor",name:"Harbor feed",schedule_mode:"inherit",sync_interval_hours:24},provider:{display_name:"Pixiv",normalized_url:source.source_url},work_total:14,recent_works:[{id:"work-harbor",title:"Harbor light",asset_count:1,posted_at:when}],sync_history:[job],recent_jobs:[job],active_jobs:[]});
  if(path==="/api/v1/repositories/repository-harbor/sync-now")return json(route,{status:"enqueued",message:"Sync queued"},202);
  if(path==="/api/v1/subscriptions/subscription-harbor/sources/repository-harbor"&&method==="PATCH"){enabled=Boolean(request.postDataJSON().is_enabled);return json(route,{...source,is_enabled:enabled});}
  if(path==="/api/v1/repositories/repository-harbor/deletion-preview")return json(route,{mode:"permanent",active_task_count:0,can_delete_files:true,affected_work_count:1,exclusive_work_count:1,shared_work_count:0,exclusive_asset_count:1});
  if(path==="/api/v1/repositories/repository-harbor/tags")return json(route,{items:[{id:"tag-harbor",normalized_name:"harbor",category:"general",usage_count:2}],total:51});
  if(path==="/api/v1/repositories/repository-harbor/curation-graph")return failedGraph?json(route,{detail:"graph temporarily unavailable"},503):json(route,{nodes:[{id:"commit-harbor",message:"Imported harbor",trigger:"source_synced",occurred_at:when,status:"active",is_baseline:false,stats:{work_count:1},thumbnails:[],changes_summary:[]}],edges:[],total:1});
  if(path.endsWith("/gitllery/status"))return json(route,{repositories:[{repository_id:source.id,creator_dir:"Harbor Archive",projection_mode:"shadow",object_integrity_ok:true,behind:0}]});
  if(path.endsWith("/gitllery/log"))return json(route,{entries:[{commit:"harbor-commit",message:"Harbor projection",occurred_at:when}],total:1});
  if(path==="/api/v1/subscriptions/subscription-harbor")return json(route,{id:"subscription-harbor",creator_id:"creator-harbor",name:"Harbor feed",sync_enabled:true,is_active:true,sync_interval_hours:24});
  if(path.endsWith("/sources"))return json(route,[]);
  if(path==="/api/v1/download-jobs")return json(route,[]);
  return json(route,{items:[],total:0});
 });return{calls,recoverDetail:()=>{failedDetail=false;},recoverGraph:()=>{failedGraph=false;}};
}
test.describe.configure({timeout:60000});test.use({actionTimeout:10000});
test("repository overview preserves sync toggle links and exact deletion confirmation",async({page,context})=>{
 const state=await setup(context);await page.goto("/admin/subscriptions/repositories/repository-harbor");await expect(page.getByRole("heading",{level:1,name:"pixiv/408",exact:true})).toBeVisible();
 await page.locator(".business-heading-actions").getByRole("button",{name:"Sync now",exact:true}).click();await expect.poll(()=>state.calls.filter(c=>c.path.endsWith("/sync-now")&&c.method==="POST").length).toBe(1);
 await page.locator(".business-heading-actions").getByRole("button",{name:"Disable",exact:true}).click();await expect.poll(()=>state.calls.find(c=>c.method==="PATCH")?.body).toEqual({is_enabled:false});await expect(page.locator(".business-heading-actions").getByRole("button",{name:"Enable",exact:true})).toBeVisible();
 await expect(page.getByRole("link",{name:"Open subscription",exact:true})).toHaveAttribute("href","/admin/subscriptions/subscription-harbor");
 await page.locator(".business-heading-actions").getByRole("button",{name:"Remove from my sources",exact:true}).click();const dialog=page.getByRole("dialog"),confirm=dialog.getByRole("button",{name:"Confirm",exact:true});await expect(confirm).toBeDisabled();await dialog.getByRole("textbox").fill("pixiv/408");await dialog.getByRole("checkbox").check();await expect(confirm).toBeEnabled();await confirm.click();await expect.poll(()=>state.calls.find(c=>c.method==="DELETE")?.query).toBe("?delete_files=true");await expect(page).toHaveURL(/subscriptions\/subscription-harbor/);
});
test("repository content history and settings remain reachable with recoverable graph failures",async({page,context})=>{
 const state=await setup(context,{failGraph:true});await page.goto("/admin/subscriptions/repositories/repository-harbor?tab=content");await expect(page.getByText("Harbor light",{exact:true})).toBeVisible();await expect(page.getByTestId("tag-bubble-chart")).toBeVisible();await page.getByRole("button",{name:"Next",exact:true}).click();await expect.poll(()=>state.calls.some(c=>c.path.endsWith("/tags")&&c.query.includes("offset=50"))).toBe(true);
 await page.locator(".repository-tabs").getByRole("button",{name:/Sync history/}).click();await expect(page).toHaveURL(/tab=history/);await expect(page.getByText("graph temporarily unavailable",{exact:true})).toBeVisible();state.recoverGraph();await page.getByRole("button",{name:"Retry",exact:true}).click();await expect(page.getByRole("button",{name:/Imported harbor/})).toBeVisible();await page.getByRole("button",{name:/Imported harbor/}).click();await expect(page.getByRole("link",{name:"Open commit",exact:true})).toHaveAttribute("href",/commit=commit-harbor/);await expect(page.getByText("Harbor projection",{exact:true})).toBeVisible();
 await page.getByRole("combobox",{name:"Repository curation graph",exact:true}).click();await page.getByRole("option",{name:"Trash",exact:true}).click();await expect.poll(()=>state.calls.some(c=>c.path.endsWith("/curation-graph")&&c.query.includes("trigger=work_trash"))).toBe(true);
 await page.locator(".repository-tabs").getByRole("button",{name:"Settings",exact:true}).click();await expect(page.locator(".repository-config")).toContainText(source.source_url);
});
test("library-only repository readers retain sync and read recovery without management controls",async({page,context})=>{
 const state=await setup(context,{libraryOnly:true,failDetail:true});await page.goto("/admin/subscriptions/repositories/repository-harbor");await expect(page.getByText("repository temporarily unavailable",{exact:true})).toBeVisible();state.recoverDetail();await page.getByRole("button",{name:"Retry",exact:true}).click();await expect(page.getByRole("heading",{name:"pixiv/408",exact:true})).toBeVisible();await expect(page.getByRole("button",{name:/^(Disable|Enable|Remove from my sources)$/})).toHaveCount(0);expect(state.calls.some(c=>c.path==="/api/v1/system/scheduler-decisions")).toBe(false);await expect(page.locator(".business-heading-actions").getByRole("button",{name:"Sync now",exact:true})).toBeEnabled();
});
