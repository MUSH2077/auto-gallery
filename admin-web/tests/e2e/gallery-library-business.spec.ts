import {expect,test,type BrowserContext,type Page,type Route} from "@playwright/test";
test.describe.configure({timeout:45000});
test.use({actionTimeout:10000});
const json=(route:Route,body:unknown,status=200)=>route.fulfill({status,contentType:"application/json",body:JSON.stringify(body)});
const origin=process.env.PLAYWRIGHT_BASE_URL||"http://127.0.0.1:13000";
const pixel=Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlBCe0AAAAASUVORK5CYII=","base64");
async function setup(context:BrowserContext,options:{libraryOnly?:boolean;failTags?:boolean;failHistory?:boolean;legacyPreview?:boolean}={}){
 let preferences:Record<string,unknown>={lang:'en',theme:'light',appearance:{workPreviewEnabled:options.legacyPreview??false}};
 let failTags=options.failTags||false,failHistory=options.failHistory||false;const requests:Array<{path:string;method:string;query:string;body:unknown}>=[];
 const works=Array.from({length:24},(_,index)=>({id:index===0?'work-one':'work-'+index,title:index===0?'Harbor light':'Harbor '+index,creator_name:'Harbor Archive',creator_id:null,created_at:'2026-10-08T00:00:00Z',posted_at:'2026-10-07T00:00:00Z',description:'A quiet harbor.',source:index%2?'x':'pixiv',is_nsfw:index===1,is_ai_generated:false,is_favorite:false,asset_count:index===0?2:1,thumbnail_asset_id:'asset-one',thumbnail_width:1200,thumbnail_height:800,preview_asset_ids:['asset-one','asset-two'],curation_state:{visibility:'visible'}}));
 const assets=['asset-one','asset-two'].map(id=>({id,file_name:id+'.png',file_path:id+'.png',mime_type:'image/png',media_kind:'image',width:1200,height:800,file_size:68,created_at:'2026-10-08T00:00:00Z',preview_url:'/media/preview/'+id,original_url:'/media/original/'+id,thumb_url:'/media/thumb/'+id,derivative_status:'ready'}));
 await context.addCookies([{name:'ag_session',value:'library-review',url:origin},{name:'ag_csrf',value:'library-csrf',url:origin}]);
 await context.route('**/media/**',route=>route.fulfill({contentType:'image/png',body:pixel}));
 await context.route('**/api/v1/**',route=>{
  const request=route.request(),url=new URL(request.url()),path=url.pathname,method=request.method();let body:unknown;try{body=request.postDataJSON();}catch{}requests.push({path,method,query:url.search,body});
  if(path==='/api/v1/auth/me')return json(route,{id:92,username:'library-review',is_admin:!options.libraryOnly,is_active:true,permissions:options.libraryOnly?['library']:['library','curation','system'],preferences,nsfw_visible:true,must_change_password:false});
  if(path==='/api/v1/auth/me/preferences'){preferences=(body as any).preferences;return json(route,{preferences});}
  if(path==='/api/v1/auth/ws-ticket')return json(route,{detail:'isolated fixture'},503);
  if(path==='/api/v1/works/derivative-progress')return json(route,{total:0,remaining:0,completed:0,affected_works:0,completion_percent:100,status:'idle'});
  if(path==='/api/v1/search/assist'){
   const data=body as {before_cursor?:string;scope?:string;composes?:Array<{key?:string;value?:string;operation?:string}>};
   if(data.before_cursor==='harbor'&&data.scope==='works'){
    const canonical='harbor source:pixiv source:x';
    const tokens=[{kind:'text',value:'harbor'},{kind:'qualifier',key:'source',value:'pixiv',negated:false},{kind:'qualifier',key:'source',value:'x',negated:false}];
    return json(route,{query:canonical,canonical_query:canonical,parsed:{tokens},diagnostics:[],suggestions:[]});
   }
   if(data.scope==='works'&&data.before_cursor==='sort:random'){
    const canonical='sort:random';
    return json(route,{query:canonical,canonical_query:canonical,parsed:{tokens:[{kind:'qualifier',key:'sort',value:'random',negated:false}]},diagnostics:[],suggestions:[]});
   }
   return json(route,{detail:'unexpected search assist fixture request'},400);
  }
  if(path==='/api/v1/search'){const q=url.searchParams.get('q')||'',offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||8);let items=works.filter(work=>q.includes('is:trashed')?work.curation_state.visibility==='trashed':work.curation_state.visibility!=='trashed');if(q.includes('is:favorite'))items=items.filter(work=>work.is_favorite);return json(route,{query:q,canonical_query:q,parsed:{tokens:q==='harbor source:pixiv source:x'?[{kind:'text',value:'harbor'},{kind:'qualifier',key:'source',value:'pixiv',negated:false},{kind:'qualifier',key:'source',value:'x',negated:false}]:q.includes('sort:random')?[{kind:'qualifier',key:'sort',value:'random',negated:false}]:q.includes('source:pixiv')?[{kind:'qualifier',key:'source',value:'pixiv',negated:false}]:[],diagnostics:[]},groups:{works:{total:items.length,items:items.slice(offset,offset+limit)}},total:items.length,seed:Number(url.searchParams.get('seed')||0),next_cursor:null});}
  if(path==='/api/v1/works/batch-curate'){for(const id of (body as any).ids){const work=works.find(work=>work.id===id);if(work)work.curation_state.visibility=(body as any).action==='trash'?'trashed':'visible';}return json(route,{id:'curation-one',message:'updated',occurred_at:'2026-10-08T00:00:00Z'});}
  const matched=path.match(/^\/api\/v1\/works\/([^/]+)(?:\/(.*))?$/);
  if(matched){const work=works.find(work=>work.id===matched[1])||works[0];switch(matched[2]){
   case 'assets':return json(route,assets);
   case 'sources':return json(route,[{id:'source-one',source:'pixiv',source_work_id:'1200',source_url:'https://www.pixiv.net/artworks/1200',raw_metadata:{width:1200,height:800,page_count:2,tags:['night sky'],tools:['Watercolor']}}]);
   case 'tags':return json(route,failTags?{detail:'tags unavailable'}:[{id:'tag-one',normalized_name:'night sky'}],failTags?503:200);
   case 'remote-state':return json(route,{source:'pixiv',source_work_id:'1200',fetched_at:'2026-10-08T09:00:00Z',total_views:120,total_bookmarks:9,is_bookmarked:true});
   case 'favorite':work.is_favorite=!work.is_favorite;return json(route,work);
   default:return json(route,work);
  }}
  if(path==='/api/v1/curation/commits')return json(route,failHistory?{detail:'history unavailable'}:{total:1,items:[{id:'history-one',message:'Imported from local archive',occurred_at:'2026-10-08T00:00:00Z',trigger:'manual'}]},failHistory?503:200);
  if(path==='/api/v1/admin/clear/preview/works')return json(route,{entity:'works',confirmation_phrase:'CLEAR WORKS',counts:{works:24,assets:25},preserves_repository_sync_receipts:true,deletes_media_files:true});
  if(path==='/api/v1/admin/operations/clear')return json(route,{task_id:'clear-task',job_id:'clear-job',operation_type:'clear',status:'enqueued'},202);
  if(path==='/api/v1/notifications')return json(route,{total:0,items:[]});
  return json(route,{});
 });
 return {requests,recoverTags:()=>{failTags=false;},recoverHistory:()=>{failHistory=false;}};
}
async function displaySettings(page:Page){await page.getByRole('button',{name:'Display',exact:true}).click();await page.getByRole('button',{name:'Display settings',exact:true}).click();await expect(page.getByRole('dialog',{name:'Display settings'})).toBeVisible();return page.getByRole('dialog',{name:'Display settings'});}

