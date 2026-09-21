import { expect, test, type Route } from "@playwright/test";
test.describe.configure({ timeout: 60_000 });
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
const me={id:1,username:"tasks",display_name:"Tasks",is_admin:false,is_active:true,permissions:["tasks"],modules:{tasks:true},preferences:{},nsfw_visible:true,upload_used_bytes:0,must_change_password:false};
const workbench={updated_at:"2026-09-08T00:00:00Z",queue:{default:0,scheduled:0,failed:0,active_download_count:0,active_import_count:0,failed_download_count:1,failed_import_count:0,stale_download_count:0,stale_import_count:0,stale_count:0},scheduler:{enabled:true,mode:"interval",timezone:"UTC",scan_interval_minutes:60},storage:{disk_total_bytes:1,disk_free_bytes:1,disk_used_bytes:0,risk_level:"ok"},health:{},attention:{auth_unhealthy_count:0,failed_download_count:1,failed_import_count:0,stale_job_count:0,low_disk_warning:false,scheduler_disabled_warning:false},recent:{download_jobs:[],import_jobs:[],works:[],successful_syncs:[]}};
test("download actions follow capabilities and repeat validates exact accepted identity",async({context,page})=>{
 await context.addCookies([{name:"ag_token",value:"fixture",domain:"127.0.0.1",path:"/"}]); await context.addInitScript(()=>{localStorage.setItem("ag_token","fixture");localStorage.setItem("auto-gallery-lang","en")});
 const oldId="00000000-0000-4000-8000-000000000001", newId="00000000-0000-4000-8000-000000000002", taskId="00000000-0000-4000-8000-000000000003"; let repeatBody:any=null;
 await context.route("**/api/v1/**",async route=>{const req=route.request(),u=new URL(req.url()),p=u.pathname;if(p==="/api/v1/auth/me")return json(route,me);if(p==="/api/v1/system/workbench")return json(route,workbench);if(p==="/api/v1/download-jobs")return json(route,[{id:oldId,subscription_id:"sub",source:"pixiv",source_url:"https://pixiv.net/users/1",status:"complete",retry_count:0,created_at:"2026-09-08T00:00:00Z",updated_at:"2026-09-08T00:00:00Z",available_actions:["repeat_sync"],disabled_reasons:{retry:"completed_download_requires_repeat_sync"}}]);if(p===`/api/v1/download-jobs/${oldId}/repeat-sync`){repeatBody=req.postDataJSON();return json(route,{task_id:taskId,job_id:newId,previous_job_id:oldId,request_id:repeatBody.request_id,action:"repeat_sync",status:"enqueued"},202)}if(p==="/api/v1/tasks")return json(route,{total:0,items:[]});if(p==="/api/v1/import-jobs")return json(route,{total:0,items:[]});return json(route,{});});
 await page.goto("/admin/jobs?tab=downloads"); await expect(page.getByRole("button",{name:"Retry"})).toHaveCount(0); await page.getByRole("button",{name:"Repeat sync"}).click(); await expect(page.getByRole("dialog")).toContainText("preserving the original"); await page.getByRole("button",{name:"Confirm"}).click(); await expect.poll(()=>repeatBody?.request_id).toMatch(/^[0-9a-f-]{36}$/); await expect(page).toHaveURL(new RegExp(`job=${newId}`));
});

test("dashboard exposes repeat sync from the returned capability and adopts the accepted job",async({context,page})=>{
 await context.addCookies([{name:"ag_token",value:"fixture",domain:"127.0.0.1",path:"/"}]); await context.addInitScript(()=>{localStorage.setItem("ag_token","fixture");localStorage.setItem("auto-gallery-lang","en")});
 const oldId="00000000-0000-4000-8000-000000000011",newId="00000000-0000-4000-8000-000000000012",taskId="00000000-0000-4000-8000-000000000013";let requestId="";
 const dashboard={...workbench,recent:{...workbench.recent,download_jobs:[{id:oldId,subscription_id:"sub",source:"pixiv",source_url:"https://pixiv.net/users/1",creator_name:"Repeatable creator",status:"complete",retry_count:0,created_at:"2026-09-08T00:00:00Z",updated_at:"2026-09-08T00:00:00Z",available_actions:["repeat_sync"],disabled_reasons:{retry:"completed_download_requires_repeat_sync"}}]}};
 await context.route("**/api/v1/**",async route=>{const req=route.request(),p=new URL(req.url()).pathname;if(p==="/api/v1/auth/me")return json(route,me);if(p==="/api/v1/system/workbench")return json(route,dashboard);if(p===`/api/v1/download-jobs/${oldId}/repeat-sync`){requestId=req.postDataJSON().request_id;return json(route,{task_id:taskId,job_id:newId,previous_job_id:oldId,request_id:requestId,action:"repeat_sync",status:"enqueued"},202)}return json(route,{});});
 await page.goto("/admin");await expect(page.getByRole("button",{name:"Repeat sync"})).toBeVisible();await page.getByRole("button",{name:"Repeat sync"}).click();await page.getByRole("button",{name:"Confirm"}).click();await expect.poll(()=>requestId).toMatch(/^[0-9a-f-]{36}$/);await expect(page).toHaveURL(new RegExp(`job=${newId}`));
});

