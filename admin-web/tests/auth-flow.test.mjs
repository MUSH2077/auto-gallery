import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/authFlow.ts", import.meta.url), "utf8");
function loadAuthFlow({ storageThrows = false, fetchImpl } = {}) {
  const calls = [];
  let cookie = "ag_token=legacy; ag_csrf=session-csrf";
  const context = {
    exports: {},
    window: {},
    document: { get cookie() { return cookie; }, set cookie(value) { if (value.startsWith("ag_token=")) cookie = cookie.split(";").filter((part) => !part.trim().startsWith("ag_token=")).join(";").trim(); else cookie = value; } },
    localStorage: {
      removeItem(key) {
        calls.push({ type: "removeItem", key });
        if (storageThrows) throw new Error("Storage disabled");
      },
    },
    fetch: async (...args) => {
      calls.push({ type: "fetch", args });
      return fetchImpl?.(...args) || { ok: true, status: 200, json: async () => ({ id: 1, username: "review", display_name: "Review", must_change_password: false }) };
    },
  };
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2017 },
  }).outputText;
  vm.runInNewContext(compiled, context);
  return { authFlow: context.exports, calls, document: context.document };
}

test("login continues when legacy token storage is unavailable", async () => {
  const { authFlow, calls } = loadAuthFlow({ storageThrows: true });
  const user = await authFlow.loginAndLoadUser("review", "fixture");
  assert.equal(user.username, "review");
  assert.deepEqual(calls.filter((call) => call.type === "fetch").map((call) => call.args[0]), [
    "/api/v1/auth/browser/login",
    "/api/v1/auth/me",
  ]);
});

test("logout continues when legacy token storage is unavailable", async () => {
  const { authFlow, calls } = loadAuthFlow({ storageThrows: true });
  await authFlow.logoutBrowser();
  assert.equal(calls.filter((call) => call.type === "fetch").length, 1);
  assert.equal(calls.find((call) => call.type === "fetch").args[0], "/api/v1/auth/browser/logout");
});

test("normal login still clears the legacy token and preserves authentication errors", async () => {
  const { authFlow, calls, document } = loadAuthFlow({
    fetchImpl: async (path) => path.endsWith("/login")
      ? { ok: false, status: 401, json: async () => ({ detail: "Invalid credentials" }) }
      : { ok: true, status: 200, json: async () => ({}) },
  });
  await assert.rejects(
    authFlow.loginAndLoadUser("review", "wrong"),
    (error) => error.status === 401 && error.message === "Invalid credentials",
  );
  assert.equal(document.cookie, "ag_csrf=session-csrf");
  assert.deepEqual(calls.filter((call) => call.type === "removeItem"), [{ type: "removeItem", key: "ag_token" }]);
  assert.equal(calls.filter((call) => call.type === "fetch").length, 1);
});

test("loadUser preserves the existing 503 presentation status", async () => {
  const { authFlow } = loadAuthFlow({
    fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({}) }),
  });
  await assert.rejects(
    authFlow.loadUser(),
    (error) => error.status === 503 && error.message === "Session service temporarily unavailable",
  );
});

test("clearLegacyToken removes the old cookie before a normal session lookup", async () => {
  const { authFlow, calls, document } = loadAuthFlow();
  authFlow.clearLegacyToken();
  assert.equal(document.cookie, "ag_csrf=session-csrf");
  await authFlow.loadUser();
  assert.equal(calls.filter((call) => call.type === "fetch").length, 1);
});
