import type { WorkListItem } from "@/lib/api";

export type WorksVisibilityFilter = "visible" | "trashed";
export type WorksSafetyFilter = "all" | "sfw" | "nsfw";
export type WorksAiFilter = "all" | "human" | "ai";
export type WorksMediaFilter = "image" | "animation" | "video" | "multiple-assets";

export interface WorksFilterValue {
  visibility: WorksVisibilityFilter;
  sources: string[];
  safety: WorksSafetyFilter;
  ai: WorksAiFilter;
  favorite: boolean;
  media: WorksMediaFilter[];
}

export type WorksSortValue =
  | "relevance"
  | "heat-desc"
  | "random"
  | "created-desc"
  | "created-asc"
  | "posted-desc"
  | "posted-asc"
  | "updated-desc"
  | "updated-asc"
  | "title-desc"
  | "title-asc";

export type WorkCardPreview = {
  work: WorkListItem;
  anchor: DOMRect;
  assetIds: string[];
  pageIndex: number;
};
