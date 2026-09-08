import assert from "node:assert/strict";
import test from "node:test";

import { writeClipboardText } from "../src/lib/clipboard.ts";
import { secureRandomUuid } from "../src/lib/random.ts";

test("secureRandomUuid uses getRandomValues on an HTTP origin and emits UUID v4", () => {
  const original = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      getRandomValues(bytes) {
        bytes.fill(0xab);
        return bytes;
      },
    },
  });
  try {
    const value = secureRandomUuid();
    assert.match(value, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(value, "abababab-abab-4bab-abab-abababababab");
  } finally {
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: original });
  }
});

test("writeClipboardText resolves only after the exact text is written", async () => {
  let release;
  let received;
  const pending = writeClipboardText("exact diagnostics", {
    writeText(text) {
      received = text;
      return new Promise((resolve) => { release = resolve; });
    },
  });
  let settled = false;
  pending.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(received, "exact diagnostics");
  assert.equal(settled, false);
  release();
  assert.equal(await pending, "exact diagnostics");
});

test("writeClipboardText reports rejected and unavailable clipboard APIs", async () => {
  await assert.rejects(
    writeClipboardText("secret", { writeText: async () => { throw new Error("denied"); } }),
    /denied/,
  );
  await assert.rejects(writeClipboardText("secret", undefined), /Clipboard unavailable/);
});
