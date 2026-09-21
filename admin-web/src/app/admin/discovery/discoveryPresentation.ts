import { ApiError, type DiscoveryCandidate, type RemoteDiscoverySource } from "@/lib/api";
import type { TFunction } from "@/lib/i18n";

export const DISCOVERY_SOURCES: readonly RemoteDiscoverySource[] = ["pixiv", "x", "bilibili"];

export function providerLabel(t: TFunction, source: RemoteDiscoverySource) {
  return t(`discovery.provider_${source}`);
}

export function safeDiscoveryError(t: TFunction, error: unknown, fallback: string) {
  if (error instanceof ApiError && error.status === 403) return t("discovery.forbidden");
  return fallback;
}

export function candidateMetadata(candidate: DiscoveryCandidate) {
  return candidate.metadata && typeof candidate.metadata === "object" ? candidate.metadata : {};
}

export function candidateUsername(candidate: DiscoveryCandidate) {
  const username = candidateMetadata(candidate).username;
  return typeof username === "string" && username ? username : null;
}

export function candidateAvatar(candidate: DiscoveryCandidate) {
  return candidate.avatar_url || null;
}

export function localCreatorIds(candidate: DiscoveryCandidate) {
  const ids = candidateMetadata(candidate).local_creator_ids;
  return Array.isArray(ids) ? ids.filter((value): value is string => typeof value === "string") : [];
}

const REASON_KEYS = new Set([
  "unique_local_identity",
  "danbooru_verified_link",
  "verified_cross_site_link",
  "pixiv_illustration_preview",
  "multiple_creator_evidence",
  "art_focused_bio",
  "recent_visual_post",
  "supported_site_link",
  "single_creator_evidence",
  "no_creator_evidence",
  "multiple_local_identity_matches",
  "manually_resolved_identity",
]);

export function confidenceReasonLabel(t: TFunction, reason: string | Record<string, unknown>) {
  const code = typeof reason === "string"
    ? reason
    : typeof reason.code === "string"
      ? reason.code
      : "unknown";
  return t(`discovery.reason_${REASON_KEYS.has(code) ? code : "unknown"}`);
}
