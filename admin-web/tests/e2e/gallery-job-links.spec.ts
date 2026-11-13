import {expect,test,type BrowserContext,type Route} from "@playwright/test";
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
const user={id:82,username:"job-review",is_admin:true,is_active:true,permissions:["tasks"],preferences:{lang:"en",theme:"light"},must_change_password:false};
const record={id:"download-original",status:"failed",source:"manual",source_url:"file:///review/source.png",created_at:"2026-10-08T00:00:00Z",updated_at:"2026-10-08T00:00:00Z",retry_count:0,available_actions:["delete","retry","repeat_sync"],disabled_reasons:{}};
async function setup(context:BrowserContext,handler?:(route:Route,path:string)=>Promise<unknown>|undefined){
 await context.addCookies([{name:"ag_session",value:"job-session",url:process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000"}]);
 await context.addInitScript(()=>{localStorage.setItem("auto-gallery-lang","en");localStorage.setItem("auto-gallery-theme","light");});
 await context.route("**/api/v1/**",async route=>{
  const path=new URL(route.request().url()).pathname;const handled=handler?.(route,path);if(handled!==undefined)return handled;
  if(path==="/api/v1/auth/me")return json(route,user);
  if(path==="/api/v1/system/workbench")return json(route,{updated_at:"2026-10-08T00:00:00Z",queue:{default:0,scheduled:0,failed:0,active_download_count:0,active_import_count:0,failed_download_count:0,failed_import_count:0,stale_download_count:0,stale_import_count:0,stale_count:0},scheduler:{enabled:true,mode:"interval",timezone:"UTC",scan_interval_minutes:60},storage:{},health:{},attention:{},recent:{}});

  if(path==="/api/v1/tasks")return json(route,{total:0,items:[],summary:{all:0,attention:0,running:0,waiting:0}});
  if(path==="/api/v1/download-jobs/download-original")return json(route,record);
  if(path==="/api/v1/download-jobs/download-original/imports")return json(route,[]);
  if(path==="/api/v1/notifications"||path==="/api/v1/import-jobs")return json(route,{total:0,items:[]});
  if(path==="/api/v1/download-jobs")return json(route,[]);
  return json(route,{});
 });
}

test("legacy tabs can change back to all kinds without retaining hidden fallbacks",async({page,context})=>{
 await setup(context);await page.goto("/admin/jobs?tab=downloads&view=attention&page=2");
 await page.getByRole("link",{name:"All tasks",exact:true}).click();
 await expect.poll(()=>new URL(page.url()).searchParams.has("tab")).toBe(false);
 const query=new URL(page.url()).searchParams;expect(query.has("kind")).toBe(false);expect(query.has("page")).toBe(false);expect(query.get("scope")).toBeNull();
});

test("download deep links load the correct record and require deletion confirmation",async({page,context})=>{
 let deletes=0;const wrong:string[]=[];
 await setup(context,(route,path)=>{
  if(path==="/api/v1/tasks/download-original"){wrong.push(path);return json(route,{detail:"wrong identity"},404);}
  if(path==="/api/v1/download-jobs/download-original"&&route.request().method()==="DELETE"){deletes++;return json(route,{status:"deleted"});}
 });
 await page.goto("/admin/jobs?tab=downloads&job=download-original");const dialog=page.getByRole("dialog");await expect(dialog).toContainText(record.source_url);
 await dialog.getByRole("button",{name:/^Delete$/i}).click();await expect(page.getByRole("dialog")).toHaveCount(2);expect(deletes).toBe(0);
 await page.getByRole("dialog").last().getByRole("button",{name:/^Confirm$/i}).click();await expect.poll(()=>deletes).toBe(1);
 await expect.poll(()=>new URL(page.url()).searchParams.has("job")).toBe(false);expect(wrong).toEqual([]);
});

test("legacy import filters use the foreign-key API and record links keep their identity",async({page,context})=>{
 const queries:URLSearchParams[]=[];const wrong:string[]=[];
 const imported={id:"import-child",download_job_id:"download-parent",status:"failed",created_at:record.created_at,updated_at:record.updated_at,error_log:"Review failure",available_actions:["retry"],disabled_reasons:{}};
 await setup(context,(route,path)=>{
  if(path==="/api/v1/import-jobs"){queries.push(new URL(route.request().url()).searchParams);return json(route,{total:25,items:[imported]});}
  if(path==="/api/v1/import-jobs/import-child")return json(route,imported);
  if(path==="/api/v1/tasks/import-child"){wrong.push(path);return json(route,{detail:"wrong identity"},404);}
 });
 await page.goto("/admin/jobs?tab=imports&download_job_id=download-parent&page=2");
 await expect(page.getByRole("link",{name:"import-child",exact:true})).toBeVisible();expect(queries[0].get("download_job_id")).toBe("download-parent");expect(queries[0].get("offset")).toBe("12");
 await page.getByRole("link",{name:"import-child",exact:true}).click();await expect(page.getByRole("dialog")).toContainText("Review failure");expect(wrong).toEqual([]);
 await page.getByRole("dialog").locator(".dialog-top button").click();expect(new URL(page.url()).searchParams.get("download_job_id")).toBe("download-parent");
});

test("repository-filtered downloads retain exact filters across cached pagination",async({page,context})=>{
 const queries:URLSearchParams[]=[];const records=Array.from({length:25},(_,index)=>({...record,id:"record-"+index,source_url:"source-"+index,available_actions:[]}));
 await setup(context,(route,path)=>{if(path==="/api/v1/download-jobs"){const query=new URL(route.request().url()).searchParams;queries.push(query);const offset=Number(query.get("offset")||0);const limit=Number(query.get("limit")||50);return json(route,records.slice(offset,offset+limit));}});
 await page.goto("/admin/jobs?tab=downloads&subscription_source_id=repo-original");await expect(page.getByRole("link",{name:"source-0",exact:true})).toBeVisible();
 expect(queries[0].get("subscription_source_id")).toBe("repo-original");
 await page.getByRole("button",{name:/^Next$/i}).click();await expect(page.getByRole("link",{name:"source-12",exact:true})).toBeVisible();
 const query=new URL(page.url()).searchParams;expect(query.get("subscription_source_id")).toBe("repo-original");expect(query.get("kind")).toBe("download");expect(queries).toHaveLength(2);
 expect(queries[0].get("subscription_source_id")).toBe("repo-original");expect(queries[1].get("subscription_source_id")).toBe("repo-original");
});


test("related downloads preserve raw states and filter by exact domain values",async({page,context})=>{
 const queries:URLSearchParams[]=[];let tasks=0;
 const records=['downloading','downloaded','importing','complete'].map(status=>({...record,id:status,source_url:'source-'+status,status,available_actions:[]}));
 await setup(context,(route,path)=>{
  if(path==='/api/v1/tasks'){tasks++;return json(route,{total:0,items:[],summary:{all:0,attention:0,running:0,waiting:0}});}
  if(path==='/api/v1/download-jobs'){const query=new URL(route.request().url()).searchParams;queries.push(query);const selected=query.get('status') || query.get('q')?.match(/status:(\w+)/)?.[1];return json(route,selected?records.filter(row=>row.status===selected):records);}
 });
 await page.goto('/admin/jobs?tab=downloads&subscription_source_id=repo-original');
 const downloaded=page.locator('[data-task-id="downloaded"]');await expect(downloaded).toContainText('Downloaded');await expect(downloaded.locator('.status-running')).toBeVisible();await expect(downloaded.locator('progress')).toHaveCount(0);
 await page.getByRole('combobox',{name:'Task status',exact:true}).click();await expect(page.getByRole('option',{name:'Attention',exact:true})).toHaveCount(0);await page.getByRole('option',{name:'Downloaded',exact:true}).click();
 await expect(page.getByRole('link',{name:'source-downloaded',exact:true})).toBeVisible();await expect(page.locator('[data-task-id]')).toHaveCount(1);expect(queries.at(-1)?.get('status')).toBe('downloaded');expect(queries.at(-1)?.get('subscription_source_id')).toBe('repo-original');
 await page.getByRole('link',{name:'All tasks',exact:true}).click();await expect.poll(()=>tasks).toBe(1);expect(new URL(page.url()).searchParams.has('subscription_source_id')).toBe(false);
});

test("unsupported unified scopes on related records require an explicit scope change",async({page,context})=>{
 let requests=0;await setup(context,(route,path)=>{if(path==='/api/v1/download-jobs'){requests++;return json(route,[record]);}});
 await page.goto('/admin/jobs?tab=downloads&subscription_source_id=repo-original&view=attention');
 await expect(page.getByText('Attention, Running and Waiting scopes are unavailable for related records.',{exact:false})).toBeVisible();expect(requests).toBe(0);
 await page.getByRole('button',{name:'View related records',exact:true}).click();await expect(page.getByRole('link',{name:record.source_url,exact:true})).toBeVisible();expect(requests).toBe(1);expect(new URL(page.url()).searchParams.has('scope')).toBe(false);expect(new URL(page.url()).searchParams.has('view')).toBe(false);
});


test("download and import tabs use their authoritative lists and command surfaces",async({page,context})=>{
 const download={...record,id:"download-tab",source:"pixiv",status:"failed",available_actions:["retry","delete"],source_url:"https://pixiv.example/tab"};
 const imported={id:"import-tab",download_job_id:"download-tab",status:"failed",created_at:record.created_at,updated_at:record.updated_at,error_log:"Import failed",available_actions:["retry","delete"],disabled_reasons:{}};
 const requests:string[]=[];let retryPath="";
 await setup(context,(route,path)=>{
  if(path==="/api/v1/download-jobs"){requests.push("downloads");return json(route,[download]);}
  if(path==="/api/v1/download-jobs/download-tab"){return json(route,download);}
  if(path==="/api/v1/download-jobs/download-tab/retry"){retryPath=path;return json(route,{job_id:"download-tab",status:"enqueued"});}
  if(path==="/api/v1/import-jobs"){requests.push("imports");return json(route,{total:1,items:[imported]});}
  if(path==="/api/v1/import-jobs/import-tab"){return json(route,imported);}
  if(path==="/api/v1/import-jobs/import-tab/retry"){retryPath=path;return json(route,{status:"enqueued"});}
 });
 await page.goto("/admin/jobs?tab=downloads");await expect(page.getByRole("link",{name:download.source_url,exact:true})).toBeVisible();expect(requests).toContain("downloads");
 await page.getByRole("button",{name:/Retry/i}).first().click();await expect.poll(()=>retryPath).toBe("/api/v1/download-jobs/download-tab/retry");
 await page.goto("/admin/jobs?tab=imports");await expect(page.getByRole("link",{name:imported.id,exact:true})).toBeVisible();expect(requests).toContain("imports");
 await page.getByRole("button",{name:/Retry/i}).first().click();await expect.poll(()=>retryPath).toBe("/api/v1/import-jobs/import-tab/retry");
});


test("import pagination uses server page items without a second client slice",async({page,context})=>{
 const queries:URLSearchParams[]=[];const records=Array.from({length:25},(_,index)=>({...record,id:"import-page-"+index,download_job_id:"download-parent",source_url:"import-source-"+index,available_actions:[]}));
 await setup(context,(route,path)=>{if(path==="/api/v1/import-jobs"){const query=new URL(route.request().url()).searchParams;queries.push(query);const offset=Number(query.get("offset")||0);const limit=Number(query.get("limit")||12);return json(route,{total:records.length,items:records.slice(offset,offset+limit)});}});
 await page.goto("/admin/jobs?tab=imports&download_job_id=download-parent&page=2");
 await expect(page.getByRole("link",{name:"import-source-12",exact:true})).toBeVisible();await expect(page.getByRole("link",{name:"import-source-0",exact:true})).toHaveCount(0);expect(queries[0].get("offset")).toBe("12");expect(queries[0].get("limit")).toBe("12");
});


test("task search canonicalizes through assist without changing record-domain free text",async({page,context})=>{
 let assist=0;let assistRaw="";await setup(context,(route,path)=>{
  if(path==="/api/v1/search/assist"){assist++;const body=route.request().postDataJSON() as {before_cursor?:string;query?:string;q?:string};const raw=body.before_cursor || body.query || body.q || "";if(!assistRaw)assistRaw=raw;if(raw==="raw-task-query")return json(route,{query:raw,canonical_query:"kind:admin raw-task-query",parsed:{raw,canonical:"kind:admin raw-task-query",scope:"tasks",targets:["tasks"],tokens:[]},diagnostics:[],suggestions:[],catalog:[]});return json(route,{query:raw,canonical_query:raw,parsed:{raw,canonical:raw,scope:"tasks",targets:["tasks"],tokens:[]},diagnostics:[],suggestions:[],catalog:[]});}
 });
 await page.goto("/admin/jobs");const taskSearch=page.getByRole("searchbox",{name:"Search",exact:true});await taskSearch.fill("raw-task-query");await taskSearch.press("Enter");await expect.poll(()=>assist).toBeGreaterThan(0);expect(assistRaw).toBe("raw-task-query");await expect.poll(()=>new URL(page.url()).searchParams.get("q")).toBe("kind:admin raw-task-query");
 assist=0;await page.goto("/admin/jobs?tab=downloads&q=source%20free%20text");await page.waitForTimeout(500);expect(assist).toBe(0);
});


test("raw record selection exposes the batch action and keeps the download surface",async({page,context})=>{
 const first={...record,id:"download-batch-1",status:"failed",available_actions:["retry"],source_url:"source-batch-1"};
 const second={...record,id:"download-batch-2",status:"failed",available_actions:["retry"],source_url:"source-batch-2"};let batchBody:unknown=null;
 await setup(context,(route,path)=>{if(path==="/api/v1/download-jobs")return json(route,[first,second]);if(path==="/api/v1/download-jobs/batch"){batchBody=route.request().postDataJSON();return json(route,{succeeded:2,failed:0,errors:[]});}});
 await page.goto("/admin/jobs?tab=downloads");await expect(page.getByRole("link",{name:"source-batch-1",exact:true})).toBeVisible();await page.getByRole("button",{name:/Select tasks/i}).click();await expect(page.locator(".layout-selection-bar")).toBeVisible();
 await page.getByRole("checkbox",{name:/download-batch-1/}).check();await page.getByRole("combobox",{name:/Batch action/i}).click();await page.getByRole("option",{name:/Retry/i}).click();await page.getByRole("button",{name:/Apply/i}).click();await page.getByRole("button",{name:/Confirm/i}).click();await expect.poll(()=>batchBody).toEqual({ids:["download-batch-1"],action:"retry"});
});
