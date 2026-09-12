import assert from "node:assert/strict";
import test from "node:test";

import {
  actionErrorReason,
  createRepeatSyncIntent,
  parseOptionalFiniteNumber,
  metadataCleanupPresentation,
  reconcileTaskBulkResult,
  repeatSyncStorageKey,
  readRepeatSyncIntent,
  listRepeatSyncIntents,
  storeRepeatSyncIntent,
  clearRepeatSyncIntent,
  repeatSyncConflict,
  validateRepeatSyncAcceptance,
} from "../src/lib/task-actions.ts";

test("bulk reconciliation removes only confirmed successes and retains every refusal reason", () => {
  const submission = {
    selectedIds: ["eligible-ok", "eligible-failed", "local-refused"],
    eligibleIds: ["eligible-ok", "eligible-failed"],
    localRefusals: [{ id: "local-refused", reason: "permission_denied" }],
  };
  const result = reconcileTaskBulkResult(submission, {
    action: "retry",
    requested: 2,
    succeeded: 1,
    failed: 1,
    errors: [{ id: "eligible-failed", error: { reason: "stale_capability" } }],
  });
  assert.deepEqual(result.confirmedSuccessIds, ["eligible-ok"]);
  assert.deepEqual(result.retained, [
    { id: "eligible-failed", reason: "stale_capability", kind: "server_refusal" },
    { id: "local-refused", reason: "permission_denied", kind: "local_refusal" },
  ]);
});

test("bulk reconciliation keeps all submitted IDs when the response is inconsistent or absent", () => {
  const submission = { selectedIds: ["a", "b"], eligibleIds: ["a", "b"], localRefusals: [] };
  assert.deepEqual(
    reconcileTaskBulkResult(submission, { action: "retry", requested: 2, succeeded: 2, failed: 1, errors: [] }).retained,
    [
      { id: "a", reason: "response_unverified", kind: "uncertain" },
      { id: "b", reason: "response_unverified", kind: "uncertain" },
    ],
  );
  assert.deepEqual(
    reconcileTaskBulkResult(submission, null).retained,
    [
      { id: "a", reason: "outcome_unknown", kind: "uncertain" },
      { id: "b", reason: "outcome_unknown", kind: "uncertain" },
    ],
  );
});

test("bulk reconciliation treats duplicated response identities as uncertain", () => {
  const submission = { selectedIds: ["a", "b", "c"], eligibleIds: ["a", "b", "c"], localRefusals: [] };
  const result = reconcileTaskBulkResult(submission, {
    succeeded: 1,
    failed: 2,
    errors: [{ id: "a", error: "first" }, { id: "a", error: "duplicate" }],
  });
  assert.deepEqual(result.confirmedSuccessIds, []);
  assert.deepEqual(result.retained, [
    { id: "a", reason: "response_unverified", kind: "uncertain" },
    { id: "b", reason: "response_unverified", kind: "uncertain" },
    { id: "c", reason: "response_unverified", kind: "uncertain" },
  ]);
  const duplicateSubmission = { selectedIds: ["a"], eligibleIds: ["a", "a"], localRefusals: [] };
  assert.deepEqual(
    reconcileTaskBulkResult(duplicateSubmission, { succeeded: 2, failed: 0, errors: [] }).confirmedSuccessIds,
    [],
  );
});

test("actor and original download identify one recoverable repeat intent across surfaces", () => {
  assert.equal(repeatSyncStorageKey(7, "download-1"), "auto-gallery-repeat-sync:7:download-1:repeat_sync");
  const intent = createRepeatSyncIntent(7, "download-1", "Creator One", () => "request-1");
  assert.deepEqual(intent, {
    version: 1,
    actorId: 7,
    originalJobId: "download-1",
    requestId: "request-1",
    label: "Creator One",
    action: "repeat_sync",
  });
});

