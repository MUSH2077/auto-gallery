/** UI action eligibility mirrors the backend task state machine. */
export const PAUSABLE_DOWNLOAD_STATUSES = [
  "enqueued",
  "downloading",
  "downloaded",
  "importing",
] as const;

export function canPauseDownload(status: string): boolean {
  return PAUSABLE_DOWNLOAD_STATUSES.includes(
    status as (typeof PAUSABLE_DOWNLOAD_STATUSES)[number],
  );
}
