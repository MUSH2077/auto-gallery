// Shared polling cadence so background refetch load stays predictable.
// Active = something is in flight (poll fast); idle = poll slowly.
export const POLL_ACTIVE_MS = 8000;
export const POLL_IDLE_MS = 30000;

export function pollInterval(active: boolean): number {
  return active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}

const NONTERMINAL = new Set([
  "enqueued", "running", "paused", "recovering",
  "downloading", "downloaded", "importing",
]);

export function hasActiveTask(items?: { status?: string | null }[] | null): boolean {
  return !!items?.some((t) => t.status != null && NONTERMINAL.has(t.status));
}
