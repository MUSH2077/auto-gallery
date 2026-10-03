import assert from "node:assert/strict";
import test from "node:test";
import { api } from "../src/lib/api/index.ts";
import { executeTaskBatch, executeTaskCommand } from "../src/lib/taskCommands.ts";
import { readRepeatSyncIntent } from "../src/lib/task-actions.ts";
import { adaptBatchStatus, restoreBatchRecord, BATCH_RECOVERY_MS } from "../src/lib/batchImportMonitor.ts";
import { QueryClient } from "@tanstack/react-query";
import { acceptOperationQueries } from "../src/lib/operationMonitor.ts";

test("retry intake fences both old status and old latest responses without altering another task", async () => {
  const client = new QueryClient();
  const taskKey = ["admin-operation-task", "same-task"];
  const snapshotKey = ["admin-operation-snapshot", "admin-rebuild", "global"];
  const otherKey = ["admin-operation-snapshot", "admin-rebuild", "other-scope"];
  client.setQueryData(taskKey, { task_id: "same-task", status: "failed" });
  client.setQueryData(snapshotKey, { current: { task_id: "same-task", status: "failed" } });
  client.setQueryData(otherKey, { current: { task_id: "another-task", status: "running" } });
  let statusReply, latestReply;
  const oldStatus = client.fetchQuery({ queryKey: taskKey, queryFn: () => new Promise(resolve => { statusReply = resolve; }) }).catch(() => {});
  const oldLatest = client.fetchQuery({ queryKey: snapshotKey, queryFn: () => new Promise(resolve => { latestReply = resolve; }) }).catch(() => {});
  acceptOperationQueries(client, "same-task", "admin-rebuild", "same-task");
  statusReply({ task_id: "same-task", status: "failed" });
  latestReply({ current: { task_id: "same-task", status: "failed" } });
  await Promise.all([oldStatus, oldLatest]);
  assert.equal(client.getQueryData(taskKey).status, "enqueued");
  assert.equal(client.getQueryData(snapshotKey).current.status, "enqueued");
  assert.equal(client.getQueryData(otherKey).current.task_id, "another-task");
  client.clear();
});

test("single and batch retry forward the minimal accepted identity with task context", async () => {
  const original = api.retryTask;
  const accepted = [];
  api.retryTask = async id => {
    if (id === "refused") throw new Error("conflict");
    return { task_id: id, status: "enqueued" };
  };
  const context = { originalUnavailable: "unavailable", accepted: (task, result) => accepted.push([task, result]) };
  const task = { id: "admin", kind: "admin", operation_type: "admin-rebuild" };
  try {
    await executeTaskCommand(task, "retry", context);
    const batch = await executeTaskBatch([task, { ...task, id: "refused" }], "retry", context);
    assert.equal(accepted.length, 2);
    assert.deepEqual(accepted[0], [task, { task_id: "admin", status: "enqueued" }]);
    assert.equal(batch.succeeded, 1);
    assert.equal(batch.failed, 1);
    assert.equal(batch.errors[0].id, "refused");
  } finally { api.retryTask = original; }
});

test("repeat sync retains one intent after failure and clears only validated acceptance", async () => {
  const previousStorage = globalThis.localStorage;
  const original = api.repeatTask;
  const data = new Map();
  globalThis.localStorage = { getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
  const requests = [];
  const task = { id: "task", subject_type: "download_job", subject_id: "original" };
  const context = { userId: 1, originalUnavailable: "unavailable" };
  try {
    api.repeatTask = async (_id, requestId) => { requests.push(requestId); throw new Error("uncertain"); };
    await assert.rejects(executeTaskCommand(task, "repeat_sync", context), /uncertain/);
    assert.ok(readRepeatSyncIntent(1, "original"));
    api.repeatTask = async (_id, requestId) => {
      requests.push(requestId);
      return { task_id: "new-task", job_id: "new-job", previous_job_id: "original", request_id: requestId, action: "repeat_sync", status: "enqueued" };
    };
    await executeTaskCommand(task, "repeat_sync", context);
    assert.equal(requests[0], requests[1]);
    assert.equal(data.size, 0);
  } finally { globalThis.localStorage = previousStorage; api.repeatTask = original; }
});

test("batch adaptation keeps active empty results distinct from completed results", () => {
  assert.equal(adaptBatchStatus({ status: "running", result: {} }).result, null);
  assert.throws(() => adaptBatchStatus({ status: "complete", result: null }), /no longer available/);
  const result = { imported: [], low_confidence: [], not_found: [], errors: [] };
  assert.equal(adaptBatchStatus({ status: "completed", result }).result, result);
  const old = restoreBatchRecord({ jobId: "old", importType: "url", total: 2, startedAt: 1 });
  assert.equal(old.kind, "admin-danbooru-url-batch-import");
  assert.equal(old.meta.total, 2);
  assert.equal(BATCH_RECOVERY_MS, 2 * 60 * 60 * 1000);
  assert.deepEqual(restoreBatchRecord({ jobId: "invalid" }), {});
});