test("jobs batch caller removes only confirmed success and retains local and server reasons", async ({ context, page }) => {
 await context.addCookies([{name:"ag_token",value:"fixture",domain:"127.0.0.1",path:"/"}]);
 await context.addInitScript(()=>{localStorage.setItem("ag_token","fixture");localStorage.setItem("auto-gallery-lang","en")});
 const ids=["11111111-0000-4000-8000-000000000001","22222222-0000-4000-8000-000000000002","33333333-0000-4000-8000-000000000003"];
 const task=(id:string,title:string,actions:string[],disabled_reasons:Record<string,string>={})=>({id,task_type:"download",title,status:"failed",created_at:"2026-09-12T00:00:00Z",updated_at:"2026-09-12T00:00:00Z",available_actions:actions,disabled_reasons});
 const items=[task(ids[0],"Confirmed success",["retry"]),task(ids[1],"Server refusal",["retry"]),task(ids[2],"Local refusal refusal",[],{retry:"retry_not_available"})];
 let retryCalls=0; const unhandled:string[]=[];
 await context.route("**/api/v1/**",async route=>{const req=route.request(),p=new URL(req.url()).pathname;
  if(p==="/api/v1/auth/me")return json(route,me);if(p==="/api/v1/auth/ws-ticket")return json(route,{detail:"fixture websocket unavailable"},503);if(p==="/api/v1/system/workbench")return json(route,workbench);
  if(p==="/api/v1/tasks")return json(route,{total:items.length,items});if(p==="/api/v1/search/assist")return json(route,{query:"",canonical_query:"",parsed:{tokens:[]},diagnostics:[],suggestions:[]});
  if(p===`/api/v1/tasks/${ids[0]}/retry`){retryCalls+=1;return json(route,{task_id:"accepted",status:"enqueued"},202)}
  if(p===`/api/v1/tasks/${ids[1]}/retry`){retryCalls+=1;return json(route,{detail:{reason:"authoritative_refusal"}},409)}
  if(p==="/api/v1/operations/overview")return json(route,{summary:{attention:0},items:[]});if(p==="/api/v1/system/scheduler-decisions")return json(route,{total:0,items:[],summary:{blocked_count:0}});
  unhandled.push(`${req.method()} ${p}`);return json(route,{detail:`Unhandled fixture request: ${p}`},501);
 });
 page.on("dialog",dialog=>void dialog.accept());
 await page.goto("/admin/jobs");
 await page.getByRole("button",{name:"Batch actions"}).click();
 for(const id of ids)await page.getByRole("checkbox",{name:new RegExp(id.slice(0,8))}).check();
 await page.getByRole("combobox",{name:"Batch action"}).selectOption("retry");
 await page.getByRole("button",{name:"Apply"}).click();
 await expect.poll(()=>retryCalls).toBe(2);
 await expect(page.getByRole("checkbox",{name:/11111111/})).not.toBeChecked();
 await expect(page.getByRole("checkbox",{name:/22222222/})).toBeChecked();
 await expect(page.getByRole("checkbox",{name:/33333333/})).toBeChecked();
 const alert=page.getByRole("alert").filter({hasText:"Some tasks were not completed"});
 await expect(alert).toContainText("authoritative_refusal");await expect(alert).toContainText("retry_not_available");
 expect(unhandled).toEqual([]);
});
