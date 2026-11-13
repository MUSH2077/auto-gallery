import {expect,test,type BrowserContext,type Route} from "@playwright/test";
const json=(route:Route,data:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(data)});
const workbench={updated_at:"2026-10-24T08:00:00Z",queue:{default:1,scheduled:2,failed:1,active_download_count:1,active_import_count:0,failed_download_count:1,failed_import_count:0,stale_count:0},scheduler:{enabled:true,mode:"interval",timezone:"Asia/Shanghai",scan_interval_minutes:30},storage:{disk_total_bytes:10000,disk_free_bytes:5000,disk_used_bytes:5000,disk_free_percent:50,risk_level:"ok"},health:{backend:"up",postgres:"up",redis:"up",meilisearch:"up","gallery-dl":"up"},attention:{auth_unhealthy_count:0,auth_actionable_count:0,auth_disabled_or_unchecked_count:0,credential_issue_count:0,failed_download_count:1,failed_import_count:0,stale_job_count:0,low_disk_warning:false,scheduler_disabled_warning:false},recent:{works:[{id:"work-harbor",title:"Harbor light",thumbnail_asset_id:null,creator_name:"Harbor Archive",source:"manual",created_at:"2026-10-24T07:00:00Z"}],download_jobs:[{id:"download-harbor",subscription_id:"subscription-harbor",source:"manual",source_url:"manual://harbor",creator_name:"Harbor Archive",status:"failed",available_actions:["retry","repeat_sync"],created_at:"2026-10-24T07:00:00Z"}],import_jobs:[{id:"import-harbor",download_job_id:"download-harbor",creator_name:"Import Archive",status:"running",progress_works_done:1,progress_works_total:3,available_actions:[],created_at:"2026-10-24T06:00:00Z"}],successful_syncs:[]}};
async function setup(context:BrowserContext,options:{permissions?:string[];fail?:boolean;queue?:Partial<typeof workbench.queue>;attention?:Partial<typeof workbench.attention>}={}){
 const calls:Array<{path:string;query:string;method:string;body:any}>=[];let failed=Boolean(options.fail),refreshes=0;
 await context.addCookies([{name:"ag_session",value:"dashboard-test",url:process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000"},{name:"ag_csrf",value:"dashboard-csrf",url:process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000"}]);
 await context.addInitScript(()=>{localStorage.setItem("auto-gallery-lang","en");localStorage.setItem("auto-gallery-theme","light");});
 await context.route("**/api/v1/**",async route=>{const url=new URL(route.request().url()),path=url.pathname,method=route.request().method(),body=route.request().postDataJSON();calls.push({path,query:url.search,method,body});
  if(path==="/api/v1/auth/me")return json(route,{id:94,username:"dashboard-test",is_admin:false,is_active:true,permissions:options.permissions||["system","tasks","library"],preferences:{lang:"en",theme:"light"},must_change_password:false});
  if(path==="/api/v1/notifications")return json(route,{total:0,items:[]});
  if(path==="/api/v1/auth/ws-ticket")return json(route,{detail:"fixture websocket unavailable"},503);
  if(path==="/api/v1/system/workbench"){if(failed)return json(route,{detail:"workbench unavailable"},503);if(url.searchParams.get("refresh"))refreshes++;return json(route,{...workbench,queue:{...workbench.queue,...options.queue},attention:{...workbench.attention,...options.attention},updated_at:refreshes?"2026-10-24T09:00:00Z":workbench.updated_at});}
  if(path==="/api/v1/download-jobs/retry-all")return json(route,{total_matched:2,succeeded:1,failed:1,errors:[{id:"download-failed",error:"source denied"}]});
  if(path==="/api/v1/download-jobs/download-harbor/retry"){await new Promise(r=>setTimeout(r,150));return json(route,{job_id:"download-harbor",status:"enqueued"});}
  if(path==="/api/v1/download-jobs/download-harbor/repeat-sync")return json(route,{job_id:"download-new",task_id:"task-new",previous_job_id:"download-harbor",request_id:body.request_id,action:"repeat_sync",status:"enqueued"},202);
  if(path==="/api/v1/download-jobs")return json(route,[]);
  if(path==="/api/v1/tasks"||path==="/api/v1/import-jobs")return json(route,{items:[],total:0,summary:{all:0,attention:0,running:0,waiting:0}});
  return json(route,{});
 });return{calls,recover:()=>{failed=false;}};
}
test.use({actionTimeout:10000});
test("dashboard renders approved typography refreshes live summary and links raw record identities",async({page,context})=>{
 const state=await setup(context);await page.goto("/admin");await expect(page.getByRole("heading",{name:"System Dashboard",exact:true})).toBeVisible();await expect(page.locator('[data-activity-id="download-harbor"] a')).toHaveAttribute("href","/admin/jobs?records=download&job=download-harbor");await expect(page.locator('[data-activity-id="import-harbor"] a')).toHaveAttribute("href","/admin/jobs?records=import&import_job=import-harbor");
 await page.getByRole("button",{name:"Refresh",exact:true}).click();await expect.poll(()=>state.calls.some(c=>c.path==="/api/v1/system/workbench"&&c.query.includes("refresh=true"))).toBe(true);await expect(page.locator('.business-dashboard-refresh time')).toHaveAttribute("datetime","2026-10-24T09:00:00Z");
});
test("dashboard status links preserve task q filters for failed, stale, and failed-import work",async({page,context})=>{
 const state=await setup(context,{queue:{failed_download_count:1,failed_import_count:0,stale_count:1},attention:{failed_download_count:0,failed_import_count:1,stale_job_count:1}});
 for(const [href,expected] of [
  ["/admin/jobs?q=status%3Afailed","status:failed"],
  ["/admin/jobs?q=status%3Astale","status:stale"],
  ["/admin/jobs?q=kind%3Aimport+status%3Afailed","kind:import status:failed"],
 ] as const){
  await page.goto("/admin");
  await page.locator(`[href="${href}"]`).first().click();
  await expect(page).toHaveURL(new RegExp(`/admin/jobs\\?q=${encodeURIComponent(expected).replace(/%20/g,"\\+")}`));
  await expect.poll(()=>state.calls.some(call=>call.path==="/api/v1/tasks"&&new URLSearchParams(call.query).get("q")===expected)).toBe(true);
 }
});test("dashboard retry locks its row and repeat asks for native confirmation before submitting identity",async({page,context})=>{
 const state=await setup(context);await page.goto("/admin");const row=page.locator('[data-activity-id="download-harbor"]');await row.getByRole("button",{name:"Retry",exact:true}).click();await expect.poll(()=>state.calls.filter(c=>c.path.endsWith("/download-harbor/retry")).length).toBe(1);await expect(row.getByRole("button",{name:"Retry",exact:true})).toBeEnabled();
 await row.getByRole("button",{name:/Repeat sync/i}).click();await expect(page.locator("dialog[open]")).toHaveCount(1);expect(state.calls.filter(c=>c.path.endsWith("/repeat-sync"))).toHaveLength(0);await page.getByRole("dialog").getByRole("button",{name:"Confirm",exact:true}).click();await expect.poll(()=>state.calls.filter(c=>c.path.endsWith("/repeat-sync")).length).toBe(1);expect(state.calls.find(c=>c.path.endsWith("/repeat-sync"))?.body.request_id).toMatch(/^[0-9a-f-]{36}$/);await expect(page).toHaveURL(/records=download|tab=downloads/);await expect(page).toHaveURL(/job=download-new/);
});
test("dashboard bulk retry keeps HTTP success with individual failures visible",async({page,context})=>{
 await setup(context);await page.goto("/admin");await page.getByRole("button",{name:/Retry failed downloads/i}).click();await expect(page.getByRole("log")).toContainText("source denied");
});
test("system-only dashboard readers never see forbidden library or task actions",async({page,context})=>{
 const state=await setup(context,{permissions:["system"]});await page.goto("/admin");await expect(page.locator('.business-dashboard-status')).toBeVisible();await expect(page.getByRole("button",{name:/Retry|Repeat/i})).toHaveCount(0);await expect(page.locator('.business-dashboard-works')).toHaveCount(0);expect(state.calls.some(c=>c.path==="/api/v1/download-jobs"||c.path==="/api/v1/tasks")).toBe(false);
});
test("dashboard query failures recover through the visible retry control",async({page,context})=>{
 const state=await setup(context,{fail:true});await page.goto("/admin");await expect(page.getByText("workbench unavailable",{exact:true})).toBeVisible();state.recover();await page.getByRole("button",{name:/Retry/i}).click();await expect(page.locator('.business-dashboard-status')).toBeVisible();
});
