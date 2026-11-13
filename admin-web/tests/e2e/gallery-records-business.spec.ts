import { expect, test, type BrowserContext, type Route } from "@playwright/test";

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const user = { id: 91, username: "records-review", is_admin: true, is_active: true, permissions: ["tasks", "system"], modules: { tasks: true, system: true }, preferences: { lang: "en", theme: "light" }, nsfw_visible: true, must_change_password: false };
const baseJob = { status: "failed", source: "pixiv", created_at: "2026-10-08T00:00:00Z", updated_at: "2026-10-08T00:00:00Z", retry_count: 0, available_actions: ["retry", "delete"], disabled_reasons: {} };

async function setup(context: BrowserContext, handler?: (route: Route, path: string) => Promise<unknown> | undefined) {
  const origin = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000";
  await context.addCookies([{ name: "ag_session", value: "records-session", url: origin }, { name: "ag_csrf", value: "records-csrf", url: origin }]);
  await context.addInitScript(() => { localStorage.setItem("auto-gallery-lang", "en"); localStorage.setItem("auto-gallery-theme", "light"); });
  await context.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const handled = handler?.(route, path);
    if (handled !== undefined) return handled;
    if (path === "/api/v1/auth/me") return json(route, user);
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "fixture websocket unavailable" }, 503);
    if (path === "/api/v1/notifications") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/system/workbench") return json(route, { queue: { default: 0, scheduled: 0, failed_download_count: 0, failed_import_count: 0, active_download_count: 0, active_import_count: 0, stale_count: 0 }, recent: { download_jobs: [], import_jobs: [], works: [], successful_syncs: [] }, attention: {} });
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [], summary: { all: 0, attention: 0, running: 0, waiting: 0 } });
    if (path === "/api/v1/search/assist") return json(route, { query: "", canonical_query: "", parsed: { tokens: [] }, diagnostics: [], suggestions: [], catalog: [] });
    if (path === "/api/v1/import-jobs") return json(route, { total: 0, items: [] });
    if (path === "/api/v1/download-jobs") return json(route, []);
    return json(route, {});
  });
}

test("TaskRun kind filters keep the task surface and scope semantics", async ({ page, context }) => {
  const taskQueries: URLSearchParams[] = []; let downloadListCalls = 0;
  await setup(context, (route, path) => {
    if (path === "/api/v1/tasks") { taskQueries.push(new URL(route.request().url()).searchParams); return json(route, { total: 1, items: [{ id: "task-download", title: "Task download", kind: "download", status: "running", created_at: baseJob.created_at, available_actions: [] }], summary: { all: 1, attention: 0, running: 1, waiting: 0 } }); }
    if (path === "/api/v1/download-jobs") { downloadListCalls += 1; return json(route, []); }
  });
  await page.goto("/admin/jobs?kind=download&scope=running");
  await expect(page.getByRole("link", { name: "Task download", exact: true })).toBeVisible();
  expect(taskQueries.at(-1)?.get("kind")).toBe("download"); expect(taskQueries.at(-1)?.get("scope")).toBe("running"); expect(downloadListCalls).toBe(0);
});

test("Import records use server pagination without dropping the second page", async ({ page, context }) => {
  const queries: URLSearchParams[] = []; const records = Array.from({ length: 25 }, (_, index) => ({ ...baseJob, id: `import-${index}`, download_job_id: "download-parent", source_url: `import-source-${index}` }));
  await setup(context, (route, path) => { if (path === "/api/v1/import-jobs") { const query = new URL(route.request().url()).searchParams; queries.push(query); const offset = Number(query.get("offset") || 0); const limit = Number(query.get("limit") || 12); return json(route, { total: records.length, items: records.slice(offset, offset + limit) }); } });
  await page.goto("/admin/jobs?records=import&download_job_id=download-parent&p=1");
  await expect(page.getByRole("link", { name: "import-source-12", exact: true })).toBeVisible(); await expect(page.getByRole("link", { name: "import-source-0", exact: true })).toHaveCount(0);
  expect(queries[0].get("download_job_id")).toBe("download-parent"); expect(queries[0].get("offset")).toBe("12"); expect(queries[0].get("limit")).toBe("12");
});

