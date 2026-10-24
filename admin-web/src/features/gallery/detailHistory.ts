"use client";

const WORK_DETAIL_ORIGIN_KEY = "__autoGalleryWorkDetailOrigin";
const DETAIL_ORIGIN_KEY = "__autoGalleryDetailOrigin";

function updateHistoryOrigin(value: "list" | undefined, scope = "work") {
  if (typeof window === "undefined") return;
  const state = { ...(window.history.state || {}) } as Record<string, unknown>;
  const key = scope === "work" ? WORK_DETAIL_ORIGIN_KEY : `${DETAIL_ORIGIN_KEY}:${scope}`;
  if (value) state[key] = value;
  else delete state[key];
  window.history.replaceState(state, "", window.location.href);
}

export function markDetailListEntry(scope = "work") {
  if (typeof window === "undefined") return;
  updateHistoryOrigin("list", scope);
}

export function hasDetailListOrigin(scope = "work") {
  const key = scope === "work" ? WORK_DETAIL_ORIGIN_KEY : `${DETAIL_ORIGIN_KEY}:${scope}`;
  return typeof window !== "undefined"
    && (window.history.state as Record<string, unknown> | null)?.[key] === "list";
}

export function clearDetailListOrigin(scope = "work") {
  updateHistoryOrigin(undefined, scope);
}

export function markWorkDetailListEntry() { markDetailListEntry("work"); }
export function hasWorkDetailListOrigin() { return hasDetailListOrigin("work"); }
export function clearWorkDetailListOrigin() { clearDetailListOrigin("work"); }