test('advanced multi-source filters retain compound text and apply through the search composer',async({page,context})=>{
 const state=await setup(context);await page.goto('/admin/works?q=harbor');await page.getByRole('combobox',{name:'Work source',exact:true}).click();await page.getByRole('option',{name:'Filter',exact:true}).click();
 const dialog=page.getByRole('dialog');await expect(dialog).toBeVisible();await dialog.getByLabel('Pixiv',{exact:true}).check();await dialog.getByLabel('X',{exact:true}).check();await dialog.getByRole('button',{name:'Apply',exact:true}).click();
 await expect(dialog).toHaveCount(0);await expect.poll(()=>new URL(page.url()).searchParams.get('q')).toContain('source:pixiv');const q=new URL(page.url()).searchParams.get('q')!;expect(q).toContain('source:x');expect(q).toContain('harbor');await expect(page.getByRole('combobox',{name:'Work source',exact:true})).toContainText('2 sources');const assist=state.requests.find(request=>request.path==='/api/v1/search/assist'&&Array.isArray((request.body as {composes?:unknown[]}).composes));expect(assist?.body).toMatchObject({before_cursor:'harbor',scope:'works'});const assistComposes=(assist?.body as {composes?:unknown[]}|undefined)?.composes;expect(assistComposes).toEqual(expect.arrayContaining([{key:'source',value:'pixiv',operation:'set'},{key:'source',value:'x',operation:'add'}]));
});

