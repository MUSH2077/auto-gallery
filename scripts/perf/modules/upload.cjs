'use strict';
const {browser, action, settle} = require('../bridge.cjs');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {pipeline} = require('node:stream/promises');
async function exportFixture(context, asset) {
  const target = context.config.output + '/upload-fixtures/' + asset.id + '_' + path.basename(asset.file_path);
  fs.mkdirSync(path.dirname(target), {recursive: true});
  const child = spawn('docker', ['exec', context.config.backend_container, 'cat', '/downloads/' + asset.file_path]);
  let stderr = '';
  child.stderr.on('data', b => stderr += b);
  const exited = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', code => code ? reject(Error('Fixture export failed: ' + stderr)) : resolve());
  });
  await Promise.all([pipeline(child.stdout, fs.createWriteStream(target, {mode: 0o600})), exited]);
  if (fs.statSync(target).size !== asset.bytes) throw Error('Exported fixture byte count differs from recorded asset');
  return {...asset, local_path: target};
}
module.exports = {
  name: 'upload', mode: 'finite', via: ['frontend'],
  reserveBytes(prepared, index) { return prepared.batches[index % prepared.batches.length].reduce((sum, x) => sum + x.bytes, 0) * 2 + 1000000; },
  async prepare(context) {
    const assets = context.config.fixtures.assets.filter(x => ['image/jpeg', 'image/png'].includes(x.mime))
      .sort((a, b) => a.bytes - b.bytes);
    let small = assets[0], medium = assets.find(x => x.bytes >= 1000000) || small;
    let large = assets.filter(x => x.bytes <= 8000000).at(-1) || medium;
    if (!small) throw Error('No image upload fixture');
    const exported = new Map();
    for (const asset of [small, medium, large]) if (!exported.has(asset.id)) exported.set(asset.id, await exportFixture(context, asset));
    [small, medium, large] = [small, medium, large].map(asset => exported.get(asset.id));
    return {batches: [[small], [medium], [small, medium], [large]], tasks: context.config.tasks || null,
      bytes_per_batch: [small.bytes, medium.bytes, small.bytes + medium.bytes, large.bytes]};
  },
  async execute(context, prepared, options) {
    const files = prepared.batches[options.task_index % prepared.batches.length];
    const title = context.operationId('upload');
    const {client} = await browser(context);
    const page = await client.newPage();
    page.setDefaultTimeout(60000);
    await page.route('**/*', async route => {
      const request = route.request();
      if (new URL(request.url()).origin === new URL(context.config.frontend).origin) {
        await route.continue({headers: {...await request.allHeaders(), 'x-perf-case': options.case_name, 'x-perf-operation': title}});
      } else {
        await route.continue();
      }
    });
    try {
      async function preparePage() {
      await page.goto(context.config.frontend + '/admin/upload');
      // This banner is rendered only after the authenticated client query.
      // Waiting for it avoids interacting with enabled SSR controls before hydration.
      await page.getByText('上传配额', {exact: true}).waitFor({state: 'visible'});
      await page.getByPlaceholder('应用于本批次每个作品', {exact: true}).fill(title);
      await page.getByLabel('合并为一个作品（多页）', {exact: true}).check();
      await page.locator('input[type=file]').setInputFiles(files.map(x => x.local_path));
      }
      await preparePage();
      const submit = page.getByRole('button', {name: `开始上传（${files.length}）`, exact: true});
      await submit.waitFor({state: 'visible'});
      const before = performance.now();
      let response;
      const attempts = [];
      for (let attempt = 0; attempt < 3; attempt++) {
        const at = performance.now();
        response = await action(page, '/api/upload', () => submit.click());
        attempts.push({status: response.status, error_code: response.error_code || null,
          response_ms: performance.now() - at, retry_after: response.retry_after || null});
        if (response.status !== 503 || response.error_code !== 'maintenance_in_progress' || attempt === 2) break;
        const wait = Math.max(1, Number(response.retry_after) || 10);
        attempts.at(-1).retry_wait_s = wait;
        await new Promise(r => setTimeout(r, wait * 1000));
        context.guard();
        // The server rejected this attempt before file persistence. Follow its
        // Retry-After protocol using the same ordinary page form.
        await preparePage();
      }
      const upload_ms = performance.now() - before;
      if (response.status !== 200) return {status: response.status, bytes: files.reduce((s, x) => s + x.bytes, 0), measurements: {upload_ms, attempts, error_code: response.error_code, retry_after: response.retry_after}};
      context.pendingJobs.add(response.body.download_job_id);
      const pipeline = await settle(context, response.body, files.length, title);
      context.pendingJobs.delete(response.body.download_job_id);
      return {status: 200, bytes: files.reduce((s, x) => s + x.bytes, 0), measurements: {
        title, receipt: response.body, pages: files.length, upload_response_ms: upload_ms, attempts, ...pipeline,
        mode: 'real_page_upload_to_import_media_and_search'}};
    } catch (error) {
      const target = context.config.output + '/' + title;
      await page.screenshot({path: target + '.png'});
      require('node:fs').writeFileSync(target + '.txt', await page.locator('main').innerText());
      throw error;
    } finally {
      await page.close();
    }
  },
};
