import { request } from "../client";
import type { CurationCommit, Work } from "../types";

export interface WorkAsset {
  id: string;
  file_name: string;
  file_path: string;
  width?: number;
  height?: number;
  mime_type?: string;
  thumb_sm_path?: string;
  thumb_md_path?: string;
  thumb_url?: string;
  preview_url?: string;
  original_url?: string;
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

  getWorkTags: (id: string) => request<{ id: string; normalized_name: string; category?: string }[]>(`/api/v1/works/${id}/tags`),

  mediaUrl: (assetId: string, size: "thumb" | "preview" | "original" = "thumb") =>
    `/media/${size}/${assetId}`,
};
