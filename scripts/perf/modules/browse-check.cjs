"use strict";
module.exports=async function browseCheck(context,assets){
 const listing=await context.json('/api/v1/works?offset=0&limit=100');
 const listed=new Set(listing.items.map(work=>work.id));
 const result={assets:0,thumb_bytes:0,source_probe_bytes:0,browser_images:[]};
 for(const asset of assets){
  if(!listed.has(asset.work_id))throw Error('Imported work is absent from the public list');
  const thumb=await context.request('/media/thumb/'+asset.asset_id);
  const thumbBytes=(await thumb.arrayBuffer()).byteLength;
  if(thumb.status!==200||!thumbBytes)throw Error('Public thumbnail is unreadable: '+thumb.status);
  const original=await context.request('/media/original/'+asset.asset_id,'frontend',{headers:{range:'bytes=0-65535'}});
  const sourceBytes=(await original.arrayBuffer()).byteLength;
  if(![200,206].includes(original.status)||!sourceBytes)throw Error('Public source media is unreadable: '+original.status);
  result.assets++;result.thumb_bytes+=thumbBytes;result.source_probe_bytes+=sourceBytes;
 }
 const {client}=await require('../bridge.cjs').browser(context);
 const page=await client.newPage();
 const runtimeErrors=[],consoleErrors=[];
 page.on('pageerror',error=>runtimeErrors.push(error.message));
 page.on('console',message=>{if(message.type()==='error')consoleErrors.push(message.text());});
 try{
  await page.goto(context.config.frontend+'/admin/works');
  result.page_identity={url:page.url(),title:await page.title()};
  if(!page.url().endsWith('/admin/works')||!(await page.locator('main').innerText()).trim())throw Error('Wrong or blank works page');
  const first=assets[0];
  const firstFiles=await context.json('/api/v1/works/'+first.work_id+'/assets');
  const firstFile=firstFiles.find(item=>item.id===first.asset_id);
  const thumbnailPath=new URL(firstFile.thumb_url,context.config.frontend).pathname;
  await page.waitForFunction(path=>[...document.images].some(image=>image.src.includes(path)&&image.complete&&image.naturalWidth>0),thumbnailPath);
  result.list_thumbnail_loaded=true;
  for(const asset of assets){
   const files=await context.json('/api/v1/works/'+asset.work_id+'/assets');
   const file=files.find(item=>item.id===asset.asset_id);
   if(!file)throw Error('Imported asset is missing from the work');
   await page.goto(context.config.frontend+'/admin/works/'+asset.work_id);
   await page.getByTitle(/^(查看全图|View full)$/).click();
   const image=page.getByRole('dialog').getByRole('img',{name:file.file_name,exact:true});
   await image.waitFor();
   const originalPath=new URL(file.original_url,context.config.frontend).pathname;
   await page.waitForFunction(path=>[...document.querySelectorAll('[role="dialog"] img')].some(image=>image.src.includes(path)&&image.complete&&image.naturalWidth>0),originalPath,{timeout:60000});
   result.browser_images.push({asset_id:asset.asset_id,...await image.evaluate(node=>({width:node.naturalWidth,height:node.naturalHeight}))});
  }
  await page.screenshot({path:context.config.output+'/media-browse.png'});
  if(runtimeErrors.length)throw Error('Browser runtime error: '+runtimeErrors[0]);
  result.runtime_errors=runtimeErrors;
  result.console_errors=consoleErrors;
 }finally{await page.close();}
 return result;
};
