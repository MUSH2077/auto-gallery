import { request } from "../client";
import type { CurationCommit, Work, WorkListItem } from "../types";

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
  listWorks: (offset = 0, limit = 50, filters?: { search?: string; source?: string; creator_id?: string; tag?: string; is_nsfw?: boolean; is_favorite?: boolean; is_ai_generated?: boolean; curation_visibility?: string; sort_by?: string; sort_order?: string }) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters?.search) params.set("search", filters.search);
    if (filters?.source) params.set("source", filters.source);
    if (filters?.creator_id) params.set("creator_id", filters.creator_id);
    if (filters?.tag) params.set("tag", filters.tag);
    if (filters?.is_nsfw !== undefined) params.set("is_nsfw", String(filters.is_nsfw));
    if (filters?.is_favorite !== undefined) params.set("is_favorite", String(filters.is_favorite));
    if (filters?.is_ai_generated !== undefined) params.set("is_ai_generated", String(filters.is_ai_generated));
    if (filters?.curation_visibility) params.set("curation_visibility", filters.curation_visibility);
    if (filters?.sort_by) params.set("sort_by", filters.sort_by);
    if (filters?.sort_order) params.set("sort_order", filters.sort_order);
    return request<{ total: number; items: WorkListItem[] }>(`/api/v1/works?${params.toString()}`);
  },

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