test("Download records probe one extra item for pagination", async ({ page, context }) => {
  const queries: URLSearchParams[] = []; const records = Array.from({ length: 26 }, (_, index) => ({ ...baseJob, id: `download-${index}`, source_url: `download-source-${index}` }));
  await setup(context, (route, path) => { if (path === "/api/v1/download-jobs") { const query = new URL(route.request().url()).searchParams; queries.push(query); const offset = Number(query.get("offset") || 0); const limit = Number(query.get("limit") || 13); return json(route, records.slice(offset, offset + limit)); } });
  await page.goto("/admin/jobs?records=download"); await expect(page.getByRole("link", { name: "download-source-0", exact: true })).toBeVisible(); expect(queries[0].get("limit")).toBe("13");
  await page.getByRole("button", { name: /^Next$/i }).click(); await expect(page.getByRole("link", { name: "download-source-12", exact: true })).toBeVisible(); expect(queries[1].get("offset")).toBe("12");
  await page.getByRole("button", { name: /^Next$/i }).click(); await expect(page.getByRole("link", { name: "download-source-24", exact: true })).toBeVisible(); expect(queries[2].get("offset")).toBe("24");
});

test("Task detail keeps TaskRun actions under a records URL", async ({ page, context }) => {
  let taskRetry = 0; const wrong: string[] = [];
  await setup(context, (route, path) => {
    if (path === "/api/v1/tasks") return json(route, { total: 0, items: [], summary: { all: 0, attention: 0, running: 0, waiting: 0 } });
    if (path === "/api/v1/tasks/task-detail") return json(route, { id: "task-detail", title: "Authoritative task", kind: "download", status: "failed", created_at: baseJob.created_at, available_actions: ["retry"], disabled_reasons: {} });
    if (path === "/api/v1/tasks/task-detail/retry") { taskRetry += 1; return json(route, { task_id: "task-detail", status: "enqueued" }, 202); }
    if (path === "/api/v1/download-jobs/task-detail/retry") { wrong.push(path); return json(route, { detail: "wrong surface" }, 404); }
  });
  await page.goto("/admin/jobs?records=download&task=task-detail"); const dialog = page.getByRole("dialog"); await expect(dialog).toContainText("Authoritative task"); await dialog.getByRole("button", { name: /Retry/i }).click(); await expect.poll(() => taskRetry).toBe(1); expect(wrong).toEqual([]);
});

test("Legacy delete confirmation freezes the download surface across URL changes", async ({ page, context }) => {
  let deletes = 0; const wrong: string[] = []; const job = { ...baseJob, id: "download-confirm", source_url: "https://pixiv.example/confirm", available_actions: ["delete"] };
  await setup(context, (route, path) => {
    if (path === "/api/v1/download-jobs" && route.request().method() === "GET") return json(route, [job]);
    if (path === "/api/v1/download-jobs/download-confirm" && route.request().method() === "GET") return json(route, job);
    if (path === "/api/v1/download-jobs/download-confirm" && route.request().method() === "DELETE") { deletes += 1; return json(route, { status: "deleted" }); }
    if (path === "/api/v1/import-jobs/download-confirm" || path === "/api/v1/tasks/download-confirm") { wrong.push(path); return json(route, { detail: "wrong surface" }, 404); }
  });
  await page.goto("/admin/jobs?records=download"); await page.getByRole("link", { name: job.source_url, exact: true }).click(); const drawer = page.getByRole("dialog").first(); await expect(drawer).toContainText(job.source_url); await drawer.getByRole("button", { name: /Delete/i }).click();
  await page.evaluate(() => { window.history.pushState({}, "", "/admin/jobs?records=import"); document.querySelector<HTMLButtonElement>(".confirm-actions .button.primary")?.click(); }); await expect.poll(() => deletes).toBe(1); expect(wrong).toEqual([]);
});