test('appearance changes affect visible card geometry and do not reset default masonry',async({page,context})=>{
 await setup(context);await page.setViewportSize({width:1440,height:900});await page.goto('/admin/works');await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','masonry');
 const before=await page.locator('[data-work-id]').first().boundingBox();const dialog=await displaySettings(page);await dialog.getByRole('combobox',{name:'Card size',exact:true}).click();await page.getByRole('option',{name:'Large',exact:true}).click();await dialog.getByRole('button',{name:'Done',exact:true}).click();
 await expect.poll(async()=>(await page.locator('[data-work-id]').first().boundingBox())!.width).toBeGreaterThan(before!.width+50);
 await page.getByRole('button',{name:'Display',exact:true}).click();await page.getByLabel('Blur adult content').uncheck();await page.keyboard.press('Escape');await page.getByRole('button',{name:'Next',exact:true}).click();await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','masonry');
});

test('slideshow remains reachable and search random seeds stay stable',async({page,context})=>{
 await setup(context);await page.goto('/admin/works?q=sort%3Arandom&seed=42');const dialog=await displaySettings(page);await expect(dialog.getByLabel('Enlarged preview',{exact:true})).toHaveCount(0);await dialog.getByRole('button',{name:/Slideshow/i}).click();await expect(page.getByRole('dialog')).toBeVisible();await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'Next',exact:true}).click();expect(new URL(page.url()).searchParams.get('seed')).toBe('42');
});

test('detail exposes sources tags remote status and history without changing core facts',async({page,context})=>{
 await setup(context);await page.goto('/admin/works?work=work-one');const dialog=page.getByRole('dialog');await expect(dialog).toBeVisible();await expect(dialog.locator('.detail-info>dl>div')).toHaveCount(4);await dialog.locator('.detail-more-information>summary').click();
 await dialog.locator('summary').filter({hasText:/Tags/}).click();const tag=dialog.getByRole('link',{name:'night sky',exact:true}).first();expect(new URL(await tag.getAttribute('href')||'',origin).searchParams.get('q')).toContain('tag:"night sky"');
 await dialog.locator('summary').filter({hasText:/^Source Records/i}).click();await expect(dialog.getByRole('link',{name:/View on pixiv/i})).toHaveAttribute('href','https://www.pixiv.net/artworks/1200');
 await dialog.locator('summary').filter({hasText:/Pixiv.*live|Live.*Pixiv/i}).click();await expect(dialog).toContainText('120');await expect(dialog).toContainText('9');
 await dialog.locator('summary').filter({hasText:/history/i}).click();await expect(dialog).toContainText('Imported from local archive');
});

