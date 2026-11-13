'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {test} = require('node:test');

function runBudgetCase({sampling, limit}) {
  const repo = path.resolve(__dirname, '../../..');
  const artifacts = path.join(repo, 'data/local-perf/runner-tests');
  fs.mkdirSync(artifacts, {recursive: true});
  const output = fs.mkdtempSync(path.join(artifacts, 'storage-'));
  const moduleFile = path.join(output, 'finite.cjs');
  fs.writeFileSync(moduleFile, [
    "const fs = require('node:fs');",
    "module.exports = {",
    "  name: 'budget-upload', mode: 'finite', via: ['frontend'],",
    "  prepare: async () => ({tasks: 2}), reserveBytes: () => 60,",
    "  async execute(context) {",
    "    const file = context.config.output + '/executed.json';",
    "    const count = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) + 1 : 1;",
    "    fs.writeFileSync(file, JSON.stringify(count));",
    "    if (context.config.resource_interval)",
    "      fs.writeFileSync(context.config.output + '/resource-latest.json',",
    "                       JSON.stringify({storage_bytes: count * 60}));",
    "    return {status: 200, bytes: 30};",
    "  },",
    "};",
  ].join('\n'));
  // Only remove benchmark warmup delays; exercise the real reservation loop.
  const preload = path.join(output, 'timers.cjs');
  fs.writeFileSync(preload,
    'const original = global.setTimeout; global.setTimeout = (fn, ms, ...args) => original(fn, 0, ...args);');
  const config = {
    scope: 'upload', native_only: true, output, modules: [moduleFile],
    run_id: 'storage-test', limits: {}, concurrency: [1], repeats: 1,
    resource_interval: sampling ? 2 : 0, initial_runtime_bytes: 0,
    storage_stop_bytes: limit, seconds: 1,
  };
  const file = path.join(output, 'config.json');
  fs.writeFileSync(file, JSON.stringify(config));
  const child = spawnSync(process.execPath,
    ['--require', preload, path.resolve(__dirname, '../runner.cjs'), file],
    {encoding: 'utf8', timeout: 10000});
  assert.equal(child.error, undefined);
  return {
    status: child.status,
    report: JSON.parse(fs.readFileSync(path.join(output, 'requests.json'))),
    executed: JSON.parse(fs.readFileSync(path.join(output, 'executed.json'))),
  };
}

test('unsampled upload retains completed storage reservations before the next request', () => {
  const result = runBudgetCase({sampling: false, limit: 100});
  assert.equal(result.status, 1);
  assert.equal(result.executed, 1);
  assert.equal(result.report.cases[0].errors, 1);
  assert.match(result.report.cases[0].requests[1].error_message, /Insufficient runtime storage budget/);
});

test('sampled storage is not double counted with completed reservations', () => {
  const result = runBudgetCase({sampling: true, limit: 150});
  assert.equal(result.status, 0);
  assert.equal(result.executed, 2);
  assert.equal(result.report.cases[0].errors, 0);
});