test("TaskRun kind and source filters retain task semantics past the old download list boundary", async ({ page, context }) => {
  const queries: URLSearchParams[] = [];
  await setup(context, (route, path) => {
    if (path === "/api/v1/tasks") {
      queries.push(new URL(route.request().url()).searchParams);
      return json(route, { total: 1, items: [{ id: "task-204", title: "Task page 18", kind: "download", status: "running", created_at: baseJob.created_at, available_actions: [] }], summary: { all: 1, attention: 0, running: 1, waiting: 0 } });
    }
  });
  await page.goto("/admin/jobs?kind=download&scope=running&source=pixiv&p=17");
  await expect(page.getByRole("link", { name: "Task page 18", exact: true })).toBeVisible();
  expect(queries[0].get("kind")).toBe("download");
  expect(queries[0].get("scope")).toBe("running");
  expect(queries[0].get("offset")).toBe("204");
  expect(queries[0].get("source")).toBe("pixiv");
});

test("Repeat confirmation reuses one request identity after a retry", async ({ page, context }) => {
  const requestIds: string[] = [];
  const job = { ...baseJob, id: "repeat-original", source_url: "https://pixiv.example/repeat", available_actions: ["repeat_sync"] };
  let attempts = 0;
  await setup(context, (route, path) => {
    if (path === "/api/v1/download-jobs" && route.request().method() === "GET") return json(route, [job]);
    if (path === "/api/v1/download-jobs/repeat-original" && route.request().method() === "GET") return json(route, job);
    if (path === "/api/v1/download-jobs/repeat-original/repeat-sync") {
      const body = route.request().postDataJSON() as { request_id?: string };
      requestIds.push(String(body.request_id));
      attempts += 1;
      if (attempts === 1) return json(route, { detail: { code: "request_identity_conflict", message: "temporary identity conflict" } }, 409);
      return json(route, { job_id: "repeat-accepted", task_id: "task-repeat", previous_job_id: job.id, request_id: body.request_id, action: "repeat_sync", status: "enqueued" }, 202);
    }
  });
  await page.goto("/admin/jobs?records=download");
  await page.getByRole("link", { name: job.source_url, exact: true }).click();
  const drawer = page.getByRole("dialog").first();
  await drawer.getByRole("button", { name: /Repeat sync/i }).click();
  await page.getByRole("dialog").last().getByRole("button", { name: /^Confirm$/i }).click();
  await expect(page.getByRole("dialog").last().getByRole("alert")).toContainText(/identity|request/i);
  await page.getByRole("dialog").last().getByRole("button", { name: /^Confirm$/i }).click();
  await expect.poll(() => attempts).toBe(2);
  expect(requestIds).toHaveLength(2);
  expect(requestIds[0]).toMatch(/^[0-9a-f-]{36}$/);
  expect(requestIds[1]).toBe(requestIds[0]);
  await expect(page).toHaveURL(/job=repeat-accepted/);
});

test("Download records expose a batch surface that can preserve partial failures", async ({ page, context }) => {
  const records = [
    { ...baseJob, id: "batch-ok", source_url: "https://pixiv.example/ok", available_actions: ["retry"] },
    { ...baseJob, id: "batch-refused", source_url: "https://pixiv.example/refused", available_actions: ["retry"], disabled_reasons: { retry: "retry_not_available" } },
  ];
  let batchBody: unknown;
  await setup(context, (route, path) => {
    if (path === "/api/v1/download-jobs" && route.request().method() === "GET") return json(route, records);
    if (path === "/api/v1/download-jobs/batch") { batchBody = route.request().postDataJSON(); return json(route, { succeeded: 1, failed: 1, errors: [{ id: "batch-refused", error: { reason: "authoritative_refusal" } }] }); }
  });
  await page.goto("/admin/jobs?records=download");
  await expect(page.getByRole("link", { name: records[0].source_url, exact: true })).toBeVisible();
  const choose = page.getByRole("button", { name: /Choose|Select/i });
  await expect(choose).toBeVisible();
  await choose.click();
  await page.getByRole("checkbox", { name: /batch-ok/ }).check();
  await page.getByRole("checkbox", { name: /batch-refused/ }).check();
  await page.getByRole("combobox", { name: /Batch action/i }).click(); await page.getByRole("option", { name: /Retry/i }).click();
  await page.getByRole("button", { name: /Apply/i }).click();
  await page.getByRole("button", { name: /Confirm/i }).click();
  await expect.poll(() => batchBody).not.toBeUndefined();
  expect(batchBody).toEqual({ ids: ["batch-ok", "batch-refused"], action: "retry" });
  await expect(page.locator(".batch-errors")).toContainText("authoritative_refusal");
  await expect(page.getByRole("checkbox", { name: /batch-refused/ })).toBeChecked();
});