test('library-only readers do not request forbidden history and failed detail queries can recover',async({page,context})=>{
 const state=await setup(context,{libraryOnly:true,failTags:true});await page.goto('/admin/works?work=work-one');const dialog=page.getByRole('dialog');await expect(dialog).toBeVisible();expect(state.requests.some(request=>request.path==='/api/v1/curation/commits')).toBe(false);await expect(dialog.locator('summary').filter({hasText:/history/i})).toHaveCount(0);await dialog.locator('.detail-more-information>summary').click();
 await dialog.locator('summary').filter({hasText:/Tags/}).click();await expect(dialog.getByRole('alert').filter({hasText:'Tags could not be loaded.'})).toBeVisible();state.recoverTags();await dialog.getByRole('button',{name:'Retry',exact:true}).click();await expect(dialog.getByRole('link',{name:'night sky',exact:true}).first()).toBeVisible();
});

test('clear library keeps impact preview exact confirmation phrase and native modal controls',async({page,context})=>{
 const state=await setup(context);await page.goto('/admin/works');const settings=await displaySettings(page);await settings.locator('summary').click();await settings.getByRole('button',{name:/Delete All Works/i}).click();const confirm=page.getByRole('dialog').last();await expect(page.locator('dialog[open]')).toHaveCount(2);
 const submit=confirm.getByRole('button',{name:'Confirm',exact:true});await expect(submit).toBeDisabled();await confirm.getByRole('textbox').fill('CLEAR');await expect(submit).toBeDisabled();await confirm.getByRole('textbox').fill('CLEAR WORKS');await expect(submit).toBeEnabled();await submit.click();await expect.poll(()=>state.requests.filter(request=>request.path==='/api/v1/admin/operations/clear').length).toBe(1);expect(state.requests.find(request=>request.path==='/api/v1/admin/operations/clear')?.body).toEqual({entity:'works',confirmation:'CLEAR WORKS'});
});


