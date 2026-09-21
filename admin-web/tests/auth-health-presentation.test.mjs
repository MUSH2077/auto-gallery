import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  authHealthPresentation,
  authStateForSource,
  hasActionableAuthFailure,
} from "../src/lib/auth-health.ts";

test("legacy false without an explicit failure remains unknown", () => {
  const source = { auth_healthy: false };

  assert.equal(authStateForSource(source), "unknown");
  assert.equal(hasActionableAuthFailure(source), false);
  assert.deepEqual(authHealthPresentation(source), {
    state: "unknown",
    tone: "neutral",
    labelKey: "repo.auth_unknown",
    dotClass: "bg-placeholder",
  });
});

test("only an explicit unhealthy state is an actionable auth failure", () => {
  const source = { auth_healthy: true, auth_state: "unhealthy" };

  assert.equal(hasActionableAuthFailure(source), true);
  assert.equal(authHealthPresentation(source).tone, "bad");
  assert.equal(authHealthPresentation(source).labelKey, "repo.auth_issue");
});

test("credential readiness is presented separately from auth attempts", () => {
  const source = {
    auth_healthy: true,
    auth_state: "healthy",
    credential_state: "missing",
  };

  assert.equal(hasActionableAuthFailure(source), false);
  assert.deepEqual(authHealthPresentation(source), {
    state: "credential_missing",
    tone: "bad",
    labelKey: "repo.credential_missing",
    dotClass: "bg-danger",
  });
});

test("scheduler renders credential failures as a named attention reason", () => {
  const format = readFileSync(
    new URL("../src/lib/i18n-format.ts", import.meta.url),
    "utf8",
  );
  const translations = readFileSync(
    new URL("../src/lib/i18n.tsx", import.meta.url),
    "utf8",
  );

  assert.match(format, /SCHEDULER_REASON_KEYS[\s\S]*"credential_missing"/);
  assert.match(translations, /"scheduler\.reason\.credential_missing"/);
});
