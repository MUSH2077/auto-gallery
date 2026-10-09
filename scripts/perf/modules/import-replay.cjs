'use strict';
const {browser, python} = require('../bridge.cjs');
const fs = require('node:fs');
module.exports = {
  name: 'import-replay', mode: 'finite', via: ['frontend'], concurrency: [1], repeats: 1,
  async prepare(context) { return {tasks: 1, source: context.config.replay_source || 'pixiv', entry: 'public source-scoped import-from-disk API'}; },
  async execute(context, prepared, options) {
    const before = await python(context, 'control.py', {action: 'inventory'});
    const {client} = await browser(context);
    const page = await client.newPage();
    await page.setExtraHTTPHeaders({'x-perf-case': options.case_name, 'x-perf-operation': context.operationId('replay')});
    let result;
    const begun = performance.now();
    try {
      // The full UI reset also visits unsupported historical media. This
      // module uses the existing API's documented source filter to measure
      // supported, unchanged records; it is explicitly an API module test.
      const response = await context.request('/api/v1/admin/library/import-from-disk', 'frontend', {
        method: 'POST', headers: {origin: context.config.frontend, 'x-csrf-token': context.csrf(), 'content-type': 'application/json'},
        body: JSON.stringify({reset_ledger: true, source: prepared.source})});
      const accepted = {status: response.status, body: response.ok ? await response.json() : null};
      if (![200, 202].includes(accepted.status)) throw Error('Native disk import rejected');
      const deadline = Date.now() + context.config.timeout_seconds * 1000;
      while (Date.now() < deadline) {
        context.guard();
        result = await context.json('/api/v1/admin/operations/' + accepted.body.task_id);
        if (['failed', 'cancelled', 'blocked'].includes(result.status)) throw Error('Native disk replay failed');
        if (['complete', 'completed'].includes(result.status)) break;
        await new Promise(r => setTimeout(r, 2000));
      }
      if (!['complete', 'completed'].includes(result.status)) throw Error('Native disk replay timeout');
      const scan_ms = performance.now() - begun;
      const statistics = result.result?.stats || result.result || {};
      const ids = statistics.import_job_ids || [];
      if (ids.length) {
        let waiting = new Set(ids);
        while (Date.now() < deadline && waiting.size) {
          const state = await python(context, 'control.py', {action: 'imports', ids: [...waiting]});
          for (const item of state.items) {
            if (item.status === 'failed') throw Error('Replay child import failed');
            if (['complete', 'completed'].includes(item.status)) waiting.delete(item.id);
          }
          // Native task compaction can remove completed domain rows; use a
          // captured committed status, rather than treating a 404 as success.
          const trace = context.config.output + '/events.jsonl';
          if (waiting.size && fs.existsSync(trace)) {
            for (const line of fs.readFileSync(trace, 'utf8').split('\n')) {
              let event; try { event = JSON.parse(line); } catch { continue; }
              if (event.event === 'import_persisted_status' && ['complete', 'completed'].includes(event.status)) waiting.delete(event.job_id);
            }
          }
          if (waiting.size) await new Promise(r => setTimeout(r, 2000));
        }
        if (waiting.size) throw Error('Replay child imports not confirmed complete');
      }
      const after = await python(context, 'control.py', {action: 'inventory'});
      const unchanged = ['work_ids', 'asset_ids'].every(key => before[key].length === after[key].length && before[key].every(v => after[key].includes(v)));
      return {status: unchanged ? 200 : 500, bytes: 0, measurements: {
        scan_ms, all_imports_ms: performance.now() - begun, stats: statistics, receipt: accepted.body,
        work_count_before: before.work_ids.length, work_count_after: after.work_ids.length,
        asset_count_before: before.asset_ids.length, asset_count_after: after.asset_ids.length,
        ids_unchanged: unchanged, source: prepared.source, mode: 'public_api_source_scoped_idempotent_replay'}};
    } finally { await page.close(); }
  },
};
