import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(new URL("../src/lib/preferencesSync.ts", import.meta.url), "utf8");

function loadSync({
  storage = {},
  storageFails = false,
  csrf = "session-csrf",
  updateMyPreferences = () => Promise.resolve(),
} = {}) {
  const calls = [];
  const scheduled = [];
  const cancelled = new Set();
  const exports = {};
  const context = {
    exports,
    localStorage: {
      getItem(key) {
        if (storageFails) throw new Error("storage unavailable");
        return storage[key] ?? null;
      },
    },
    window: {},
    setTimeout(callback) {
      const id = scheduled.length + 1;
      scheduled.push({ id, callback });
      return id;
    },
    clearTimeout(id) {
      cancelled.add(id);
    },
    require(id) {
      if (id === "@/lib/api") {
        return {
          api: {
            updateMyPreferences(value) {
              calls.push(value);
              return updateMyPreferences(value);
            },
          },
        };
      }
      if (id === "@/lib/themePreference") {
        return { DEFAULT_THEME: "light", THEME_STORAGE_KEY: "auto-gallery-theme" };
      }
      if (id === "@/lib/authFlow") return { csrfToken: () => csrf };
      throw new Error(`Unexpected import: ${id}`);
    },
  };
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  vm.runInNewContext(compiled, context);

  return {
    calls,
    scheduled,
    cancelled,
    pushPreferences: exports.pushPreferences,
    async runScheduled() {
      const results = scheduled
        .filter(({ id }) => !cancelled.has(id))
        .map(({ callback }) => callback());
      await Promise.all(results.filter((result) => result && typeof result.then === "function"));
    },
  };
}

test("syncs the complete merged local preference object", async () => {
  const storage = {
    "auto-gallery-theme": "dark",
    "auto-gallery-lang": "en",
    "auto-gallery-appearance-v1": JSON.stringify({ worksViewMode: "masonry" }),
    "auto-gallery-slideshow-v1": JSON.stringify({ interval: 8 }),
  };
  const sync = loadSync({ storage });
  sync.pushPreferences();
  await sync.runScheduled();

  assert.deepEqual(JSON.parse(JSON.stringify(sync.calls)), [
    {
      theme: "dark",
      lang: "en",
      appearance: { worksViewMode: "masonry" },
      slideshow: { interval: 8 },
    },
  ]);
});

test("debounces preference writes and runs only the latest timer", async () => {
  const sync = loadSync({ storage: { "auto-gallery-lang": "en" } });
  sync.pushPreferences();
  sync.pushPreferences();

  assert.equal(sync.scheduled.length, 2);
  assert.deepEqual([...sync.cancelled], [1]);
  await sync.runScheduled();
  assert.equal(sync.calls.length, 1);
});

test("does not replace server preferences when local storage contains malformed data", async () => {
  const sync = loadSync({ storage: { "auto-gallery-appearance-v1": "{broken" } });
  sync.pushPreferences();
  await sync.runScheduled();
  assert.deepEqual(sync.calls, []);
});

test("does not sync when local storage cannot be read", async () => {
  const sync = loadSync({ storageFails: true });
  sync.pushPreferences();
  await sync.runScheduled();
  assert.deepEqual(sync.calls, []);
});

test("does not schedule a write while unauthenticated", async () => {
  const sync = loadSync({ csrf: "" });
  sync.pushPreferences();
  await sync.runScheduled();
  assert.equal(sync.scheduled.length, 0);
  assert.deepEqual(sync.calls, []);
});

test("network failure leaves local preferences available for the next attempt", async () => {
  const storage = { "auto-gallery-lang": "en" };
  const sync = loadSync({
    storage,
    updateMyPreferences: () => Promise.reject(new Error("offline")),
  });
  sync.pushPreferences();
  await sync.runScheduled();

  assert.deepEqual(storage, { "auto-gallery-lang": "en" });
  assert.equal(sync.calls.length, 1);
});


test("coalesces different preference changes using their latest local values", async () => {
  const storage = { "auto-gallery-lang": "zh", "auto-gallery-theme": "light" };
  const sync = loadSync({ storage });
  sync.pushPreferences();
  storage["auto-gallery-lang"] = "en";
  storage["auto-gallery-theme"] = "dark";
  storage["auto-gallery-appearance-v1"] = JSON.stringify({ worksViewMode: "list" });
  sync.pushPreferences();
  await sync.runScheduled();
  assert.deepEqual(JSON.parse(JSON.stringify(sync.calls)), [
    { lang: "en", theme: "dark", appearance: { worksViewMode: "list" }, slideshow: {} },
  ]);
});
