"use strict";
const base = require('./upload.cjs');
const {settle} = require('../bridge.cjs');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
module.exports = {
  ...base, name: 'upload-receive',
  async prepare(context) { return {...await base.prepare(context), accepted: []}; },
  async setup(context) {
    if (context.config.isolated_upload) for (const role of ['import', 'operations'])
      execFileSync('docker', ['pause', 'auto-gallery-perf-worker-' + role + '-1']);
  },
  async execute(context, prepared, options) {
    const files = prepared.batches[options.task_index % prepared.batches.length];
    const title = context.operationId('receive');
    const data = new FormData();
    data.set('title', title);
    for (const file of files) data.append('files', await fs.openAsBlob(file.local_path), path.basename(file.local_path));
    const at = performance.now();
    const response = await context.request('/api/v1/upload', 'frontend', {
      method: 'POST', signal: AbortSignal.timeout(120000),
      headers: {origin: context.config.frontend, 'x-csrf-token': context.csrf(), 'x-perf-operation': title}, body: data});
    const response_ms = performance.now() - at;
    const body = await response.json().catch(() => null);
    if (response.ok) {
      context.pendingJobs.add(body.download_job_id);
      prepared.accepted.push({receipt: body, pages: files.length, title});
    }
    return {status: response.status, bytes: files.reduce((s, f) => s + f.bytes, 0),
      measurements: {title, receipt: response.ok ? body : null, error_code: body?.detail?.code,
        pages: files.length, upload_response_ms: response_ms, ...response.perfMeasurements}};
  },
  async cleanup(context, prepared) {
    // Primary measurements finish at the HTTP response; asynchronous completion
    // is checked separately, with at most eight observers.
    if (context.config.isolated_upload) for (const role of ['import', 'operations'])
      execFileSync('docker', ['unpause', 'auto-gallery-perf-worker-' + role + '-1']);
    const start = performance.now(), results = [];
    let next = 0;
    await Promise.all(Array.from({length: Math.min(8, prepared.accepted.length)}, async () => {
      while (next < prepared.accepted.length) {
        const job = prepared.accepted[next++];
        const result = await settle(context, job.receipt, job.pages, job.title);
        context.pendingJobs.delete(job.receipt.download_job_id);
        results.push({title: job.title, ...result});
      }
    }));
    context.recordRestoration({upload_tail: {wall_s: (performance.now()-start)/1000, jobs: results}});
  },
};
