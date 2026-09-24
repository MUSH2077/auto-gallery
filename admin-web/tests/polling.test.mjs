import assert from "node:assert/strict";
import test from "node:test";

import {
  ADMIN_OPERATION_CONFIRM_MS,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  pollInterval,
} from "../src/lib/polling.ts";

test("adaptive polling uses ten seconds while active and sixty seconds while idle", () => {
  assert.equal(ADMIN_OPERATION_CONFIRM_MS, 1_000);
  assert.equal(POLL_ACTIVE_MS, 10_000);
  assert.equal(POLL_IDLE_MS, 60_000);
  assert.equal(pollInterval(true, "visible"), 10_000);
  assert.equal(pollInterval(false, "visible"), 60_000);
});

test("adaptive polling stops while the page is hidden", () => {
  assert.equal(pollInterval(true, "hidden"), false);
  assert.equal(pollInterval(false, "hidden"), false);
});
