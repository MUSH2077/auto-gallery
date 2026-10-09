// Shared module runner. Authentication stays in memory; no browser session file is needed.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {performance} = require('node:perf_hooks');
let sequence = 0, currentCase = 'setup';
let reservedBytes = 0;
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const report = {scope: config.scope, started_at: new Date().toISOString(), cases: [], limits: config.limits, fixtures: {}};
let cookie = '', csrf = '';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function save() {
  const target = path.join(config.output, 'requests.json');
  fs.writeFileSync(target + '.tmp', JSON.stringify(report, null, 2));
  fs.renameSync(target + '.tmp', target);
}
function phase(name) {
  const target = path.join(config.output, 'phase.json');
  fs.writeFileSync(target + '.tmp', JSON.stringify({name}));
  fs.renameSync(target + '.tmp', target);
  console.log(name);
}
function guard() {
  if (fs.existsSync(path.join(config.output, 'stop.txt'))) throw Error(fs.readFileSync(path.join(config.output, 'stop.txt'), 'utf8').trim());
}
async function request(url, options = {}) {
  const response = await fetch(url, {signal: AbortSignal.timeout(30000), ...options,
    headers: {cookie, 'x-perf-case': currentCase, ...options.headers}});
  response.perfMeasurements = Object.fromEntries(['sql-ms', 'sql-count', 'app-ms'].flatMap(key => {
    const value = response.headers.get('x-perf-' + key);
    return value === null ? [] : [[key.replaceAll('-', '_'), Number(value)]];
  }));
  return response;
}
async function json(url) {
  const r = await request(url);
  if (!r.ok) { await r.arrayBuffer(); throw Error('Setup request returned HTTP ' + r.status); }
  return r.json();
}
let existingWorks;
const context = {
  config,
  guard,
  cookie: () => cookie,
  csrf: () => csrf,
  operationId: prefix => `${prefix}-${config.run_id}-${sequence++}`,
  recordRestoration: values => { Object.assign(report, values); save(); },
  pendingJobs: new Set(),
  async reserve(bytes) {
    while (true) {
      guard();
      const latest = path.join(config.output, 'resource-latest.json');
      const storage = fs.existsSync(latest) ? JSON.parse(fs.readFileSync(latest)).storage_bytes : config.initial_runtime_bytes;
      if ((storage ?? config.initial_runtime_bytes) + reservedBytes + bytes <= config.storage_stop_bytes) {
        reservedBytes += bytes;
        return () => { reservedBytes -= bytes; };
      }
      if (!reservedBytes) throw Error('Insufficient runtime storage budget for the next finite operation');
      await sleep(500);
    }
  },
  request: (pathname, via = 'frontend', options = {}) => request((via === 'frontend' ? config.frontend : config.backend) + pathname, options),
  json: (pathname, via = 'frontend') => json((via === 'frontend' ? config.frontend : config.backend) + pathname),
  async works() {
    existingWorks ??= await this.json('/api/v1/works?offset=0&limit=20');
    if (!existingWorks.items?.length) throw Error('No existing works; import real media in the UI before benchmarking');
    return existingWorks;
  },
};
function percentile(values, p) {
  if (!values.length) return null;
  const sorted = values.slice().sort((a, b) => a - b);
  return sorted[Math.ceil(sorted.length * p) - 1];
}
async function runCase(module, prepared, via, concurrency, repeat) {
  guard();
  const finite = module.mode === 'finite';
  phase(`warmup-${module.name}-${via}`);
  for (let i = 0; i < (finite ? 0 : 5); i++) {
    const result = await module.execute(context, prepared, {via});
    if (result.status !== 200) throw Error('Warmup returned HTTP ' + result.status);
  }
  await sleep(1000);
  const name = `${module.name}-${via}-c${concurrency}-r${repeat}`;
  currentCase = name;
  phase(name);
  const item = {name, route: module.name, module_file: module.file, path: prepared.path, via, concurrency, repeat,
    mode: finite ? 'finite' : 'timed', started_at: new Date().toISOString(), requests: [], statuses: {}, errors: 0};
  report.cases.push(item);
  save();
  const started = performance.now(), end = started + config.seconds * 1000;
  let taskIndex = 0;
  const tasks = finite ? (prepared.tasks || concurrency) : Infinity;
  item.task_count = finite ? tasks : null;
  item.actual_concurrency = Math.min(concurrency, tasks);
  await Promise.all(Array.from({length: item.actual_concurrency}, async () => {
    while (finite ? taskIndex < tasks : performance.now() < end) {
      guard();
      const index = taskIndex++;
      const at = performance.now();
      let status = null, bytes = 0, error = null, error_message, measurements;
      let release;
      try {
        if (module.reserveBytes) release = await context.reserve(module.reserveBytes(prepared, index));
        const result = await module.execute(context, prepared, {via, task_index: index, repeat, concurrency, case_name: name});
        status = result.status;
        bytes = result.bytes;
        measurements = result.measurements;
        if (release) measurements = {...measurements, storage_reservation_bytes: module.reserveBytes(prepared, index)};
      } catch (e) { error = e.name; error_message = e.message; }
      finally { release?.(); }
      const latency_ms = performance.now() - at;
      item.requests.push({offset_ms: at - started, latency_ms, status, bytes, error, error_message, measurements});
      item.statuses[status ?? 'network-error'] = (item.statuses[status ?? 'network-error'] || 0) + 1;
      if (status !== 200 || error) item.errors++;
      if (finite) save();
    }
  }));
  item.ended_at = new Date().toISOString();
  item.elapsed_s = (performance.now() - started) / 1000;
  item.qps = item.requests.length / item.elapsed_s;
  // Error latency is retained separately and must not improve the success percentiles.
  const times = item.requests.filter(x => x.status === 200 && !x.error).map(x => x.latency_ms);
  item.p50_ms = percentile(times, 0.5);
  item.p95_ms = percentile(times, 0.95);
  item.p99_ms = percentile(times, 0.99);
  item.max_ms = times.length ? Math.max(...times) : null;
  item.completed = true;
  save();
  console.log(JSON.stringify({case: name, requests: item.requests.length, qps: item.qps, p95_ms: item.p95_ms, errors: item.errors}));
  phase('cooldown');
  await sleep(1000);
}
(async () => {
  try {
    if (!config.native_only) {
    const credentials = JSON.parse(fs.readFileSync(config.credentials, 'utf8'));
    const login = await request(config.frontend + '/api/v1/auth/browser/login', {
      method: 'POST', headers: {'content-type': 'application/json', origin: config.frontend},
      body: JSON.stringify(credentials)});
    if (!login.ok) { await login.arrayBuffer(); throw Error('Login returned HTTP ' + login.status + '; update data/local-perf/login.json if password changed'); }
    const cookies = login.headers.getSetCookie().map(x => x.split(';')[0]).filter(x => !x.endsWith('='));
    cookie = cookies.join('; ');
    csrf = cookies.find(x => x.startsWith('ag_csrf='))?.slice('ag_csrf='.length) || '';
    if ((await login.json()).must_change_password) throw Error('Complete the initial password change in the UI first');
    }
    const modules = [];
    for (const file of config.modules) {
      const module = {...require(file), file};
      if (!module.name || typeof module.prepare !== 'function' || typeof module.execute !== 'function')
        throw Error('Module must export name, prepare(context), execute(context, prepared, options): ' + file);
      if (modules.some(x => x.module.name === module.name)) throw Error('Duplicate module name: ' + module.name);
      const prepared = await module.prepare(context);
      modules.push({module, prepared});
      report.fixtures[module.name] = prepared;
    }
    save();
    phase('idle');
    await sleep(5000);
    for (const {module, prepared} of modules) {
      try {
        if (module.setup) await module.setup(context, prepared);
        for (let repeat = 1; repeat <= (module.repeats || config.repeats); repeat++)
          for (const n of module.concurrency || config.concurrency)
            for (const via of module.via || config.via) await runCase(module, prepared, via, n, repeat);
      } finally {
        if (module.cleanup) await module.cleanup(context, prepared);
      }
    }
    report.completed_at = new Date().toISOString();
    if (report.cases.some(x => x.errors)) process.exitCode = 1;
  } catch (e) {
    report.error = e.message;
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    for (const jobId of context.pendingJobs) {
      try {
        const response = await request(config.frontend + '/api/v1/download-jobs/' + jobId + '/cancel', {
          method: 'POST', headers: {origin: config.frontend, 'x-csrf-token': csrf, 'content-type': 'application/json'},
          body: JSON.stringify({note: 'Local performance test cleanup'})});
        (report.cleanup_jobs ??= []).push({job_id: jobId, cancel_http: response.status});
        await response.arrayBuffer();
      } catch (error) {
        (report.cleanup_jobs ??= []).push({job_id: jobId, error: error.message});
        process.exitCode = 1;
      }
    }
    if (context.controlProcess) {
      context.controlProcess.child.stdin.end();
      const timer = setTimeout(() => context.controlProcess.child.kill('SIGTERM'), 5000);
      await context.controlProcess.closed;
      clearTimeout(timer);
    }
    if (context.browser) await context.browser.instance.close();
    if (cookie && csrf) {
      try {
        const logout = await request(config.frontend + '/api/v1/auth/browser/logout', {
          method: 'POST', headers: {origin: config.frontend, 'x-csrf-token': csrf}});
        await logout.arrayBuffer();
        report.logout_status = logout.status;
      } catch (e) { report.logout_error = e.name; }
    }
    save();
  }
})();
