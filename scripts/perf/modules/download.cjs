'use strict';
const {browser, action, python} = require('../bridge.cjs');
const fixed = [
  ['pixiv', 'https://www.pixiv.net/users/7324626'],
  ['x', 'https://x.com/Eul_bong'],
  ['danbooru', 'https://danbooru.donmai.us/posts?tags=yokoyari_mengo'],
];
module.exports = {
  name: 'download', mode: 'finite', via: ['frontend'],
  reserveBytes() { return 2000000000; },
  async prepare(context) {
    const repositories = fixed.map(([source, url]) => context.config.fixtures.repositories.find(r => r.source === source && r.url === url));
    if (repositories.some(x => !x)) throw Error('A fixed artist repository is missing; create it in the normal subscription UI first');
    const settings = await context.json('/api/v1/admin/settings');
    // Only non-secret download limits are retained for restoration.
    return {repositories, tasks: repositories.length, defaults: {
      max_posts: settings.download_defaults.max_posts, gallerydl_abort: settings.download_defaults.gallerydl_abort}};
  },
  async setup(context, prepared) {
    const {client} = await browser(context);
    const page = await client.newPage();
    try {
      await page.goto(context.config.frontend + '/admin/settings/download-defaults');
      await page.getByLabel('最大帖子数', {exact: true}).fill(String(context.config.download_posts));
      await page.getByLabel('连续跳过阈值', {exact: true}).fill('20');
      const result = await action(page, '/api/v1/admin/settings', () => page.getByRole('button', {name: '保存设置', exact: true}).click());
      if (result.status !== 200) throw Error('Saving bounded download defaults failed');
    } finally { await page.close(); }
  },
  async execute(context, prepared, options) {
    const repository = prepared.repositories[options.task_index % prepared.repositories.length];
    const {client} = await browser(context);
    const page = await client.newPage();
    page.setDefaultTimeout(60000);
    await page.setExtraHTTPHeaders({'x-perf-case': options.case_name, 'x-perf-operation': context.operationId('download')});
    let receipt;
    const before = performance.now();
    try {
      let response;
      if (prepared.direct_requests) {
        const requested = await context.request('/api/v1/download-jobs', 'frontend', {
          method: 'POST', headers: {origin: context.config.frontend, 'x-csrf-token': context.csrf(), 'content-type': 'application/json'},
          body: JSON.stringify({subscription_id: repository.subscription_id, source: repository.source, source_url: repository.url})});
        response = {status: requested.status, body: requested.ok ? await requested.json() : null};
      } else {
        await page.goto(context.config.frontend + '/admin/subscriptions/repositories/' + repository.id);
        response = await action(page, '/api/v1/repositories/' + repository.id + '/sync-now',
          () => page.getByRole('button', {name: '立即同步', exact: true}).first().click());
      }
      if (![200, 201, 202].includes(response.status)) throw Error('Native sync returned HTTP ' + response.status);
      receipt = response.body;
      context.pendingJobs.add(receipt.job_id);
      const deadline = performance.now() + context.config.timeout_seconds * 1000;
      let snapshot, terminal;
      while (performance.now() < deadline) {
        context.guard();
        snapshot = await python(context, 'control.py', {action: 'job', job_id: receipt.job_id, source: repository.source});
        if (snapshot.artifact_bytes > context.config.limits.download_bytes) throw Error('Download budget violated');
        if (['failed', 'blocked', 'cancelled'].includes(snapshot.status)) {
          context.pendingJobs.delete(receipt.job_id);
          return {status: 500, bytes: snapshot.artifact_bytes, measurements: {source: repository.source, receipt, snapshot}};
        }
        if (snapshot.status === 'complete') {
          terminal ??= performance.now() - before;
          if (!snapshot.media_files || (snapshot.assets.length && snapshot.assets.every(a => a.state === 'complete' && a.sha256 && a.thumb &&
            (!['image/jpeg', 'image/png'].includes(a.mime) || a.phash)) && snapshot.indexed)) {
            context.pendingJobs.delete(receipt.job_id);
            return {status: 200, bytes: snapshot.artifact_bytes, measurements: {source: repository.source,
              receipt, terminal_ms: terminal, all_ready_ms: performance.now() - before,
              classification: snapshot.media_files ? 'download_and_import' : 'archive_hit_no_new_media', snapshot}};
          }
        }
        await new Promise(r => setTimeout(r, 2000));
      }
      throw Error('Native download/derive/index timeout');
    } finally { await page.close(); }
  },
  async cleanup(context, prepared) {
    const {client} = await browser(context);
    const page = await client.newPage();
    try {
      await page.goto(context.config.frontend + '/admin/settings/download-defaults');
      await page.getByLabel('最大帖子数', {exact: true}).fill(String(prepared.defaults.max_posts));
      await page.getByLabel('连续跳过阈值', {exact: true}).fill(String(prepared.defaults.gallerydl_abort));
      const result = await action(page, '/api/v1/admin/settings', () => page.getByRole('button', {name: '保存设置', exact: true}).click());
      if (result.status !== 200) throw Error('Restoring download defaults failed');
      context.recordRestoration({download_defaults_restored: true});
    } finally { await page.close(); }
  },
};
