/**
 * Job status classification — distinguishes "in-strategy" (auto-recovering)
 * from "needs-manual-action" (requires user intervention) states.
 *
 * Matches the backend's three-layer protection:
 *   1. gallery-dl internal retries (--retries / --timeout)
 *   2. Stall detection (120s no progress → kill)
 *   3. Job-level retry with exponential backoff (3 attempts)
 */

export type JobCategory =
  | "in_strategy"   // system handles automatically
  | "retrying"      // auto-retry in progress
  | "recoverable"   // flagged but can be manually retried
  | "manual"        // requires user intervention
  | "done";          // successfully completed

/** Jobs the system will process without any user action. */
export function isInStrategy(status: string): boolean {
  return ["enqueued", "downloading", "downloaded", "importing"].includes(status);
}

/** Job is being automatically retried by the system. */
export function isAutoRetrying(status: string, retryCount: number, maxRetries: number): boolean {
  return status === "enqueued" && retryCount > 0 && retryCount <= maxRetries;
}

/** Job requires explicit user action (manual retry, resume, or acknowledge). */
export function needsManualAction(status: string): boolean {
  return ["failed", "cancelled", "paused"].includes(status);
}

/** Job is flagged as recoverable — user can retry but system won't auto-retry. */
export function isRecoverable(status: string): boolean {
  return status === "stale";
}

/** Job is complete and needs no further attention. */
export function isDone(status: string): boolean {
  return status === "complete";
}

/** Classify a job into one of five categories. */
export function classifyJob(
  status: string,
  retryCount: number = 0,
  maxRetries: number = 3,
): JobCategory {
  if (isDone(status)) return "done";
  if (isAutoRetrying(status, retryCount, maxRetries)) return "retrying";
  if (isRecoverable(status)) return "recoverable";
  if (needsManualAction(status)) return "manual";
  if (isInStrategy(status)) return "in_strategy";
  return "manual"; // unknown statuses default to manual
}

/** Category label suitable for display badges. */
export function categoryLabel(category: JobCategory): string {
  const labels: Record<JobCategory, string> = {
    in_strategy: "auto",
    retrying: "retrying",
    recoverable: "recoverable",
    manual: "manual",
    done: "done",
  };
  return labels[category];
}

/** CSS border-left color class for a job row based on its category. */
export function categoryBorderClass(category: JobCategory): string {
  switch (category) {
    case "in_strategy":
      return "border-l-[#0969da]";           // blue
    case "retrying":
      return "border-l-[#0969da] border-dashed"; // blue dashed
    case "recoverable":
      return "border-l-[#d29922]";           // orange/amber
    case "manual":
      return "border-l-[#cf222e]";           // red — needs attention
    case "done":
      return "";                              // no special border
    default:
      return "";
  }
}

/**
 * Estimate retry backoff time remaining (in seconds) based on retry_count
 * and the base backoff value. Returns null if not a retry scenario.
 */
export function estimatedRetryBackoff(
  retryCount: number,
  backoffBaseSeconds: number = 60,
): number | null {
  if (retryCount <= 0) return null;
  return backoffBaseSeconds * Math.pow(2, retryCount - 1);
}

/**
 * Classify error_log text into a human-readable category.
 * Used to show actionable hints in the job detail drawer.
 */
export function classifyError(
  errorLog?: string | null,
): { type: "timeout" | "stall" | "auth" | "network" | "unknown"; label: string } {
  if (!errorLog) return { type: "unknown", label: "unknown" };
  const lower = errorLog.toLowerCase();

  // Check for stall detection (our new feature)
  if (lower.includes("stalled") || lower.includes("no progress")) {
    return { type: "stall", label: "stall" };
  }
  // Check for timeout
  if (lower.includes("timeout") || lower.includes("timed out")) {
    return { type: "timeout", label: "timeout" };
  }
  // Check for auth errors — matches backend AUTH_ERROR_PATTERNS
  if (
    lower.includes("401") || lower.includes("403") ||
    lower.includes("unauthorized") || lower.includes("forbidden") ||
    lower.includes("authentication") || lower.includes("login") ||
    lower.includes("cookie") || lower.includes("token") ||
    lower.includes("no valid")
  ) {
    return { type: "auth", label: "auth" };
  }
  // Check for network errors
  if (
    lower.includes("connection") || lower.includes("refused") ||
    lower.includes("network") || lower.includes("unreachable") ||
    lower.includes("resolve") || lower.includes("dns")
  ) {
    return { type: "network", label: "network" };
  }
  return { type: "unknown", label: "unknown" };
}
