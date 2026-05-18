const BASE = process.env.NEXT_PUBLIC_API_URL || "";
const ADMIN_KEY = process.env.NEXT_PUBLIC_ADMIN_KEY || "changeme";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", "X-Admin-Key": ADMIN_KEY, ...options?.headers },
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
  danbooru_artist_id?: number;
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
  sync_interval_hours: number;
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
  source?: string;
  creator_name?: string;
  creator_id?: string;
  has_ugoira?: boolean;
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

export interface DedupSettings {
  source_level_enabled: boolean;
  cross_source_enabled: boolean;
  auto_merge: boolean;
  phash_threshold: number;
}

export interface SubscriptionDefaults {
  default_sync_interval_hours: number;
  scheduler_scan_interval_minutes: number;
  default_sync_enabled: boolean;
  schedule_mode: "interval" | "fixed_time";
  scheduled_times: string;
}

export interface DownloadDefaults {
  timeout_seconds: number;
  max_retries: number;
  retry_backoff_base_seconds: number;
}

// Gallery-dl multi-source config types

export interface PixivSourceConfig {
  refresh_token?: string;
  cookies_path?: string;
  filename?: string;
  directory?: string;
  include?: string;
  tags?: string;
  ugoira?: string;
  sleep_request?: number;
  max_posts?: number;
}

export interface TwitterSourceConfig {
  cookies_path?: string;
  filename?: string;
  directory?: string;
  include?: string;
  retweets?: boolean;
  replies?: boolean;
  cards?: boolean;
  videos?: boolean;
  text_tweets?: boolean;
  quoted?: boolean;
  max_posts?: number;
}

export interface IwaraSourceConfig {
  cookies_path?: string;
  username?: string;
  password?: string;
  filename?: string;
  directory?: string;
  format?: string;
}

export interface GalleryDLSourceMeta {
  name: string;
  supported: boolean;
  description: string;
}

export interface GalleryDLMultiConfig {
  pixiv: PixivSourceConfig;
  twitter: TwitterSourceConfig;
  iwara: IwaraSourceConfig;
  sources: Record<string, GalleryDLSourceMeta>;
}

export interface ProxySettings {
  http_proxy: string;
  https_proxy: string;
  no_proxy: string;
  enabled: boolean;
}

export interface AdminSettings {
  dedup: DedupSettings;
  subscription_defaults: SubscriptionDefaults;
  download_defaults: DownloadDefaults;
  proxy: ProxySettings;
}

export interface AuthStatusItem {
  id: string;
  source: string;
  source_url: string;
  source_creator_id?: string;
  auth_healthy: boolean | null;
  last_successful_auth: string | null;
  is_enabled: boolean;
  subscription: {
    id: string;
    name?: string;
    is_active: boolean;
    sync_enabled: boolean;
  };
  creator: {
    id: string;
    name: string;
    display_name?: string;
  };
}

export interface AuthStatusResponse {
  sources: AuthStatusItem[];
  summary: {
    total: number;
    healthy: number;
    unhealthy: number;
    unknown: number;
  };
}

// ── API ──

