import { request } from "../client";
import type { CurationCommit, Work } from "../types";
import type { MediaAssetData } from "../../media";

export interface WorkAsset extends MediaAssetData {
  file_name: string;
  file_path: string;
  created_at: string;
}

export const worksApi = {
  deleteWork: (id: string) =>
    request<void>(`/api/v1/works/${id}`, { method: "DELETE" }),

  batchCurateWorks: (ids: string[], action: "trash" | "restore", reason?: string, message?: string) =>
    request<CurationCommit>("/api/v1/works/batch-curate", { method: "POST", body: JSON.stringify({ ids, action, reason, message }) }),

  getWork: (id: string) => request<Work>(`/api/v1/works/${id}`),

  toggleWorkFavorite: (id: string) =>
    request<Work>(`/api/v1/works/${id}/favorite`, { method: "POST" }),

  getWorkSources: (id: string) => request<unknown[]>(`/api/v1/works/${id}/sources`),

  getWorkAssets: (id: string) => request<WorkAsset[]>(`/api/v1/works/${id}/assets`),

  createPlaybackTicket: (workId: string, assetId: string) =>
    request<{ url: string; expires_at: string }>(
      `/api/v1/works/${workId}/assets/${assetId}/playback-ticket`,
      { method: "POST" },
    ),

  getWorkTags: (id: string) => request<{ id: string; normalized_name: string; category?: string }[]>(`/api/v1/works/${id}/tags`),

  mediaUrl: (assetId: string, size: "thumb" | "preview" | "original" | "poster" | "stream" = "thumb") =>
    `/media/${size}/${assetId}`,
};
