import test from "node:test";
import assert from "node:assert/strict";

const monitor = await import("../src/lib/operationStatus.ts");
const controller = await import("../src/lib/operationStatus.ts");

const running = { jobId: "a", kind: "admin-clear", title: "Clear", startedAt: 1, status: "running", progress: null, result: null };

test("terminal operation states stop polling", () => {
  for (const status of ["complete", "completed", "failed", "stale", "cancelled", "unknown", "future-state"]) {
    assert.equal(monitor.normalizeOperationStatus(status), status === "future-state" ? "unknown" : status);
  }
});

test("active and missing operation responses remain observable", () => {
  assert.equal(monitor.isOperationActive("running"), true);
  assert.equal(monitor.isOperationTerminal(undefined), true);
});

test("controller stops and refreshes exactly once per terminal response", () => {
  const completed = controller.transitionOperation(running, { status: "complete", result: { deleted: 2 } });
  assert.equal(completed.terminal, true);
  assert.equal(completed.shouldRefresh, true);
  assert.equal(completed.state.status, "completed");
  const repeated = controller.transitionOperation(completed.state, { status: "complete", result: { deleted: 2 } });
  assert.equal(repeated.state, completed.state);
  assert.equal(repeated.shouldRefresh, false);
  const cancelled = controller.transitionOperation(running, { status: "cancelled", error: "cancelled" });
  assert.equal(cancelled.terminal, true);
  assert.equal(cancelled.shouldRefresh, false);
  const replaced = controller.transitionOperation({ ...running, jobId: "new" }, { status: "complete", result: { deleted: 1 } });
  assert.equal(replaced.state.jobId, "new");
});