test('hovering never opens a preview or requests original media',async({page,context})=>{
 const state=await setup(context,{legacyPreview:true});const media:string[]=[];page.on('request',request=>{if(/\/media\/(original|preview)\//.test(request.url()))media.push(request.url());});await page.setViewportSize({width:1440,height:900});await page.goto('/admin/works');
 const card=page.locator('[data-work-id="work-one"]');await card.locator('h2 a').hover();await page.waitForTimeout(600);
 await expect(page.locator('.popover.fixed')).toHaveCount(0);
 expect(state.requests.filter(request=>request.path==='/api/v1/works/work-one/assets')).toHaveLength(0);expect(media).toHaveLength(0);
 await card.locator('h2 a').click();await expect(page.getByRole('dialog')).toBeVisible();
});

test('complete sort choices are reachable and reshuffle changes the random seed',async({page,context})=>{
 await setup(context);await page.goto('/admin/works?q=sort%3Arandom&seed=42');const sort=page.getByRole('combobox',{name:'Sort',exact:true});await sort.click();await expect(page.getByRole('option',{name:'Title · Z to A',exact:true})).toBeVisible();await page.getByRole('option',{name:'Reshuffle',exact:true}).click();await expect.poll(()=>new URL(page.url()).searchParams.get('seed')).not.toBe('42');await expect(sort).toContainText('Random');
});

test('long work history opens canonical curation with the same work filter',async({page,context})=>{
 await setup(context);const queries:string[]=[];const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await context.route('**/api/v1/curation/**',route=>{const url=new URL(route.request().url());
  if(url.pathname==='/api/v1/curation/commits'){queries.push(url.search);return json(route,{total:url.searchParams.get('limit')==='20'?6:0,items:url.searchParams.get('limit')==='20'?Array.from({length:6},(_,i)=>({id:'history-'+i,message:'Archive update '+i,occurred_at:'2026-10-08T00:00:00Z',trigger:'manual',status:'active',changes:[]})):[]});}
  if(url.pathname==='/api/v1/curation/backfill/status')return json(route,{is_complete:true,expected:{},existing:{},missing:{}});
  if(url.pathname==='/api/v1/curation/rule-suggestions')return json(route,[]);
  if(url.pathname==='/api/v1/curation/purge/preview')return json(route,{work_count:0,bytes_reclaimable:0,works:[]});
  return route.fallback();
 });
 await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog');await expect(detail).toBeVisible();await detail.locator('.detail-more-information>summary').click();await detail.locator('summary').filter({hasText:/history/i}).click();
 const link=detail.getByRole('link',{name:'Open curation',exact:true});await expect(link).toHaveAttribute('href','/admin/data-mgmt/curation?subject_type=work&subject_id=work-one');await link.click();
 await expect(page).toHaveURL(/\/admin\/data-mgmt\/curation\?subject_type=work&subject_id=work-one$/);await expect(page.getByRole('heading',{level:1,name:/Curation/i})).toBeVisible();
 await expect.poll(()=>queries.some(q=>{const params=new URLSearchParams(q);return params.get('limit')==='40'&&params.get('subject_type')==='work'&&params.get('subject_id')==='work-one';})).toBe(true);expect(errors).toEqual([]);
});

for (const [label, broken] of [["non-array", null], ["null record", [null]], ["invalid metadata", [{id:"source-one",source:"pixiv",raw_metadata:"bad"}]]] as const) {
 test(`invalid work sources ${label} stays local and recovers through retry`,async({page,context})=>{
  await setup(context);
  let recovered=false;
  await context.route("**/api/v1/works/work-one/sources", route=>json(route,recovered?[{id:"source-one",source:"pixiv",source_work_id:"1200",source_creator_id:null,source_url:"https://www.pixiv.net/artworks/1200",description:null,posted_at:null,raw_metadata:null}]:broken));
  await page.goto("/admin/works?work=work-one");
  const detail=page.getByRole("dialog");await expect(detail).toBeVisible();
  await expect(detail.locator(".detail-media-viewer")).toBeVisible();
  await detail.locator(".detail-more-information>summary").click();
  await detail.locator("summary").filter({hasText:/^Source Records/i}).click();
  await expect(detail.getByRole("alert").filter({hasText:"Source records could not be loaded."})).toBeVisible();
  recovered=true;await detail.getByRole("button",{name:"Retry",exact:true}).click();
  await expect(detail.getByRole("link",{name:/View on pixiv/i})).toHaveAttribute("href","https://www.pixiv.net/artworks/1200");
 });
}


test('detail trash reacts immediately and restores state on failure',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 await context.route('**/api/v1/works/batch-curate',async route=>{await gate;return json(route,{detail:'Curation failed'},500);});
 try{
  await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
  await detail.getByRole('button',{name:'Move to Trash',exact:true}).click();
  const confirm=page.getByRole('dialog',{name:'Move to Trash'});await confirm.getByRole('button',{name:'Confirm',exact:true}).click();
  await expect(confirm).toHaveCount(0);
  await expect(detail.getByRole('button',{name:'Restore',exact:true})).toBeVisible();
  release();
  await expect(detail.getByRole('button',{name:'Move to Trash',exact:true})).toBeVisible();
  await expect(detail.getByRole('alert').filter({hasText:'Curation failed'})).toBeVisible();
 }finally{release();}
});

test('batch trash immediately removes selected card and restores it on failure',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 await context.route('**/api/v1/works/batch-curate',async route=>{await gate;return json(route,{detail:'Batch failed'},500);});
 try{
  await page.goto('/admin/works');await page.getByRole('button',{name:'Select works'}).click();
  const card=page.locator('[data-work-id="work-one"]');await card.getByRole('checkbox').check();
  await page.getByRole('button',{name:'Move to Trash',exact:true}).click();
  const confirm=page.getByRole('dialog');await confirm.getByRole('button',{name:'Confirm',exact:true}).click();
  await expect(confirm).toHaveCount(0);await expect(card).toHaveCount(0);
  release();await expect(card).toBeVisible();await expect(page.getByText('Batch failed').first()).toBeVisible();
 }finally{release();}
});


test('favorite stays selected when opening detail before the response',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 await context.route('**/api/v1/works/work-one/favorite',async route=>{await gate;return route.fallback();});
 try{
  await page.goto('/admin/works');const card=page.locator('[data-work-id="work-one"]');
  await card.getByRole('button',{name:'Favorite: Harbor light'}).click();
  await card.getByRole('link',{name:'Harbor light',exact:true}).click();
  const detail=page.getByRole('dialog').first();
  await expect(detail.getByRole('button',{name:'Unfavorite work'})).toHaveAttribute('aria-pressed','true');
 }finally{release();}
});

