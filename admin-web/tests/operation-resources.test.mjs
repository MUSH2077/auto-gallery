import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "../src/lib/api/index.ts";
import { refreshOperationResources } from "../src/lib/operationRefresh.ts";

test("clear operations invalidate the actual queue, storage and domain query keys", () => {
  const client = new QueryClient();
  const keys = [queryKeys.system.queueStats, queryKeys.system.systemInfo, queryKeys.system.storageBreakdown,
    queryKeys.downloadJobs.all, queryKeys.importJobs.all, queryKeys.tasks.all, queryKeys.workbench];
  for (const key of keys) client.setQueryData(key, { ready: true });
  client.setQueryData(queryKeys.creators.count, { count: 7 });
  refreshOperationResources(client, "admin-clear", { entity: "all" });
  for (const key of keys) assert.equal(client.getQueryState(key)?.isInvalidated, true, JSON.stringify(key));
  assert.deepEqual(client.getQueryData(queryKeys.creators.count), { count: 7 });
  client.clear();
});

test("download import list aliases use one request and preserve the response", async () => {
  assert.equal(api.listDownloadJobImports, api.getDownloadJobImports);
  const original = globalThis.fetch;
  const body = [{ id: "child", download_job_id: "parent", status: "paused", progress_data: { stage: "paused" } }];
  const calls = [];
  globalThis.fetch = async (url) => { calls.push(url); return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } }); };
  try {
    assert.deepEqual(await api.getDownloadJobImports("parent"), body);
    assert.deepEqual(await api.listDownloadJobImports("parent"), body);
    assert.deepEqual(calls, ["/api/v1/download-jobs/parent/imports", "/api/v1/download-jobs/parent/imports"]);
  } finally { globalThis.fetch = original; }
});

test("global completion refresh covers the data-management callbacks it owns", () => {
  for (const [kind, keys] of [
    ["admin-backup-create", [queryKeys.backups.all, queryKeys.backups.list]],
    ["admin-cleanup-metadata-jsons", [queryKeys.system.systemInfo, queryKeys.system.storageBreakdown]],
    ["admin-creator-reenrich", [queryKeys.system.systemInfo, queryKeys.system.storageBreakdown]],
  ]) {
    const client = new QueryClient();
    for (const key of keys) client.setQueryData(key, { ready: true });
    refreshOperationResources(client, kind);
    for (const key of keys) assert.equal(client.getQueryState(key)?.isInvalidated, true, `${kind}: ${key}`);
    client.clear();
  }
});
