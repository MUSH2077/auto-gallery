// Shared polling cadence so background refetch load stays predictable.
// Active = something is in flight (poll fast); idle = poll slowly.
export const POLL_ACTIVE_MS = 10000;
export const POLL_IDLE_MS = 60000;
// A newly accepted admin operation gets one quick confirmation after its first
// server-observed running state. Recurring fallback polling still uses the
// shared ten-second cadence above.
export const ADMIN_OPERATION_CONFIRM_MS = 1000;

export function pollInterval(
  active: boolean,
  visibility: DocumentVisibilityState = typeof document === "undefined"
    ? "visible"
    : document.visibilityState,
): number | false {
  if (visibility === "hidden") return false;
  return active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}

const NONTERMINAL = new Set([
  "enqueued", "running", "paused", "recovering",
  "downloading", "downloaded", "importing",
]);

export function hasActiveTask(items?: { status?: string | null }[] | null): boolean {
  return !!items?.some((t) => t.status != null && NONTERMINAL.has(t.status));
}
