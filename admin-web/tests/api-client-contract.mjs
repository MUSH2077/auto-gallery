import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, apiErrorFromData, assertBackupArchiveResponse, request, requestBlob, uploadWithProgress } from "../src/lib/api/client.ts";

function installBrowserToken() {
  const removed = [];
  const redirects = [];
  globalThis.window = { location: { pathname: "/admin/settings/backup", search: "", hash: "", replace(path) { redirects.push(path); } } };
  globalThis.document = { cookie: "ag_csrf=csrf-fixture" };
  globalThis.localStorage = {
    getItem() { return null; },
    removeItem(key) { removed.push(`local:${key}`); },
  };
  globalThis.sessionStorage = { removeItem(key) { removed.push(`session:${key}`); } };
  return { removed, redirects };
}

test("request uses session cookie and keeps caller headers", async () => {
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

  assert.equal(received.get("authorization"), null);
  assert.equal(received.get("x-restore-token"), "restore-fixture");
  assert.equal(received.get("content-type"), "application/json");
});

test("writes attach the session CSRF value", async () => {
  installBrowserToken();
  let received;
  globalThis.fetch = async (_url, init) => {
    received = new Headers(init.headers);
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  };
  await request("/api/v1/tasks/task-1/retry", { method: "POST" });
  assert.equal(received.get("x-csrf-token"), "csrf-fixture");
  assert.equal(received.get("authorization"), null);
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
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(browser.removed.includes("session:danbooru_batch_job"));
  assert.deepEqual(browser.redirects, ["/admin/login?redirect=%2Fadmin%2Fsettings%2Fbackup"]);
  assert.equal(received.get("authorization"), null);
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

test("shared error normalization preserves structured business fields", () => {
  const error = apiErrorFromData(409, "Conflict", { detail: { code: "batch_active", message: "Already running", task_id: "task-1" } });
  assert.ok(error instanceof ApiError);
  assert.equal(error.kind, "business");
  assert.equal(error.status, 409);
  assert.equal(error.message, "Already running");
  assert.equal(error.code, "batch_active");
  assert.deepEqual(error.detail, { code: "batch_active", message: "Already running", task_id: "task-1" });
  assert.deepEqual(error.body, { detail: { code: "batch_active", message: "Already running", task_id: "task-1" } });
});

test("shared error normalization keeps validation and plain body errors readable", () => {
  const validation = apiErrorFromData(422, "Unprocessable Entity", { detail: [{ msg: "name is required" }, { msg: "name is too short" }] });
  assert.equal(validation.kind, "business");
  assert.equal(validation.message, "name is required; name is too short");
  assert.deepEqual(validation.detail, [{ msg: "name is required" }, { msg: "name is too short" }]);

  const plain = apiErrorFromData(502, "Bad Gateway", "gateway exploded");
  assert.equal(plain.kind, "http");
  assert.equal(plain.message, "gateway exploded");
  assert.equal(plain.detail, "gateway exploded");
  assert.equal(plain.body, "gateway exploded");
});

test("shared error normalization categorizes timeout and empty bodies consistently", () => {
  const timeout = apiErrorFromData(0, "", undefined, "Upload timed out", "network");
  assert.equal(timeout.kind, "network");
  assert.equal(timeout.status, 0);
  assert.equal(timeout.message, "Upload timed out");
  const empty = apiErrorFromData(503, "Service Unavailable", "");
  assert.equal(empty.kind, "http");
  assert.equal(empty.message, "503 Service Unavailable");
});

test("XHR upload matches fetch error fields and preserves progress and CSRF", async () => {
  installBrowserToken();
  const cases = [
    [409, "Conflict", { detail: { code: "batch_active", message: "Already running" } }, "business", "Already running"],
    [422, "Unprocessable Entity", { detail: [{ msg: "name is required" }, { msg: "name is too short" }] }, "business", "name is required; name is too short"],
    [400, "Bad Request", { detail: "Invalid upload" }, "http", "Invalid upload"],
    [502, "Bad Gateway", "gateway exploded", "http", "gateway exploded"],
    [503, "Service Unavailable", "", "http", "503 Service Unavailable"],
  ];
  for (const [status, statusText, body, kind, message] of cases) {
    let xhrInstance;
    class FakeXHR {
      upload = {};
      constructor() { xhrInstance = this; }
      responseText = typeof body === "string" ? body : JSON.stringify(body);
      open() {}
      setRequestHeader(name, value) { this.headers = { ...this.headers, [name]: value }; }
      send() { this.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 2 }); queueMicrotask(() => this.onload()); }
      status = status;
      statusText = statusText;
    }
    globalThis.XMLHttpRequest = FakeXHR;
    const responseText = typeof body === "string" ? body : JSON.stringify(body);
    globalThis.fetch = async () => new Response(responseText, { status, statusText });
    let fetchError;
    await assert.rejects(request("/api/v1/fixture"), (error) => { fetchError = error; return true; });
    const progress = [];
    await assert.rejects(uploadWithProgress(new FormData(), "/api/v1/upload", (pct) => progress.push(pct)), (error) => {
      for (const field of ["status", "message", "detail", "code", "kind", "body"]) {
        assert.deepEqual(error[field], fetchError[field], field);
      }
      assert.equal(error.kind, kind);
      assert.equal(error.message, message);
      assert.deepEqual(error.body, typeof body === "string" && body ? body : body && body.detail ? body : undefined);
      return true;
    });
    assert.deepEqual(progress, [50]);
    assert.equal(xhrInstance.headers["X-CSRF-Token"], "csrf-fixture");
  }
});

