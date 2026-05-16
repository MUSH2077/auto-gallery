const BASE = process.env.NEXT_PUBLIC_API_URL || "";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

// ── Types ──

export interface HealthResponse {
  status: string;
  version: string;
  services: Record<string, string>;
}

export interface ProviderInfo {
  source_name: string;
  display_name: string;
  capabilities: {
    can_download: boolean;
    can_import_local: boolean;
    supports_gallerydl: boolean;
    supports_tags: boolean;
    is_reference_only: boolean;
  };
}

export interface Creator {
  id: string;
  name: string;
  display_name?: string;
  description?: string;
  thumbnail_url?: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreatorLink {
  id: string;
  creator_id: string;
  url: string;
  link_type: string;
  source?: string;
  confidence: number;
  is_verified: boolean;
  notes?: string;
  created_at: string;
  updated_at: string;
}

export interface SourceCreator {
  id: string;
  creator_id: string;
  source: string;
  source_creator_id: string;
  source_url?: string;
  display_name?: string;
  raw_metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Subscription {
  id: string;
  creator_id: string;
  name?: string;
  is_active: boolean;
  sync_enabled: boolean;
  last_synced_at?: string;
  created_at: string;
  updated_at: string;
}

export interface SubscriptionSource {
  id: string;
  subscription_id: string;
  source: string;
  source_creator_id?: string;
  source_url?: string;
  is_enabled: boolean;
  last_successful_auth?: string;
  auth_healthy: boolean;
  created_at: string;
  updated_at: string;
}

export interface DownloadJob {
  id: string;
  subscription_id: string;
  subscription_source_id?: string;
  source: string;
  source_url: string;
  status: string;
  retry_count: number;
  error_log?: string;
  created_at: string;
  updated_at: string;
}

export interface WorkListItem {
  id: string;
  title?: string;
  posted_at?: string;
  is_nsfw: boolean;
  thumbnail_asset_id?: string;
  asset_count: number;
  created_at: string;
}

export interface Work {
  id: string;
  title?: string;
  description?: string;
  posted_at?: string;
  is_nsfw: boolean;
  thumbnail_asset_id?: string;
  asset_count: number;
  created_at: string;
  updated_at: string;
}

export interface Tag {
  id: string;
  normalized_name: string;
  category?: string;
  created_at: string;
}

export interface AdminSettings {
  dedup: {
    source_level_enabled: boolean;
    cross_source_enabled: boolean;
    auto_merge: boolean;
    phash_threshold: number;
  };
}

// ── API ──

export const api = {
  // System
  health: () => request<HealthResponse>("/api/v1/system/health"),

  // Sources
  sources: () => request<{ sources: ProviderInfo[] }>("/api/v1/sources"),

  // Creators
  listCreators: (offset = 0, limit = 50) => request<Creator[]>("/api/v1/creators"),

  getCreator: (id: string) => request<Creator>(`/api/v1/creators/${id}`),

  createCreator: (data: { name: string; display_name?: string; description?: string; thumbnail_url?: string }) =>
    request<Creator>("/api/v1/creators", { method: "POST", body: JSON.stringify(data) }),

  updateCreator: (id: string, data: Record<string, unknown>) =>
    request<Creator>(`/api/v1/creators/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  deleteCreator: (id: string) =>
    request<void>(`/api/v1/creators/${id}`, { method: "DELETE" }),

  listCreatorLinks: (creatorId: string) =>
    request<CreatorLink[]>(`/api/v1/creators/${creatorId}/links`),

  createCreatorLink: (creatorId: string, data: { url: string; link_type: string; source?: string; confidence?: number }) =>
    request<CreatorLink>(`/api/v1/creators/${creatorId}/links`, { method: "POST", body: JSON.stringify(data) }),

  updateCreatorLink: (creatorId: string, linkId: string, data: Record<string, unknown>) =>
    request<CreatorLink>(`/api/v1/creators/${creatorId}/links/${linkId}`, { method: "PATCH", body: JSON.stringify(data) }),

  listSourceCreators: (creatorId: string) =>
    request<SourceCreator[]>(`/api/v1/creators/${creatorId}/sources`),

  createSourceCreator: (creatorId: string, data: { source: string; source_creator_id: string; source_url?: string; display_name?: string }) =>
    request<SourceCreator>(`/api/v1/creators/${creatorId}/sources`, { method: "POST", body: JSON.stringify(data) }),

  deleteCreatorLink: (creatorId: string, linkId: string) =>
    request<void>(`/api/v1/creators/${creatorId}/links/${linkId}`, { method: "DELETE" }),

  // Subscriptions
  listSubscriptions: (offset = 0, limit = 50) => request<Subscription[]>(`/api/v1/subscriptions`),

  getSubscription: (id: string) => request<Subscription>(`/api/v1/subscriptions/${id}`),

  createSubscription: (data: { creator_id: string; name?: string; is_active?: boolean; sync_enabled?: boolean }) =>
    request<Subscription>("/api/v1/subscriptions", { method: "POST", body: JSON.stringify(data) }),

  updateSubscription: (id: string, data: Record<string, unknown>) =>
    request<Subscription>(`/api/v1/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

  deleteSubscription: (id: string) =>
    request<void>(`/api/v1/subscriptions/${id}`, { method: "DELETE" }),

  listSubscriptionSources: (subId: string) =>
    request<SubscriptionSource[]>(`/api/v1/subscriptions/${subId}/sources`),

  createSubscriptionSource: (subId: string, data: { source: string; source_creator_id?: string; source_url?: string; is_enabled?: boolean }) =>
    request<SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources`, { method: "POST", body: JSON.stringify(data) }),

  updateSubscriptionSource: (subId: string, ssId: string, data: Record<string, unknown>) =>
    request<SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "PATCH", body: JSON.stringify(data) }),

  deleteSubscriptionSource: (subId: string, ssId: string) =>
    request<void>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "DELETE" }),

  // Download Jobs
  listDownloadJobs: (status?: string, offset = 0, limit = 50) =>
    request<DownloadJob[]>(`/api/v1/download-jobs${status ? `?status=${status}&offset=${offset}&limit=${limit}` : `?offset=${offset}&limit=${limit}`}`),

  getDownloadJob: (id: string) =>
    request<DownloadJob>(`/api/v1/download-jobs/${id}`),

  createDownloadJob: (data: { subscription_id: string; subscription_source_id?: string; source: string; source_url: string }) =>
    request<{ job_id: string; status: string }>("/api/v1/download-jobs", { method: "POST", body: JSON.stringify(data) }),

  retryDownloadJob: (id: string) =>
    request<{ job_id: string; status: string }>(`/api/v1/download-jobs/${id}/retry`, { method: "POST" }),

  listDownloadJobImports: (jobId: string) =>
    request<{ id: string; download_job_id: string; status: string; error_log?: string }[]>(`/api/v1/download-jobs/${jobId}/imports`),

  // Works
  listWorks: (offset = 0, limit = 50) => request<WorkListItem[]>(`/api/v1/works?offset=${offset}&limit=${limit}`),

  getWork: (id: string) => request<Work>(`/api/v1/works/${id}`),

  getWorkSources: (id: string) => request<unknown[]>(`/api/v1/works/${id}/sources`),

  getWorkAssets: (id: string) => request<{id:string;file_name:string;file_path:string;width?:number;height?:number;mime_type?:string;thumb_sm_path?:string;thumb_md_path?:string}[]>(`/api/v1/works/${id}/assets`),

  getWorkTags: (id: string) => request<{id:string;normalized_name:string;category?:string}[]>(`/api/v1/works/${id}/tags`),

  // Tags
  listTags: (offset = 0, limit = 100) => request<Tag[]>(`/api/v1/tags?offset=${offset}&limit=${limit}`),

  createTag: (data: { normalized_name: string; category?: string }) =>
    request<Tag>("/api/v1/tags", { method: "POST", body: JSON.stringify(data) }),

  updateTag: (id: string, data: { normalized_name?: string; category?: string }) =>
    request<Tag>(`/api/v1/tags/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteTag: (id: string) =>
    request<{ status: string }>(`/api/v1/tags/${id}`, { method: "DELETE" }),

  // Media URL helper (not a fetch call)
  mediaUrl: (assetId: string, size: "thumb" | "preview" | "original" = "thumb") =>
    `/media/${size}/${assetId}`,

  // Search
  search: (q: string, offset = 0, limit = 20) => request<{ results: unknown[]; total: number }>(`/api/v1/search?q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`),

  // Import Jobs
  listImportJobs: (status?: string, offset = 0, limit = 50) =>
    request<{ id: string; download_job_id: string; status: string; error_log?: string; created_at: string }[]>(`/api/v1/import-jobs?offset=${offset}&limit=${limit}${status ? `&status=${status}` : ""}`),

  scanImports: () => request<{ status: string; message: string }>("/api/v1/import-jobs/scan", { method: "POST" }),

  retryImportJob: (id: string) =>
    request<{ status: string; message: string }>(`/api/v1/import-jobs/${id}/retry`, { method: "POST" }),

  deleteImportJob: (id: string) =>
    request<{ status: string }>(`/api/v1/import-jobs/${id}`, { method: "DELETE" }),

  // Admin
  getAdminSettings: () => request<AdminSettings>("/api/v1/admin/settings"),

  updateAdminSettings: (data: { dedup?: { source_level_enabled?: boolean; cross_source_enabled?: boolean; auto_merge?: boolean; phash_threshold?: number } }) =>
    request<{ status: string; message: string }>("/api/v1/admin/settings", { method: "PUT", body: JSON.stringify(data) }),

  reindexSearch: () => request<{ status: string; message: string }>("/api/v1/admin/search/reindex", { method: "POST" }),

  // Danbooru Reference
  previewDanbooruArtist: (tag: string) =>
    request<{ status: string; artist?: { tag: string; name: string; related_urls: string[] } }>("/api/v1/reference/danbooru/artist/preview", { method: "POST", body: JSON.stringify({ tag }) }),

  // gallery-dl Config
  getGalleryDLConfig: () => request<{ refresh_token?: string; cookies_path?: string; filename?: string; directory?: string; include?: string; tags?: string; ugoira?: boolean; sleep_request?: number; max_posts?: number }>("/api/v1/admin/gallerydl-config"),

  updateGalleryDLConfig: (data: Record<string, unknown>) =>
    request<{ status: string; path: string }>("/api/v1/admin/gallerydl-config", { method: "PUT", body: JSON.stringify(data) }),

  // Naming Templates
  listNamingTemplates: () => request<{ id: string; name: string; source?: string; template: string; is_default: boolean }[]>("/api/v1/admin/naming-templates"),

  createNamingTemplate: (data: { name: string; source?: string; template: string; is_default?: boolean }) =>
    request<{ id: string }>("/api/v1/admin/naming-templates", { method: "POST", body: JSON.stringify(data) }),
};

// ── Query Key Factory ──

export const queryKeys = {
  health: ["health"] as const,
  sources: ["sources"] as const,
  creators: {
    all: ["creators"] as const,
    detail: (id: string) => ["creators", id] as const,
    links: (id: string) => ["creators", id, "links"] as const,
  },
  subscriptions: {
    all: ["subscriptions"] as const,
    detail: (id: string) => ["subscriptions", id] as const,
    sources: (id: string) => ["subscriptions", id, "sources"] as const,
  },
  downloadJobs: {
    all: ["download-jobs"] as const,
    detail: (id: string) => ["download-jobs", id] as const,
    imports: (id: string) => ["download-jobs", id, "imports"] as const,
  },
  works: {
    all: ["works"] as const,
    detail: (id: string) => ["works", id] as const,
    sources: (id: string) => ["works", id, "sources"] as const,
  },
  tags: {
    all: ["tags"] as const,
  },
  importJobs: {
    all: ["import-jobs"] as const,
  },
  admin: {
    settings: ["admin", "settings"] as const,
    namingTemplates: ["admin", "naming-templates"] as const,
  },
  reference: {
    danbooru: ["reference", "danbooru"] as const,
  },
} as const;