test('restore reacts immediately and rolls back if it fails',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 await context.route('**/api/v1/works/batch-curate',async route=>{
  if(route.request().postDataJSON().action==='trash')return route.fallback();
  await gate;return json(route,{detail:'Restore failed'},500);
 });
 try{
  await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
  await detail.getByRole('button',{name:'Move to Trash',exact:true}).click();
  await page.getByRole('dialog',{name:'Move to Trash'}).getByRole('button',{name:'Confirm'}).click();
  const restore=detail.getByRole('button',{name:'Restore',exact:true});await expect(restore).toBeEnabled();await restore.click();
  await page.getByRole('dialog',{name:'Restore'}).getByRole('button',{name:'Confirm'}).click();
  await expect(detail.getByRole('button',{name:'Move to Trash',exact:true})).toBeVisible();
  release();await expect(detail.getByRole('button',{name:'Restore',exact:true})).toBeVisible();
  await expect(detail.getByRole('alert').filter({hasText:'Restore failed'})).toBeVisible();
 }finally{release();}
});


test('trash list restore immediately hides item and restores it on failure',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});
 const work={id:'work-trash',title:'Recoverable work',creator_name:'Archive',created_at:'2026-10-08T00:00:00Z',source:'manual',is_nsfw:false,is_ai_generated:false,is_favorite:false,asset_count:1,thumbnail_asset_id:'asset-one',curation_state:{visibility:'trashed'}};
 await context.route(/\/api\/v1\/search(?:\?.*)?$/,route=>json(route,{query:'is:trashed',canonical_query:'is:trashed',parsed:{tokens:[{kind:'qualifier',key:'is',value:'trashed',negated:false}]},groups:{works:{total:1,items:[work]}},total:1,next_cursor:null}));
 await context.route('**/api/v1/works/batch-curate',async route=>{await gate;return json(route,{detail:'Restore failed'},500);});
 try{
  await page.goto('/admin/works?q=is%3Atrashed');const card=page.locator('[data-work-id="work-trash"]');
  await card.getByRole('button',{name:'Restore',exact:true}).click();await expect(card).toHaveCount(0);
  release();await expect(card).toBeVisible();await expect(card.getByRole('alert')).toContainText('Restore failed');
 }finally{release();}
});


test('permanent deletion keeps confirmation open until the server responds',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});let calls=0;
 await context.route('**/api/v1/works/work-one',route=>json(route,{id:'work-one',title:'Harbor light',creator_name:'Harbor Archive',created_at:'2026-10-08T00:00:00Z',source:'manual',is_nsfw:false,is_ai_generated:false,is_favorite:false,asset_count:1,curation_state:{visibility:'trashed'}}));
 await context.route('**/api/v1/curation/purge',async route=>{calls++;await gate;return json(route,{detail:'Purge failed'},500);});
 try{
  await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
  await detail.getByRole('button',{name:'Permanently delete',exact:true}).click();
  const confirm=page.getByRole('dialog').last();await confirm.getByRole('button',{name:'Confirm',exact:true}).click();
  await expect.poll(()=>calls).toBe(1);await expect(confirm).toBeVisible();
  release();await expect(confirm.getByRole('alert')).toContainText('Purge failed');
 }finally{release();}
});