test("task details preserve conflict comparison, apply and rollback", async ({ page, context }) => {
  let resolved = false, applied: unknown, rollbacks = 0;
  const resolution = { resolution_id: "resolution-one", expires_at: "2027-01-01T00:00:00Z" };
  const item = { relative_path: "image.png", file_type: "media", mime_type: "image/png", canonical_size: 68, staged_size: 68, evidence: { auto_eligible: false, recommended_winner: "canonical", checks: { source: true, repository: true, work: true, creator: true, page: true, source_asset: true } } };
  await setup(context, (route, path) => {
    if (path === "/api/v1/tasks/conflict-task") return json(route, { id: "conflict-task", title: "Conflicted download", kind: "download", status: "failed", reason_code: "download_staging_conflict", created_at: baseJob.created_at, available_actions: [], meta: resolved ? { staging_conflict_resolution: resolution } : {} });
    if (path === "/api/v1/tasks/conflict-task/conflicts") return json(route, { task_id: "conflict-task", download_job_id: "download-one", source: "pixiv", source_url: "https://pixiv.example/work", status: "failed", all_auto_eligible: false, items: [item] });
    if (path === "/api/v1/tasks/conflict-task/conflicts/media") return route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=", "base64") });
    if (path === "/api/v1/tasks/conflict-task/conflicts/resolve") { applied = route.request().postDataJSON(); resolved = true; return json(route, { ...resolution, task_id: "conflict-task", download_job_id: "download-one", state: "applied", automatic: false, resolved_at: baseJob.created_at, retry: { status: "needs_retry" } }); }
    if (path === "/api/v1/tasks/conflict-task/conflicts/resolutions/resolution-one/rollback") { resolved = false; rollbacks += 1; return json(route, { ...resolution, state: "rolled_back" }); }
  });
  await page.goto("/admin/jobs?task=conflict-task");
  const details = page.getByRole("dialog", { name: "Task detail", exact: true });
  const compare = details.getByRole("button", { name: "Compare and resolve", exact: true });
  await expect(compare).toBeVisible();
  await compare.click();
  const comparison = page.getByRole("dialog", { name: "Download file conflict comparison", exact: true });
  await expect(comparison.getByRole("heading", { name: "1. image.png", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(comparison).toHaveCount(0);
  await expect(details).toBeVisible();
  await expect(compare).toBeFocused();
  await compare.click();
  await comparison.locator('input[value="staged"]').check();
  await comparison.getByRole("button", { name: "Apply all choices and continue", exact: true }).click();
  await expect.poll(() => applied).toEqual({ decisions: [{ relative_path: "image.png", winner: "staged" }] });
  await expect(comparison).toHaveCount(0);
  await details.getByRole("button", { name: "View resolution and rollback", exact: true }).click();
  await comparison.getByRole("button", { name: "Roll back file choices", exact: true }).click();
  await expect.poll(() => rollbacks).toBe(1);
  await expect(comparison).toHaveCount(0);
});


test("one task status push refreshes the list once while progress still updates it",async({page,context})=>{
 let reads=0,notify!:(type:string)=>void,release!:()=>void;
 const gate=new Promise<void>(resolve=>{release=resolve;});
 await setup(context,(route,path)=>{
  if(path==='/api/v1/auth/ws-ticket')return json(route,{ticket:'refresh-ticket'});
  if(path==='/api/v1/tasks')return (async()=>{const read=++reads;if(read===2||read===3)await gate;
   return json(route,{total:1,items:[{id:'push-task',kind:'download',title:read===1?'Before push':read===2?'After status':'After progress',status:'complete',created_at:baseJob.created_at,available_actions:[]}],summary:{all:1,attention:0,running:0,waiting:0}});})();
 });
 await context.routeWebSocket('**/api/v1/ws*',socket=>{notify=type=>socket.send(JSON.stringify({type,task_id:'push-task',new_status:'complete'}));});
 try{
  await page.goto('/admin/jobs');await expect(page.getByRole('link',{name:'Before push',exact:true})).toBeVisible();await expect.poll(()=>typeof notify).toBe('function');
  notify('status_change');await expect.poll(()=>reads).toBeGreaterThan(1);await page.waitForTimeout(100);expect(reads).toBe(2);
  release();await expect(page.getByRole('link',{name:'After status',exact:true})).toBeVisible();
  notify('progress');await expect(page.getByRole('link',{name:'After progress',exact:true})).toBeVisible();expect(reads).toBe(3);
 }finally{release();}
});


for (const outcome of ["success","partial","failure"] as const) test(`filter retry ${outcome} refreshes the records once`,async({page,context})=>{
 let reads=0,body:unknown,release!:()=>void,submitted=false;const pending=new Promise<void>(resolve=>{release=resolve;});
 await setup(context,(route,path)=>{
  if(path==='/api/v1/download-jobs')return (async()=>{const read=++reads;if(submitted)await pending;return json(route,[{...baseJob,id:'filter-job',source_url:read===1?'Before retry':'After retry'}]);})();
  if(path==='/api/v1/download-jobs/batch-by-filter'){body=route.request().postDataJSON();submitted=true;return json(route,outcome==='failure'?{detail:'Retry refused'}:{matched:2,succeeded:outcome==='success'?2:1,failed:outcome==='success'?0:1,errors:outcome==='success'?[]:[{id:'refused',error:'Not retryable'}]},outcome==='failure'?500:200);}
 });
 try{
  await page.goto('/admin/jobs?records=download&status=failed');await expect(page.getByRole('link',{name:'Before retry',exact:true})).toBeVisible();
  await page.getByText('Records and maintenance',{exact:true}).click();await page.getByRole('button',{name:'Batch current filter',exact:true}).click();
  const dialog=page.getByRole('dialog',{name:'Retry All Failed',exact:true});await dialog.getByRole('button',{name:'Confirm',exact:true}).click();
  await expect.poll(()=>reads).toBeGreaterThan(1);await page.waitForTimeout(100);expect(reads).toBe(2);expect(body).toEqual({filters:{status:'failed'},action:'retry'});
  release();await expect(page.getByRole('link',{name:'After retry',exact:true})).toBeVisible();
  if(outcome==='success')await expect(dialog).toHaveCount(0);else await expect(dialog).toBeVisible();
  if(outcome==='failure')await expect(dialog.getByRole('alert')).toContainText('Retry refused');
 }finally{release();}
});


for(const view of ['task-download','task-import','record-download','record-import','all','unknown','same-kind-foreign'] as const)test(`progress scope ${view}`,async({page,context})=>{
 let reads=0,notify!:(type?:string)=>void;
 const download=view==='task-download'||view==='record-download'||view==='same-kind-foreign'||view==='unknown';const record=view.startsWith('record-');const domain=download?'download':'import';
 const path=record?'/api/v1/'+domain+'-jobs':'/api/v1/tasks';
 await setup(context,(route,url)=>{if(url==='/api/v1/auth/ws-ticket')return json(route,{ticket:'scope-ticket'});if(url===path){reads++;const item=record?{...baseJob,id:'domain-visible',source_url:'Visible record'}:{id:'run-visible',subject_id:'domain-visible',subject_type:domain+'_job',kind:domain,title:'Visible task',status:'complete',created_at:baseJob.created_at,available_actions:[]};return json(route,record&&download?[item]:{total:1,items:[item],summary:{all:1,attention:0,running:0,waiting:0}});}});
 await context.routeWebSocket('**/api/v1/ws*',socket=>{notify=type=>socket.send(JSON.stringify({type:'progress',task_id:'other-domain-id',...(type?{task_type:type}:{})}));});
 const query=record?'records='+domain:view==='all'?'':view==='unknown'||view==='same-kind-foreign'?'kind=download':'kind='+domain;
 await page.goto('/admin/jobs?'+query);await expect(page.getByRole('link',{name:record?'Visible record':'Visible task',exact:true})).toBeVisible();await expect.poll(()=>typeof notify).toBe('function');const before=reads;
 await page.evaluate(()=>{(window as any).__progressSeen=0;window.addEventListener('auto-gallery:job-event',()=>{(window as any).__progressSeen++;});});
 notify(view==='unknown'?undefined:view==='same-kind-foreign'?'download':download?'import':'download');await expect.poll(()=>page.evaluate(()=>(window as any).__progressSeen)).toBe(1);await new Promise(r=>setTimeout(r,100));
 expect(reads-before).toBe(view==='all'||view==='unknown'||view==='same-kind-foreign'?1:0);
});

test('latest progress does not wait for an older in-flight response',async({page,context})=>{
 let reads=0,notify!:()=>void,release!:()=>void,oldReturned=false;const pending=new Promise<void>(resolve=>{release=resolve;});
 await setup(context,(route,path)=>{if(path==='/api/v1/auth/ws-ticket')return json(route,{ticket:'latency-ticket'});if(path==='/api/v1/tasks')return(async()=>{const n=++reads;if(n===2)await pending;await json(route,{total:1,items:[{id:'run-visible',subject_type:'download_job',subject_id:'domain-visible',kind:'download',status:'running',title:n>=3?'Latest progress':n===2?'Old progress':'Initial progress',progress_current:n>=3?42:1,progress_total:100,created_at:baseJob.created_at,available_actions:[]}],summary:{all:1,attention:0,running:1,waiting:0}});if(n===2)oldReturned=true;})();});
 await context.routeWebSocket('**/api/v1/ws*',socket=>{notify=()=>socket.send(JSON.stringify({type:'progress',task_id:'domain-visible',task_type:'download',progress:{current:42,total:100}}));});
 try{
  await page.goto('/admin/jobs?kind=download');await expect(page.getByRole('link',{name:'Initial progress',exact:true})).toBeVisible();await expect.poll(()=>typeof notify).toBe('function');
  notify();await expect.poll(()=>reads).toBe(2);notify();await expect(page.getByRole('link',{name:'Latest progress',exact:true})).toBeVisible();
  release();await expect.poll(()=>oldReturned).toBe(true);await expect(page.getByRole('link',{name:'Latest progress',exact:true})).toBeVisible();
 }finally{release();}
});

for(const status of ['complete','failed'] as const)test(`terminal status stays immediate ${status}`,async({page,context})=>{
 let reads=0,send!:(type:string)=>void,release!:()=>void;const pending=new Promise<void>(resolve=>{release=resolve;});
 await setup(context,(route,path)=>{if(path==='/api/v1/auth/ws-ticket')return json(route,{ticket:'terminal-ticket'});if(path==='/api/v1/tasks')return(async()=>{const n=++reads;if(n===2)await pending;return json(route,{total:1,items:[{id:'terminal-task',kind:'download',title:n>=3?'Terminal '+status:'Running task',status:n>=3?status:'running',created_at:baseJob.created_at,available_actions:[]}],summary:{all:1,attention:status==='failed'?1:0,running:n>=3?0:1,waiting:0}});})();});
 await context.routeWebSocket('**/api/v1/ws*',socket=>{send=type=>socket.send(JSON.stringify({type,task_id:'terminal-task',task_type:type==='progress'?'download':'import',new_status:status}));});
 try{
 await page.goto('/admin/jobs?kind=download');await expect(page.getByRole('link',{name:'Running task',exact:true})).toBeVisible();await expect.poll(()=>typeof send).toBe('function');
 send('progress');await expect.poll(()=>reads).toBe(2);send('status_change');await expect(page.getByRole('link',{name:'Terminal '+status,exact:true})).toBeVisible();
 }finally{release();}
});