export const api = {
  // System
  health: () => request<HealthResponse>("/api/v1/system/health"),

  storageStats: () => request<{
    downloads: { path: string; size_bytes: number; file_count: number };
    library: { path: string; size_bytes: number; file_count: number };
    disk: { total_bytes: number; free_bytes: number; used_bytes: number };
  }>("/api/v1/system/storage"),

  queueStats: () => request<{ default_queue: number; scheduled_queue: number; failed_jobs: number }>("/api/v1/system/queue-stats"),

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

  batchDeleteCreators: (ids: string[]) =>
    request<{ status: string; results: { id: string; status: string; error?: string }[] }>(
      "/api/v1/creators/batch-delete", { method: "POST", body: JSON.stringify({ ids }) }),

  listDuplicateCreators: () =>
    request<{ duplicates: { reason: string; description: string; creator_ids: string[]; creator_names: string[] }[]; total: number }>("/api/v1/creators/duplicates"),

  mergeCreators: (targetId: string, sourceIds: string[]) =>
    request<{ status: string; results: { source_id: string; status: string; links_moved?: number; source_creators_moved?: number; subscriptions_moved?: number; error?: string }[] }>(
      "/api/v1/creators/merge", { method: "POST", body: JSON.stringify({ target_id: targetId, source_ids: sourceIds }) }),

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

  batchDeleteSubscriptions: (ids: string[]) =>
    request<{ status: string; results: { id: string; status: string; error?: string }[] }>(
      "/api/v1/subscriptions/batch-delete", { method: "POST", body: JSON.stringify({ ids }) }),

  batchToggleSyncSubscriptions: (ids: string[], syncEnabled: boolean) =>
    request<{ status: string; results: { id: string; status: string; sync_enabled?: boolean; error?: string }[] }>(
      "/api/v1/subscriptions/batch-toggle-sync", { method: "POST", body: JSON.stringify({ ids, sync_enabled: syncEnabled }) }),

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
  listWorks: (offset = 0, limit = 50, filters?: { search?: string; source?: string; creator_id?: string; is_nsfw?: boolean; sort_by?: string; sort_order?: string }) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters?.search) params.set("search", filters.search);
    if (filters?.source) params.set("source", filters.source);
    if (filters?.creator_id) params.set("creator_id", filters.creator_id);
    if (filters?.is_nsfw !== undefined) params.set("is_nsfw", String(filters.is_nsfw));
    if (filters?.sort_by) params.set("sort_by", filters.sort_by);
    if (filters?.sort_order) params.set("sort_order", filters.sort_order);
    return request<WorkListItem[]>(`/api/v1/works?${params.toString()}`);
  },

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

  updateAdminSettings: (data: {
    dedup?: Partial<DedupSettings>;
    subscription_defaults?: Partial<SubscriptionDefaults>;
    download_defaults?: Partial<DownloadDefaults>;
    proxy?: Partial<ProxySettings>;
  }) =>
    request<{ status: string; message: string }>("/api/v1/admin/settings", { method: "PUT", body: JSON.stringify(data) }),

  reindexSearch: () => request<{ status: string; message: string }>("/api/v1/admin/search/reindex", { method: "POST" }),

  getAuthStatus: () => request<AuthStatusResponse>("/api/v1/admin/auth-status"),

  testProxy: () => request<{
    proxy_enabled: boolean;
    proxy_config: { http: string; https: string };
    results: { name: string; url: string; direct_ok: boolean; direct_ms: number; proxy_ok: boolean | null; proxy_ms: number | null }[];
  }>("/api/v1/admin/proxy/test", { method: "POST" }),

  clearEntity: (entity: string) =>
    request<{ status: string; message: string; deleted?: Record<string, number> }>(`/api/v1/admin/clear/${entity}`, { method: "POST" }),

  resetSettings: () =>
    request<{ status: string; message: string }>("/api/v1/admin/reset-settings", { method: "POST" }),

  listDuplicates: () => request<{ duplicates: { source: string; source_work_id: string; count: number; work_ids: string[] }[]; total: number }>("/api/v1/admin/dedup/duplicates"),
  scanDuplicates: () => request<{ status: string; unique_works: number; total_source_records: number; message: string }>("/api/v1/admin/dedup/scan", { method: "POST" }),
  listMergeCandidates: () => request<{ candidates: { title: string; source_count: number; sources: string[]; work_ids: string[] }[]; total: number }>("/api/v1/admin/merge-candidates"),

  // Danbooru Reference
  previewDanbooruArtist: (params: { url?: string; pixiv_id?: string; name?: string }) =>
    request<{
      status: string; found?: boolean; message?: string;
      artist?: { id: number; name: string; other_names: string[]; post_count?: number;
                 notes?: string; is_active?: boolean; created_at?: string;
                 urls: { url: string; normalized_url: string; is_active: boolean }[] };
      suggested_links?: { url: string; link_type: string; source: string; confidence: number;
                          is_verified: boolean; notes?: string }[];
    }>("/api/v1/reference/danbooru/artist/preview", { method: "POST", body: JSON.stringify(params) }),

  importDanbooruArtist: (params: { creator_id: string; url?: string; pixiv_id?: string; name?: string }) =>
    request<{ status: string; imported: number; artist_name?: string }>("/api/v1/reference/danbooru/artist/import", { method: "POST", body: JSON.stringify(params) }),

  importAllDanbooru: (params: { creator_id?: string; creator_name?: string; url?: string; pixiv_id?: string; name?: string }) =>
    request<{ status: string; found?: boolean; creator_id?: string; artist_name?: string; links_imported: number; sources_created: number; subscription_id?: string }>("/api/v1/reference/danbooru/artist/import-all", { method: "POST", body: JSON.stringify(params) }),

  batchImportDanbooru: (pixivIds: string[]) =>
    request<{
      total: number; imported_count: number; low_confidence_count: number;
      not_found_count: number; error_count: number;
      imported: { pixiv_id: string; creator_id: string; artist_name: string;
                  artist_id: number; links_imported: number; sources_created: number;
                  downloadable_urls: string[] }[];
      low_confidence: { pixiv_id: string; artist_name: string; artist_id: number;
                        url_count: number; message: string }[];
      not_found: { pixiv_id: string; message: string }[];
      errors: { pixiv_id: string; error: string }[];
    }>("/api/v1/reference/danbooru/artist/batch-import", { method: "POST", body: JSON.stringify({ pixiv_ids: pixivIds }) }),

  // gallery-dl Config
  getGalleryDLConfig: (source?: string) => request<GalleryDLMultiConfig>(`/api/v1/admin/gallerydl-config${source ? `?source=${source}` : ""}`),

  updateGalleryDLConfig: (data: { pixiv?: Partial<PixivSourceConfig>; twitter?: Partial<TwitterSourceConfig>; iwara?: Partial<IwaraSourceConfig> }) =>
    request<{ status: string; message: string; path: string }>("/api/v1/admin/gallerydl-config", { method: "PUT", body: JSON.stringify(data) }),

  // Naming Templates
  listNamingTemplates: () => request<{ id: string; name: string; source?: string; template: string; is_default: boolean }[]>("/api/v1/admin/naming-templates"),

  createNamingTemplate: (data: { name: string; source?: string; template: string; is_default?: boolean }) =>
    request<Record<string, unknown>>("/api/v1/admin/naming-templates", { method: "POST", body: JSON.stringify(data) }),

  updateNamingTemplate: (id: string, data: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/v1/admin/naming-templates/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteNamingTemplate: (id: string) =>
    request<void>(`/api/v1/admin/naming-templates/${id}`, { method: "DELETE" }),
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