test('detail favorite keeps image and trash action visually stable while protecting curation',async({page,context})=>{
 await setup(context);let release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});let assetCalls=0, favoriteCalls=0;
 await context.route('**/api/v1/works/work-one/assets',route=>{assetCalls++;return route.fallback();});
 await context.route('**/api/v1/works/work-one/favorite',async route=>{favoriteCalls++;await gate;return route.fallback();});
 try{
  await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
  const image=detail.locator('.detail-media-viewer img').first();await expect(image).toBeVisible();
  const handle=await image.elementHandle();const src=await image.getAttribute('src');const initialCalls=assetCalls;
  const trash=detail.getByRole('button',{name:'Move to Trash',exact:true});const opacity=await trash.evaluate(node=>getComputedStyle(node).opacity);
  await detail.locator('.detail-favorite-toggle').click();await expect.poll(()=>favoriteCalls).toBe(1);
  await expect(trash).toHaveAttribute('aria-disabled','true');expect(await trash.evaluate(node=>getComputedStyle(node).opacity)).toBe(opacity);
  await trash.dispatchEvent('click');await expect(page.getByRole('dialog',{name:'Move to Trash'})).toHaveCount(0);
  release();await expect(trash).not.toHaveAttribute('aria-disabled','true');
  expect(await handle!.evaluate(node=>node.isConnected)).toBe(true);await expect(image).toHaveAttribute('src',src!);expect(assetCalls).toBe(initialCalls);
 }finally{release();}
});


test('restore refreshes visibility and history without refetching unchanged media',async({page,context})=>{
 const fixture=await setup(context);await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
 await expect(detail.locator('.detail-media-viewer img').first()).toBeVisible();
 await detail.getByRole('button',{name:'Move to Trash',exact:true}).click();await page.getByRole('dialog',{name:'Move to Trash'}).getByRole('button',{name:'Confirm'}).click();
 await expect(detail.getByRole('button',{name:'Restore',exact:true})).toBeEnabled();
 const before=fixture.requests.length;const image=await detail.locator('.detail-media-viewer img').first().elementHandle();
 await detail.getByRole('button',{name:'Restore',exact:true}).click();await page.getByRole('dialog',{name:'Restore'}).getByRole('button',{name:'Confirm'}).click();
 await expect(detail.getByRole('button',{name:'Move to Trash',exact:true})).toBeEnabled();
 const reads=fixture.requests.slice(before).filter(r=>r.method==='GET');
 expect(reads.filter(r=>/\/(assets|sources|tags|remote-state)$/.test(r.path))).toEqual([]);
 expect(reads.filter(r=>r.path==='/api/v1/works/derivative-progress')).toEqual([]);
 expect(reads.some(r=>r.path==='/api/v1/works/work-one')).toBe(true);
 expect(reads.some(r=>r.path==='/api/v1/search')).toBe(true);
 expect(await image!.evaluate(node=>node.isConnected)).toBe(true);
});

