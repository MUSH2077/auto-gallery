'use strict';
const {python} = require('../bridge.cjs');
module.exports = {
  name: 'import', mode: 'finite', via: ['native'], concurrency: [1],
  async prepare(context) {
    const providers = context.config.fixtures.providers;
    return {providers, tasks: providers.length};
  },
  async execute(context, prepared, options) {
    const result = await python(context, 'probe.py', {action: 'provider',
      provider: prepared.providers[options.task_index % prepared.providers.length]});
    return {status: 200, bytes: 0, measurements: result};
  },
};
