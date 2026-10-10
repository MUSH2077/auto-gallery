'use strict';
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const readline = require('node:readline');

function control(context, payload) {
  if (!context.controlProcess) {
    const child = spawn('docker', ['exec', '-i', '-e', 'AUTO_GALLERY_PERF_TRACE=',
      ...(context.config.native_only ? ['-e', 'PYTHONPATH=/app'] : []),
      context.config.backend_container, 'python', '/perf-tools/control.py']);
    const pending = [];
    let stderr = '';
    child.stderr.on('data', b => stderr += b);
    readline.createInterface({input: child.stdout}).on('line', line => {
      const waiter = pending.shift();
      if (!waiter) return;
      try {
        const value = JSON.parse(line);
        if (value.probe_error) waiter.reject(Error('Control probe: ' + value.probe_error + ': ' + value.message));
        else waiter.resolve(value);
      } catch (e) { waiter.reject(e); }
    });
    const closed = new Promise(resolve => child.on('close', code => {
      for (const waiter of pending.splice(0)) waiter.reject(Error('Control process exited ' + code + ': ' + stderr.slice(-1200)));
      resolve();
    }));
    child.on('error', error => { for (const waiter of pending.splice(0)) waiter.reject(error); });
    context.controlProcess = {child, pending, closed};
  }
  return new Promise((resolve, reject) => {
    context.controlProcess.pending.push({resolve, reject});
    context.controlProcess.child.stdin.write(JSON.stringify(payload) + '\n');
  });
}

function python(context, script, payload) {
  if (script === 'control.py') return control(context, payload);
  return new Promise((resolve, reject) => {
    const child = spawn('docker', ['exec', '-i', '-e', 'AUTO_GALLERY_PERF_TRACE=',
      ...(context.config.native_only ? ['-e', 'PYTHONPATH=/app'] : []),
      context.config.backend_container, 'python', '/perf-tools/' + script]);
    let output = '', error = '';
    child.stdout.on('data', b => output += b);
    child.stderr.on('data', b => error += b);
    child.on('error', reject);
    child.on('close', code => {
      if (code) {
        const file = context.config.output + '/' + context.operationId('probe-error') + '.log';
        fs.writeFileSync(file, error);
        return reject(Error(`Probe ${script} failed (${code}); ${file}: ${error.slice(-1200)}`));
      }
      try { resolve(JSON.parse(output)); } catch { reject(Error('Probe returned invalid JSON')); }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

async function browser(context) {
  if (context.browser) return context.browser;
  if (context.browserPromise) return context.browserPromise;
  context.browserPromise = (async () => {
  const {chromium} = require(context.config.playwright_module);
  const instance = await chromium.launch({headless: true});
  const client = await instance.newContext({viewport: {width: 1440, height: 1000}});
  const ownOrigins = new Set([context.config.frontend, context.config.backend].map(url => new URL(url).origin));
  await client.route('**/*', async route => {
    const request = route.request();
    if (ownOrigins.has(new URL(request.url()).origin)) {
      await route.continue({headers: {...await request.allHeaders(), 'x-perf-case': 'ui-' + context.config.run_id}});
    } else {
      // Profiling headers must not trigger CORS preflights on external fonts.
      await route.continue();
    }
  });
  const cookies = context.cookie().split('; ').filter(Boolean).map(s => {
    const at = s.indexOf('=');
    return {name: s.slice(0, at), value: s.slice(at + 1), url: context.config.frontend};
  });
  await client.addCookies(cookies);
  context.browser = {instance, client};
  return context.browser;
  })();
  return context.browserPromise;
}

async function action(page, pathname, perform) {
  const waiting = page.waitForResponse(r => r.request().method() !== 'GET' && new URL(r.url()).pathname === pathname, {timeout: 90000});
  await perform();
  const response = await waiting;
  if (response.ok()) return {status: response.status(), body: await response.json()};
  let code;
  try { code = (await response.json())?.detail?.code; } catch {}
  return {status: response.status(), body: null,
    error_code: typeof code === 'string' ? code : null,
    retry_after: response.headers()['retry-after'] || null};
}

async function settle(context, receipt, pages, title) {
  const begun = performance.now(), deadline = begun + context.config.timeout_seconds * 1000;
  let observedImport, observedMedia, observedSearch, latest;
  let searchUnavailableObservations = 0;
  while (performance.now() < deadline) {
    context.guard();
    latest = await python(context, 'control.py', {action: 'job', job_id: receipt.download_job_id,
      work_source_id: receipt.work_id});
    if (latest.status === 'failed' || latest.imports.some(x => x.status === 'failed')) throw Error('Native import failed');
    if (!observedImport && latest.assets.length === pages) observedImport = performance.now() - begun;
    if (!observedMedia && latest.assets.length === pages && latest.assets.every(x => x.state === 'complete' && x.thumb && x.sha256 &&
      (!['image/jpeg', 'image/png'].includes(x.mime) || x.phash))) observedMedia = performance.now() - begun;
    if (!observedSearch && latest.assets.length) {
      const response = await context.request('/api/v1/works?q=' + encodeURIComponent(title) + '&limit=20');
      const j = await response.json();
      if (response.ok) {
        if (j.items.some(x => x.id === latest.assets[0].work_id)) observedSearch = performance.now() - begun;
      } else if (response.status === 503 && j.detail?.code === 'search_unavailable') {
        // The empty test stack may still be creating its first search index.
        // Keep waiting for actual visibility; the normal settle deadline applies.
        searchUnavailableObservations++;
      } else {
        throw Error('Search inspection returned HTTP ' + response.status);
      }
    }
    if (observedImport !== undefined && observedMedia !== undefined && observedSearch !== undefined)
      return {import_visible_ms: observedImport, media_ready_ms: observedMedia, search_visible_ms: observedSearch,
        snapshots: latest, observation_interval_ms: 2000, search_unavailable_observations: searchUnavailableObservations};
    await new Promise(r => setTimeout(r, 2000));
  }
  throw Error('Native pipeline settle timeout');
}

module.exports = {python, browser, action, settle};