test('favorite count and cached paging recover after removing the last item on a page',async({page,context})=>{
 await setup(context);let items=Array.from({length:9},(_,i)=>({id:'fav-'+i,title:'Favorite '+i,is_favorite:true,is_nsfw:false,is_ai_generated:false,asset_count:0,created_at:'2026-10-08T00:00:00Z',curation_state:{visibility:'visible'}}));
 await context.route(/\/api\/v1\/search(?:\?.*)?$/,route=>{const u=new URL(route.request().url()),offset=Number(u.searchParams.get('offset')||0);return json(route,{query:'is:favorite',canonical_query:'is:favorite',parsed:{tokens:[{kind:'qualifier',key:'is',value:'favorite',negated:false}]},groups:{works:{total:items.length,items:items.slice(offset,offset+8)}},total:items.length,next_cursor:null});});
 await context.route('**/api/v1/works/fav-8/favorite',route=>{items=items.slice(0,8);return json(route,{id:'fav-8',is_favorite:false});});
 await page.goto('/admin/works?q=is%3Afavorite');await expect(page.locator('.layout-scope-tabs')).toContainText('9');
 await page.locator('.pagination button').last().click();
 await page.getByRole('button',{name:'Unfavorite: Favorite 8',exact:true}).click();
 await expect(page.locator('[data-work-id="fav-0"]')).toBeVisible();await expect(page.locator('.layout-scope-tabs')).toContainText('8');
 expect(new URL(page.url()).searchParams.get('p')).toBe('0');await expect(page.locator('.pagination button').first()).toBeDisabled();await expect(page.locator('.pagination button').last()).toBeDisabled();
});


test('favoriting a work keeps its in-flight media query alive',async({page,context})=>{
 await setup(context);let release!:()=>void;const pending=new Promise<void>(resolve=>{release=resolve;});let assetReads=0;
 await context.route('**/api/v1/works/work-one/assets',async route=>{assetReads++;await pending;return route.fallback();});
 try{
  await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();await expect.poll(()=>assetReads).toBe(1);
  await detail.getByRole('button',{name:'Favorite work',exact:true}).click();
  await expect(detail.getByRole('button',{name:'Unfavorite work',exact:true})).not.toHaveAttribute('aria-busy','true');
  release();await expect(detail.locator('.detail-media-viewer img').first()).toBeVisible();expect(assetReads).toBe(1);
 }finally{release();}
});


test('favorite still rejects an older in-flight response for the same work detail',async({page,context})=>{
 await setup(context);await page.clock.install();let reads=0,release!:()=>void,returned=false;const pending=new Promise<void>(resolve=>{release=resolve;});
 await context.route('**/api/v1/works/work-one',async route=>{
  if(++reads===1)return route.fallback();await pending;await json(route,{id:'work-one',title:'Harbor light',is_favorite:false,asset_count:2,created_at:'2026-10-08T00:00:00Z',curation_state:{visibility:'visible'}});returned=true;
 });
 try{
  await page.goto('/admin/works?work=work-one');let detail=page.getByRole('dialog').first();await expect(detail.locator('.detail-media-viewer img').first()).toBeVisible();
  await detail.locator('.dialog-top button').first().click();await expect(detail).toHaveCount(0);await page.clock.fastForward(31_000);
  await page.locator('[data-work-id="work-one"]').getByRole('link',{name:'Harbor light',exact:true}).click();detail=page.getByRole('dialog').first();await expect.poll(()=>reads).toBe(2);
  await detail.getByRole('button',{name:'Favorite work',exact:true}).click();await expect(detail.getByRole('button',{name:'Unfavorite work',exact:true})).not.toHaveAttribute('aria-busy','true');
  release();await expect.poll(()=>returned).toBe(true);await expect(detail.getByRole('button',{name:'Unfavorite work',exact:true})).toHaveAttribute('aria-pressed','true');
 }finally{release();}
});


test('failed image explains loading failure and a different asset still loads',async({page,context})=>{
 await setup(context);let calls=0;
 await context.route('**/media/preview/asset-one',route=>{calls++;return json(route,{detail:'File not found'},404);});
 await page.goto('/admin/works?work=work-one');const detail=page.getByRole('dialog').first();
 await expect(detail.locator('.detail-media-viewer')).toContainText('Image failed to load');
 expect(calls).toBe(1);
 await detail.getByRole('button',{name:'Next image',exact:true}).click();
 await expect(detail.locator('.detail-media-viewer img').first()).toBeVisible();
 await expect(detail.locator('.detail-media-viewer img').first()).toHaveAttribute('src','/media/preview/asset-two');
});
