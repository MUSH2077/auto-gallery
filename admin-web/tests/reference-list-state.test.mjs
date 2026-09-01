import assert from "node:assert/strict";
import test from "node:test";

let state = {};
try {
  state = await import("../src/lib/reference-list-state.ts");
} catch {
  // The first TDD run intentionally lands here before the module exists.
}

test("reference sorting defaults to name ascending and toggles active direction", () => {
  assert.deepEqual(state.referenceSortFromTokens?.([]), {
    field: "name",
    direction: "asc",
  });
  assert.equal(
    state.nextReferenceSortValue?.(
      { field: "name", direction: "asc" },
      "name",
    ),
    "name-desc",
  );
  assert.equal(
    state.nextReferenceSortValue?.(
      { field: "created", direction: "desc" },
      "created",
    ),
    "created-asc",
  );
});

test("first-time date sorts descend while first-time name sorts ascend", () => {
  const current = { field: "updated", direction: "desc" };
  assert.equal(state.nextReferenceSortValue?.(current, "name"), "name-asc");
  assert.equal(
    state.nextReferenceSortValue?.(current, "created"),
    "created-desc",
  );
});

test("parsed server tokens control name-anchor availability", () => {
  const nameSort = [
    { kind: "qualifier", key: "is", value: "inactive", negated: false },
    { kind: "qualifier", key: "sort", value: "name-desc", negated: false },
  ];
  assert.equal(state.referenceNameAnchorsAvailable?.(nameSort), true);
  assert.equal(
    state.referenceNameAnchorsAvailable?.([
      ...nameSort,
      { kind: "text", value: "pixiv" },
    ]),
    false,
  );
  assert.equal(
    state.referenceNameAnchorsAvailable?.([
      { kind: "qualifier", key: "sort", value: "updated-desc", negated: false },
    ]),
    false,
  );
});

test("legacy page links become 25-row initial positions inside 50-row batches", () => {
  assert.equal(state.legacyPageInitialIndex?.("0"), 0);
  assert.equal(state.legacyPageInitialIndex?.("3"), 75);
  assert.equal(state.legacyPageInitialIndex?.("-4"), 0);
  assert.equal(state.referenceBatchOffset?.(75), 50);
  assert.equal(state.REFERENCE_BATCH_SIZE, 50);
});

test("session payloads are versioned, bounded, and reject another query", () => {
  assert.equal(
    state.referenceSessionStorageKey?.(
      17,
      "/admin/creators",
      "creators:is:inactive sort:name-asc",
    ),
    "reference-list:v1:17:%2Fadmin%2Fcreators:creators%3Ais%3Ainactive%20sort%3Aname-asc",
  );
  const payload = state.createReferenceListSession?.({
    queryFingerprint: "creators:is:inactive sort:name-asc",
    scrollY: 480,
    loadedOffsets: [100, 0, 50, 50, -50, 10000],
    selectedIds: ["creator-2", "creator-1", "creator-2"],
  });
  assert.deepEqual(payload, {
    version: 1,
    queryFingerprint: "creators:is:inactive sort:name-asc",
    scrollY: 480,
    loadedOffsets: [0, 50, 100, 10000],
    selectedIds: ["creator-1", "creator-2"],
  });
  assert.deepEqual(
    state.readReferenceListSession?.(
      JSON.stringify(payload),
      "creators:is:inactive sort:name-asc",
    ),
    payload,
  );
  assert.equal(
    state.readReferenceListSession?.(
      JSON.stringify(payload),
      "creators:is:active sort:name-asc",
    ),
    null,
  );
  assert.equal(state.readReferenceListSession?.("not-json", "x"), null);
});