test("repeat intent storage is actor scoped, validates entries, and supports compaction recovery", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key), key: (index) => [...values.keys()][index] ?? null, get length() { return values.size; } };
  const intent = createRepeatSyncIntent(7, "old", "Creator", () => "request-1");
  storeRepeatSyncIntent(intent, storage);
  assert.deepEqual(readRepeatSyncIntent(7, "old", storage), intent);
  assert.deepEqual(readRepeatSyncIntent(8, "old", storage), null);
  assert.deepEqual(listRepeatSyncIntents(7, storage), [intent]);
  values.set(repeatSyncStorageKey(7, "bad"), JSON.stringify({ requestId: "untrusted" }));
  assert.deepEqual(listRepeatSyncIntents(7, storage), [intent]);
  clearRepeatSyncIntent(intent, storage);
  assert.equal(readRepeatSyncIntent(7, "old", storage), null);
});

test("repeat conflicts distinguish existing admission from irrecoverable identity", () => {
  assert.deepEqual(repeatSyncConflict({ detail: { code: "repeat_sync_not_admitted", existing_job_id: "existing", reason: "active" } }), { kind: "existing", existingJobId: "existing", reason: "active" });
  assert.deepEqual(repeatSyncConflict({ detail: { code: "request_identity_conflict", reason: "different original" } }), { kind: "identity", reason: "different original" });
  assert.deepEqual(repeatSyncConflict(new Error("network")), { kind: "other", reason: "network" });
});

test("repeat acceptance validates every identity and exposes the accepted task and job", () => {
  const intent = createRepeatSyncIntent(7, "old", "Old", () => "request-1");
  const accepted = {
    task_id: "task-2", job_id: "job-2", previous_job_id: "old", request_id: "request-1",
    action: "repeat_sync", status: "enqueued",
  };
  assert.deepEqual(validateRepeatSyncAcceptance(intent, accepted), accepted);
  for (const invalid of [
    { ...accepted, previous_job_id: "wrong" },
    { ...accepted, request_id: "wrong" },
    { ...accepted, task_id: "" },
    { ...accepted, job_id: "" },
    { ...accepted, status: "complete" },
    { ...accepted, action: "retry" },
  ]) assert.throws(() => validateRepeatSyncAcceptance(intent, invalid), /repeat_identity_invalid/);
});

test("structured action reasons and finite zero preserve authoritative values", () => {
  assert.equal(actionErrorReason({ detail: { reason: "permission_denied" }, message: "409 Conflict" }), "permission_denied");
  assert.equal(actionErrorReason({ detail: { message: "Already active" } }), "Already active");
  assert.equal(actionErrorReason(new Error("network down")), "network down");
  assert.equal(actionErrorReason({}), "action_failed");
  assert.equal(parseOptionalFiniteNumber("0", undefined), 0);
  assert.equal(parseOptionalFiniteNumber("", undefined), undefined);
  assert.equal(parseOptionalFiniteNumber("bad", 3), 3);
});

test("metadata cleanup presentation is operation and terminal-result gated", () => {
  assert.equal(metadataCleanupPresentation({ operation_type: "other", status: "complete", result_data: { removed: 9 } }), null);
  assert.equal(metadataCleanupPresentation({ operation_type: "admin-cleanup-metadata-jsons", status: "running", result_data: { removed: 2 } }), null);
  assert.deepEqual(metadataCleanupPresentation({
    operation_type: "admin-cleanup-metadata-jsons", status: "complete",
    result_data: { status: "ok", removed: 2, scanned: 4, skipped: 2, failed: 0, reasons: { retained: 2 } },
  }), { kind: "success", removed: 2, scanned: 4, skipped: 2, failed: 0, reasons: { retained: 2 }, errors: [] });
  assert.deepEqual(metadataCleanupPresentation({
    operation_type: "admin-cleanup-metadata-jsons", status: "failed", reason_code: "metadata_cleanup_partial_failure",
    result_data: { status: "partial", removed: 1, scanned: 4, skipped: 1, failed: 2, reasons: { unsafe: 1 }, errors: ["proof changed"] },
  }), { kind: "partial", removed: 1, scanned: 4, skipped: 1, failed: 2, reasons: { unsafe: 1 }, errors: ["proof changed"] });
});
