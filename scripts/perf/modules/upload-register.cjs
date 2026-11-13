'use strict';
const fs = require('node:fs');
const receive = require('./upload-receive.cjs');
const {python} = require('../bridge.cjs');

// Measure upload responses with normal background workers, then inspect only
// the accepted batch. No browser launch or pipeline observer runs during timing.
module.exports = {
  ...receive, name: 'upload-register',
  async prepare(context) {
    const fixture = context.config.fixtures;
    if (!fixture?.upload_inputs?.length || !fixture.upload_batches?.length)
      throw Error('Provide upload_inputs and upload_batches in --fixtures');
    const files = new Map(fixture.upload_inputs.map(file => {
      const size = fs.statSync(file.local_path).size;
      if (size !== file.bytes) throw Error('Upload fixture byte count differs');
      return [file.id, file];
    }));
    const batches = fixture.upload_batches.map(ids => ids.map(id => {
      const file = files.get(id);
      if (!file) throw Error('Unknown upload fixture ID');
      return file;
    }));
    if (batches.some(batch => !batch.length)) throw Error('Empty upload fixture batch');
    return {batches, tasks: context.config.tasks || null, accepted: []};
  },
  async setup() {},
  async cleanup(context, prepared) {
    if (!prepared.accepted.length) return;
    const start = performance.now(), deadline = start + context.config.timeout_seconds * 1000;
    const items = prepared.accepted.map(job => ({
      job_id: job.receipt.download_job_id, work_source_id: job.receipt.work_id,
    }));
    let latest;
    while (performance.now() < deadline) {
      context.guard();
      latest = await python(context, 'control.py', {action: 'upload_batch', items});
      if (latest.items.some(item => ['failed', 'blocked', 'cancelled'].includes(item.status)))
        throw Error('Accepted upload failed to import');
      if (latest.items.every((item, i) =>
        item.assets.length === prepared.accepted[i].pages && item.indexed &&
        item.assets.every(asset => asset.state === 'complete' && asset.thumb && asset.sha256 && asset.phash))) {
        for (let i = 0; i < prepared.accepted.length; i++) {
          const job = prepared.accepted[i];
          const result = await context.json('/api/v1/works?q=' + encodeURIComponent(job.title) + '&limit=20');
          if (!result.items.some(work => work.id === latest.items[i].assets[0].work_id))
            throw Error('Uploaded title missing from actual search');
          context.pendingJobs.delete(job.receipt.download_job_id);
        }
        context.recordRestoration({upload_batch_validation: {
          wall_s: (performance.now() - start) / 1000,
          observation_interval_ms: 1000, jobs: latest.items,
        }});
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    context.recordRestoration({upload_batch_validation: {wall_s: (performance.now() - start) / 1000, jobs: latest?.items, incomplete: true}});
    throw Error('Accepted upload batch did not complete');
  },
};