test("XHR upload network, timeout, success, and stale 401 behavior stay consistent", async () => {
  const browser = installBrowserToken();
  let successXHR;
  class SuccessXHR {
    upload = {};
    constructor() { successXHR = this; }
    status = 201;
    responseText = JSON.stringify({ uploaded: 2 });
    open() {}
    setRequestHeader(name, value) { this.headers = { [name]: value }; }
    send() { this.upload.onprogress?.({ lengthComputable: true, loaded: 2, total: 2 }); queueMicrotask(() => this.onload()); }
  }
  globalThis.XMLHttpRequest = SuccessXHR;
  const progress = [];
  assert.deepEqual(await uploadWithProgress(new FormData(), "/api/v1/upload", (pct) => progress.push(pct)), { uploaded: 2 });
  assert.deepEqual(progress, [100]);
  assert.equal(successXHR.headers["X-CSRF-Token"], "csrf-fixture");

  class NetworkXHR { upload = {}; open() {} setRequestHeader() {} send() { queueMicrotask(() => this.onerror()); } }
  globalThis.XMLHttpRequest = NetworkXHR;
  await assert.rejects(uploadWithProgress(new FormData(), "/api/v1/upload"), (error) => error.kind === "network" && error.status === 0);
  class TimeoutXHR { upload = {}; open() {} setRequestHeader() {} send() { queueMicrotask(() => this.ontimeout()); } }
  globalThis.XMLHttpRequest = TimeoutXHR;
  await assert.rejects(uploadWithProgress(new FormData(), "/api/v1/upload"), (error) => error.kind === "network" && error.message === "Upload timed out");

  class UnauthorizedXHR {
    upload = {};
    status = 401;
    statusText = "Unauthorized";
    responseText = JSON.stringify({ detail: { code: "token_expired", message: "Expired" } });
    open() {}
    setRequestHeader() {}
    send() { document.cookie = "ag_csrf=new-session"; queueMicrotask(() => this.onload()); }
  }
  globalThis.XMLHttpRequest = UnauthorizedXHR;
  await assert.rejects(uploadWithProgress(new FormData(), "/api/v1/upload"));
  assert.deepEqual(browser.redirects, []);
});

test("requestBlob shares session/header/error handling and preserves response metadata", async () => {
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
  assert.equal(received.get("authorization"), null);
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
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(browser.redirects, ["/admin/login?redirect=%2Fadmin%2Fsettings%2Fbackup"]);

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


test("expired-session redirects retain the current query and fragment", async () => {
  const browser = installBrowserToken();
  window.location.pathname = "/admin/jobs";
  window.location.search = "?scope=attention&task=one";
  window.location.hash = "#details";
  globalThis.fetch = async () => new Response(JSON.stringify({detail:"Expired"}), { status:401 });
  await assert.rejects(request("/api/v1/tasks/one"));
  const target = new URL(browser.redirects[0], "http://localhost");
  assert.equal(target.pathname, "/admin/login");
  assert.equal(target.searchParams.get("redirect"), "/admin/jobs?scope=attention&task=one#details");
});
