'use strict';
const {python} = require('../bridge.cjs');
module.exports = {
  name: 'media', mode: 'finite', via: ['native'], concurrency: [1],
  async prepare(context) {
    const ids = context.config.asset_ids;
    const fixtures = context.config.fixtures.assets.filter(x => !ids || ids.includes(x.id));
    if (ids && ids.some(id => !fixtures.some(x => x.id === id))) throw Error('Requested media fixture is missing');
    const formats = [...new Set(fixtures.map(x => x.mime))];
    const assets = ids ? fixtures : formats.flatMap(format => fixtures.filter(x => x.mime === format)
      .sort((a, b) => (a.width * a.height) - (b.width * b.height))
      .filter((x, i, a) => i === 0 || i === a.length - 1).slice(0, context.config.samples));
    if (!assets.length) throw Error('No media fixtures');
    const stages = context.config.media_stages || ['thumbnail_and_dimensions', 'sha256', 'phash', 'native_derive_sync'];
    const tasks = context.config.media_sequence
      ? stages.map(stage => ({assets, stage}))
      : assets.flatMap(asset => stages.map(stage => ({asset, stage})));
    return {assets, operations: tasks, tasks: tasks.length,
      sample_policy: ids ? 'explicit fixture IDs' : 'smallest and largest pixel counts per format'};
  },
  async execute(context, prepared, options) {
    const {asset, assets, stage} = prepared.operations[options.task_index];
    const result = await python(context, 'probe.py', {action: assets ? 'media_sequence' : 'media', asset, assets, stages: [stage],
      iterations: context.config.media_iterations, measurement: context.config.media_measurement,
      sample_ms: context.config.media_sample_ms, operation_id: context.operationId('media')});
    return {status: 200, bytes: result.bytes, measurements: result};
  },
};
