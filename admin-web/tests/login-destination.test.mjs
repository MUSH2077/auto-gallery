import assert from "node:assert/strict";
import test from "node:test";
import { loginDestination } from "../src/lib/loginDestination.ts";

test("restores protected page, query and fragment", () => {
  assert.equal(loginDestination("/admin/jobs?scope=attention&task=one#details"), "/admin/jobs?scope=attention&task=one#details");
  assert.equal(loginDestination("/admin/works?q=source%3Apixiv&p=2"), "/admin/works?q=source%3Apixiv&p=2");
  assert.equal(loginDestination("/?q=test"), "/?q=test");
});
test("rejects external destinations, login loops and non-application paths", () => {
  for (const value of [null, "", "https://example.com", "//example.com", "/\\example.com", "/%2f%2fexample.com", "/admin/%6cogin", "/admin/login?redirect=/admin", "/api/v1/auth/me", "/admin/%00", "/admin/%zz", "javascript:alert(1)"]) {
    assert.equal(loginDestination(value), "/admin", String(value));
  }
});
test("required password change takes priority over return target", () => {
  assert.equal(loginDestination("/admin/jobs?task=one", true), "/admin/profile");
});
