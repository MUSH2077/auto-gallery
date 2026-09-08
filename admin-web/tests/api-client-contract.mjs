import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, request } from "../src/lib/api/client.ts";

function installBrowserToken(token = "jwt-fixture") {
  globalThis.window = { location: { pathname: "/admin/settings/backup", replace() {} } };
  globalThis.document = { cookie: "" };
  globalThis.localStorage = {
    getItem(key) { return key === "ag_token" ? token : null; },
    removeItem() {},
  };
  globalThis.sessionStorage = { removeItem() {} };
}

test("request merges JWT authorization with caller headers", async () => {
  installBrowserToken();
  let received;
  globalThis.fetch = async (_url, init) => {
    received = new Headers(init.headers);
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await request("/api/v1/admin/backup/restore/uploads/upload-1", {
    headers: { "X-Restore-Token": "restore-fixture" },
  });

  assert.equal(received.get("authorization"), "Bearer jwt-fixture");
  assert.equal(received.get("x-restore-token"), "restore-fixture");
  assert.equal(received.get("content-type"), "application/json");
});

test("request keeps structured business rejection details", async () => {
  installBrowserToken();
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: {
      code: "batch_active",
      message: "A subscription sync batch is already active",
      task_id: "existing-task",
      mode: "manual_all_enabled",
    },
  }), {
    status: 409,
    statusText: "Conflict",
    headers: { "Content-Type": "application/json" },
  });

  await assert.rejects(
    request("/api/v1/admin/scheduler/sync-now", { method: "POST" }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "business");
      assert.equal(error.status, 409);
      assert.equal(error.code, "batch_active");
      assert.equal(error.detail.task_id, "existing-task");
      return true;
    },
  );
});

test("request distinguishes network failures and preserves 204 responses", async () => {
  installBrowserToken();
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  await assert.rejects(request("/api/v1/system/health"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.kind, "network");
    assert.equal(error.status, 0);
    return true;
  });

  globalThis.fetch = async () => new Response(null, { status: 204 });
  assert.equal(await request("/api/v1/no-content", { method: "DELETE" }), undefined);
});
