import type { SyncOutcome } from "@/lib/api/types";

const OUTCOME_CODES = new Set(["new_content", "no_changes", "no_content"]);

export function parseSyncOutcome(value: unknown): SyncOutcome | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<SyncOutcome>;
  if (!candidate.code || !OUTCOME_CODES.has(candidate.code)) return null;
  if (typeof candidate.completed_at !== "string") return null;
  return {
    code: candidate.code,
    metadata_count: Number(candidate.metadata_count || 0),
    media_count: Number(candidate.media_count || 0),
    completed_at: candidate.completed_at,
  };
}
