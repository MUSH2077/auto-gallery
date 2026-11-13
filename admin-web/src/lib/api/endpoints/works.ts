import { request } from "../client";
import type { CurationCommit, RemoteWorkState, Work, WorkSourceData } from "../types";
import type { DerivativeStatus, MediaAssetData } from "../../media";

export interface WorkAsset extends MediaAssetData {
  file_name: string;
  file_path: string;
  created_at: string;
  derivative_status?: DerivativeStatus;
}

export interface MediaDerivativeProgress {
  total: number;
  completed: number;
  pending: number;
  processing: number;
  failed: number;
  remaining: number;
  affected_works: number;
  completion_percent: number;
  status: "idle" | "waiting" | "running" | "stalled" | "failed" | "complete";
  last_completed_at?: string | null;
  oldest_unfinished_at?: string | null;
  stall_after_seconds: number;
}

// The sources endpoint exposes generic JSON; validate its consumed fields once.
function isWorkSource(value: unknown): value is WorkSourceData {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  return typeof record.id === "string"
    && typeof record.source === "string"
    && ["source_work_id", "source_url", "title", "description", "posted_at"]
      .every(key => record[key] == null || typeof record[key] === "string")
    && (record.raw_metadata == null
      || (typeof record.raw_metadata === "object" && !Array.isArray(record.raw_metadata)));
}

export const worksApi = {
  resolveManualUploadWork: (sourceWorkId: string) => request<{ items: Work[]; total: number }>(
    `/api/v1/works?q=${encodeURIComponent(`pid:manual/${sourceWorkId}`)}&offset=0&limit=2`,
  ),
  deleteWork: (id: string) =>
    request<void>(`/api/v1/works/${id}`, { method: "DELETE" }),

  batchCurateWorks: (ids: string[], action: "trash" | "restore", reason?: string, message?: string) =>
    request<CurationCommit>("/api/v1/works/batch-curate", { method: "POST", body: JSON.stringify({ ids, action, reason, message }) }),

  getWork: (id: string) => request<Work>(`/api/v1/works/${id}`),

  toggleWorkFavorite: (id: string) =>
    request<Work>(`/api/v1/works/${id}/favorite`, { method: "POST" }),

  getWorkSources: async (id: string): Promise<WorkSourceData[]> => {
    const data = await request<unknown>(`/api/v1/works/${id}/sources`);
    if (!Array.isArray(data) || !data.every(isWorkSource)) throw new Error("Invalid work sources response");
    return data;
  },

  getWorkRemoteState: (id: string) => request<RemoteWorkState>(`/api/v1/works/${id}/remote-state`),

  getWorkAssets: (id: string) => request<WorkAsset[]>(`/api/v1/works/${id}/assets`),

  getMediaDerivativeProgress: (signal?: AbortSignal) =>
    request<MediaDerivativeProgress>("/api/v1/works/derivative-progress", { signal }),

  createPlaybackTicket: (workId: string, assetId: string) =>
    request<{ url: string; expires_at: string }>(
      `/api/v1/works/${workId}/assets/${assetId}/playback-ticket`,
      { method: "POST" },
    ),

  getWorkTags: (id: string) => request<{ id: string; normalized_name: string; category?: string }[]>(`/api/v1/works/${id}/tags`),

  mediaUrl: (assetId: string, size: "thumb" | "preview" | "original" | "poster" | "stream" = "thumb") =>
    `/media/${size}/${assetId}`,
};
