import assert from "node:assert/strict";
import test from "node:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
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


for (const kind of ["danbooru-import-all", "danbooru-mapping-refresh", "hierarchy-delete", "admin-clear"]) {
  test(`${kind} refreshes each affected active cache once`, async () => {
    const client = new QueryClient();
    const keys = [queryKeys.creators.detail("artist"), queryKeys.creators.links("artist"), queryKeys.creators.count,
      queryKeys.subscriptions.detail("sub"), queryKeys.subscriptions.sources("sub"), queryKeys.subscriptions.count,
      queryKeys.workbench, ["creator-subscription-overview", "artist"]];
    const counts = new Map();
    const unsubscribe = keys.map(queryKey => {
      const observer = new QueryObserver(client, { queryKey, initialData: { version: 0 }, staleTime: Infinity,
        queryFn: async ({signal}) => { const key=JSON.stringify(queryKey);counts.set(key,(counts.get(key)||0)+1);
          await new Promise(resolve => setTimeout(resolve, 10));return {version:1}; } });
      return observer.subscribe(() => {});
    });
    try {
      refreshOperationResources(client, kind, {entity:"all"});
      await new Promise(resolve => setTimeout(resolve, 30));
      for(const key of keys) assert.equal(counts.get(JSON.stringify(key)), 1, JSON.stringify(key));
    } finally {unsubscribe.forEach(stop=>stop());client.clear();}
  });
}

test("completed import also refreshes inactive creator and subscription caches",async()=>{
 const client=new QueryClient();let reads=0;
 const queryKey=queryKeys.creators.detail("inactive");
 try {
  await client.fetchQuery({queryKey,queryFn:async()=>({version:++reads}),staleTime:Infinity});
  refreshOperationResources(client,"danbooru-import-all");
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(reads,2);assert.deepEqual(client.getQueryData(queryKey),{version:2});
 }finally{client.clear();}
});
