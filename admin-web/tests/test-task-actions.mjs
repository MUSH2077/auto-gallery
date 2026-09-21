import assert from "node:assert/strict";

import test from "node:test";

import { actionReason, hasTaskAction, partitionTaskAction } from "../src/lib/task-actions.ts";

const rows = [
  { id: "eligible", available_actions: ["pause", "cancel"], disabled_reasons: { retry: "invalid_task_state" } },
  { id: "denied", available_actions: [], disabled_reasons: { pause: "permission_denied" } },
  { id: "repeat", available_actions: ["repeat_sync"], disabled_reasons: { retry: "completed_sync_requires_repeat" } },
];

test("capability checks use returned actions rather than status", () => {
  assert.equal(hasTaskAction(rows[0], "pause"), true);
  assert.equal(hasTaskAction({ ...rows[0], status: "failed" }, "pause"), true);
  assert.equal(hasTaskAction({ status: "running", available_actions: [] }, "pause"), false);
  assert.equal(hasTaskAction(rows[2], "retry"), false);
  assert.equal(hasTaskAction(rows[2], "repeat_sync"), true);
});

test("partition preserves selected order and explains ineligible rows", () => {
  assert.deepEqual(partitionTaskAction(rows, new Set(["denied", "eligible"]), "pause"), {
    eligible: [rows[0]],
    ineligible: [{ row: rows[1], reason: "permission_denied" }],
  });
  assert.deepEqual(partitionTaskAction(rows, new Set(), "pause"), { eligible: [], ineligible: [] });
});

test("unknown or absent reasons remain explicit", () => {
  assert.equal(actionReason(rows[0], "retry"), "invalid_task_state");
  assert.equal(actionReason({ available_actions: [], disabled_reasons: { retry: "future_reason" } }, "retry"), "future_reason");
  assert.equal(actionReason({ available_actions: [] }, "retry"), "action_unavailable");
  assert.equal(actionReason(rows[0], "pause"), null);
});
