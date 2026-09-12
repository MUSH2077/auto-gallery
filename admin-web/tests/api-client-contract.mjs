import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, assertBackupArchiveResponse, request, requestBlob } from "../src/lib/api/client.ts";

function installBrowserToken(token = "jwt-fixture") {
  const removed = [];
  const redirects = [];
  globalThis.window = { location: { pathname: "/admin/settings/backup", replace(path) { redirects.push(path); } } };
  globalThis.document = { cookie: "" };
  globalThis.localStorage = {
    getItem(key) { return key === "ag_token" ? token : null; },
    removeItem(key) { removed.push(`local:${key}`); },
  };
  globalThis.sessionStorage = { removeItem(key) { removed.push(`session:${key}`); } };
  return { removed, redirects };
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

test("protected structured 401 clears auth and retains the rich rejection", async () => {
  const browser = installBrowserToken();
  const body = {
    detail: {
      code: "token_expired",
      message: "The access token has expired",
      reauthenticate: true,
    },
  };
  let received;
  globalThis.fetch = async (_url, init) => {
    received = new Headers(init.headers);
    return new Response(JSON.stringify(body), {
      status: 401,
      statusText: "Unauthorized",
      headers: { "Content-Type": "application/json" },
    });
  };

  await assert.rejects(request("/api/v1/tasks/task-1"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.kind, "business");
    assert.equal(error.status, 401);
    assert.equal(error.message, "The access token has expired");
    assert.equal(error.code, "token_expired");
    assert.deepEqual(error.detail, body.detail);
    assert.deepEqual(error.body, body);
    return true;
  });
  assert.deepEqual(browser.removed, ["local:ag_token", "session:danbooru_batch_job"]);
  assert.deepEqual(browser.redirects, ["/admin/login"]);
  assert.equal(received.get("authorization"), "Bearer jwt-fixture");
  assert.match(globalThis.document.cookie, /max-age=0/);
});

test("plain non-JSON rejection remains an HTTP error", async () => {
  installBrowserToken();
  globalThis.fetch = async () => new Response("gateway exploded", {
    status: 502,
    statusText: "Bad Gateway",
    headers: { "Content-Type": "text/plain" },
  });

  await assert.rejects(request("/api/v1/system/health"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.kind, "http");
    assert.equal(error.status, 502);
    assert.equal(error.message, "gateway exploded");
    assert.equal(error.detail, "gateway exploded");
    assert.equal(error.body, "gateway exploded");
    return true;
  });
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

test("requestBlob shares auth/header/error handling and preserves response metadata", async () => {
  installBrowserToken();
  let received;
  globalThis.fetch = async (_url, init) => {
    received = new Headers(init.headers);
    return new Response(new Uint8Array([1, 2, 3]), {
      status: 200,
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": "attachment; filename*=UTF-8''backup%20safe.zip",
      },
    });
  };
  const result = await requestBlob("/api/v1/admin/backup/download?filename=backup.zip", {
    headers: { "X-Trace": "binary" },
  });
  assert.equal(received.get("authorization"), "Bearer jwt-fixture");
  assert.equal(received.get("x-trace"), "binary");
  assert.equal(received.has("content-type"), false);
  assert.equal(result.contentDisposition, "attachment; filename*=UTF-8''backup%20safe.zip");
  assert.equal(result.contentType, "application/zip");
  assert.deepEqual([...new Uint8Array(await result.blob.arrayBuffer())], [1, 2, 3]);

  const browser = installBrowserToken();
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: "token_expired", message: "Expired" } }), {
    status: 401,
    headers: { "Content-Type": "application/json" },
  });
  await assert.rejects(requestBlob("/api/v1/admin/backup/download"), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, "token_expired");
    assert.equal(error.message, "Expired");
    return true;
  });
  assert.deepEqual(browser.redirects, ["/admin/login"]);

  installBrowserToken();
  globalThis.fetch = async () => new Response("proxy unavailable", { status: 503 });
  await assert.rejects(requestBlob("/api/v1/admin/backup/download"), (error) => {
    assert.equal(error.kind, "http");
    assert.equal(error.detail, "proxy unavailable");
    return true;
  });
});

test("backup download rejects an HTTP 200 JSON diagnostic instead of returning archive bytes", async () => {
  const diagnostic = new Blob([JSON.stringify({ status: "error", message: "No backups available" })], {
    type: "application/json",
  });
  await assert.rejects(
    assertBackupArchiveResponse({ blob: diagnostic, contentType: "application/json", contentDisposition: null }),
    /No backups available/,
  );
  const archive = { blob: new Blob([new Uint8Array([1, 2, 3])]), contentType: "application/gzip", contentDisposition: null };
  assert.equal(await assertBackupArchiveResponse(archive), archive);
});

test("backup archive validation accepts only the backend gzip media type", async () => {
  const bytes = new Blob([new Uint8Array([0x1f, 0x8b, 0x08])]);
  for (const contentType of [null, "", "text/plain", "text/html; charset=utf-8", "application/octet-stream", "application/x-gzip"]) {
    await assert.rejects(
      assertBackupArchiveResponse({ blob: bytes, contentType, contentDisposition: "attachment; filename=unsafe.tar.gz" }),
      /unexpected backup archive content type/i,
      `must reject ${contentType ?? "missing Content-Type"}`,
    );
  }
  const archive = { blob: bytes, contentType: "Application/GZip; charset=binary", contentDisposition: "attachment; filename=safe.tar.gz" };
  assert.equal(await assertBackupArchiveResponse(archive), archive);
});
