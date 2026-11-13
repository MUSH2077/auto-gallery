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
