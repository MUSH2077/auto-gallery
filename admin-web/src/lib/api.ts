const BASE = "";

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!headers.has("Content-Type") && !(options?.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  // Attach JWT token if present in localStorage
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("ag_token");
    if (token && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
  }

  const res = await fetch(`${BASE}${path}`, {
    headers,
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
  business?: {
    queues?: Record<string, number>;
    jobs?: Record<string, number>;
    scheduler?: Record<string, string | null>;
    gallerydl?: Record<string, unknown>;
  };
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
  is_favorite: boolean;
  subscription_count?: number;
  source_count?: number;
  repository_count?: number;
  last_synced_at?: string;
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
  creator_name?: string;
  creator_display_name?: string;
  is_active: boolean;
  sync_enabled: boolean;
  sync_interval_hours: number;
  schedule_mode?: string | null;
  scheduled_times?: string | null;
  last_synced_at?: string;
  source_count?: number;
  enabled_source_count?: number;
  latest_job_status?: string;
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
  last_synced_at?: string;
  last_attempted_at?: string;
  auth_status?: string | null;
  auth_error_reason?: string | null;
  last_auth_checked_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepositoryLatestJob {
  id: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
  error_log_excerpt?: string | null;
}

export interface CreatorRepository {
  id: string;
  subscription_id: string;
  source: string;
  source_display_name?: string;
  source_creator_id?: string;
  source_url?: string;
  is_enabled: boolean;
  auth_healthy: boolean;
  last_successful_auth?: string | null;
  last_synced_at?: string | null;
  last_attempted_at?: string | null;
  auth_status?: string | null;
  auth_error_reason?: string | null;
  last_auth_checked_at?: string | null;
  can_download: boolean;
  supports_gallerydl: boolean;
  url_valid: boolean;
  is_repository: boolean;
  latest_job?: RepositoryLatestJob | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CreatorSubscriptionOverview {
  creator_id: string;
  subscriptions: {
    id: string;
    name?: string | null;
    is_active: boolean;
    sync_enabled: boolean;
    sync_interval_hours: number;
    schedule_mode?: string | null;
    scheduled_times?: string | null;
    last_synced_at?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
  }[];
  repositories: CreatorRepository[];
  summary: {
    subscription_count: number;
    repository_count: number;
    enabled_repository_count: number;
    running_job_count: number;
  };
}

export interface WorkbenchSummary {
  updated_at: string;
  queue: {
    default: number;
    scheduled: number;
    failed: number;
    started?: number;
    active_download_count: number;
    active_import_count: number;
    failed_download_count: number;
    failed_import_count: number;
    stale_download_count: number;
    stale_import_count: number;
    stale_count: number;
  };
  scheduler: {
    enabled: boolean;
    mode: string;
    timezone: string;
    scheduled_times?: string | null;
    scan_interval_minutes: number;
    next_scan_at?: string | null;
  };
  storage: {
    original_media_size_bytes: number;
    original_media_file_count: number;
    library_size_bytes: number;
    library_file_count: number;
    disk_total_bytes: number;
    disk_free_bytes: number;
    disk_used_bytes: number;
    disk_used_percent?: number | null;
    disk_free_percent?: number | null;
    risk_level: "ok" | "warning" | "critical" | "unknown" | string;
  };
  health: Record<string, string>;
  attention: {
    auth_unhealthy_count: number;
    failed_download_count: number;
    failed_import_count: number;
    stale_job_count: number;
    low_disk_warning: boolean;
    scheduler_disabled_warning: boolean;
  };
  recent: {
    download_jobs: {
      id: string;
      subscription_id: string;
      subscription_source_id?: string | null;
      source: string;
      source_url: string;
      status: string;
      created_at?: string | null;
      updated_at?: string | null;
      error_log_excerpt?: string | null;
    }[];
    import_jobs: {
      id: string;
      download_job_id: string;
      status: string;
      created_at?: string | null;
      updated_at?: string | null;
      error_log_excerpt?: string | null;
    }[];
    works: {
      id: string;
      title?: string | null;
      thumbnail_asset_id?: string | null;
      created_at?: string | null;
    }[];
    successful_syncs: {
      source_id: string;
      subscription_id: string;
      creator_id: string;
      creator_name: string;
      source: string;
      source_url?: string | null;
      last_synced_at?: string | null;
    }[];
  };
}

export interface SchedulerDecisionItem {
  subscription_id: string;
  subscription_name?: string | null;
  subscription_active: boolean;
  subscription_sync_enabled: boolean;
  creator_id: string;
  creator_name: string;
  source_id: string;
  source: string;
  source_display_name?: string | null;
  source_url?: string | null;
  source_creator_id?: string | null;
  source_enabled: boolean;
  effective_mode: string;
  timezone: string;
  scheduled_times?: string | null;
  sync_interval_hours: number;
  last_synced_at?: string | null;
  last_attempted_at?: string | null;
  due: boolean;
  decision: string;
  reason: string;
  next_due_at?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  auth_healthy: boolean;
  url_valid: boolean;
  can_download: boolean;
}

export interface SchedulerDecisionsResponse {
  updated_at: string;
  scheduler_enabled: boolean;
  timezone: string;
  items: SchedulerDecisionItem[];
}

export interface DownloadJob {
  id: string;
  subscription_id: string;
  subscription_source_id?: string;
  creator_id?: string | null;
  creator_name?: string | null;
  subscription_name?: string | null;
  source: string;
  source_url: string;
  status: string;
  retry_count: number;
  error_log?: string;
  gallerydl_config_path?: string | null;
  download_dir?: string | null;
  manifest?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface WorkListItem {
  id: string;
  title?: string;
  posted_at?: string;
  is_nsfw: boolean;
  is_ai_generated: boolean;
  thumbnail_asset_id?: string;
  asset_count: number;
  created_at: string;
  source?: string;
  creator_name?: string;
  creator_id?: string;
  has_ugoira?: boolean;
  preview_asset_ids?: string[];
  is_favorite?: boolean;
}

export interface SearchWorkResult extends WorkListItem {
  description?: string;
  tags?: string[];
}

export interface Work {
  id: string;
  title?: string;
  description?: string;
  posted_at?: string;
  is_nsfw: boolean;
  is_ai_generated: boolean;
  thumbnail_asset_id?: string;
  asset_count: number;
  is_favorite: boolean;
  created_at: string;
  updated_at: string;
}

export interface Tag {
  id: string;
  normalized_name: string;
  category?: string;
  usage_count: number;
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
  scheduler_enabled: boolean;
  schedule_mode: "interval" | "fixed_time";
  scheduled_times: string;
  timezone: string;
}

export interface DownloadDefaults {
  timeout_seconds: number;
  max_retries: number;
  retry_backoff_base_seconds: number;
  max_posts: number;
  skip_ai_generated: boolean;
}

// Gallery-dl multi-source config types

export interface PixivSourceConfig {
  auto_enable_on_import?: boolean;
  refresh_token?: string;
  cookies_path?: string;
  cookie_content?: string;
  filename?: string;
  directory?: string;
  include?: string;
  tags?: string;
  ugoira?: string;
  sleep_request?: number;
  max_posts?: number;
  metadata?: boolean;
  metadata_bookmark?: boolean;
  captions?: boolean;
  comments?: boolean;
  sanity?: boolean;
}

export interface TwitterSourceConfig {
  auto_enable_on_import?: boolean;
  cookies_path?: string;
  cookie_content?: string;
  filename?: string;
  directory?: string;
  strategy?: string;
  include?: string;
  retweets?: boolean;
  replies?: boolean;
  cards?: boolean;
  videos?: boolean;
  text_tweets?: boolean;
  quoted?: boolean;
  pinned?: boolean;
  previews?: boolean;
  articles?: boolean;
  max_posts?: number;
}

export interface IwaraSourceConfig {
  auto_enable_on_import?: boolean;
  cookies_path?: string;
  cookie_content?: string;
  username?: string;
  password?: string;
  filename?: string;
  directory?: string;
  format?: string;
  include?: string;
}

export interface GalleryDLSourceMeta {
  name: string;
  supported: boolean;
  description: string;
}

export interface DanbooruSourceConfig {
  auto_enable_on_import?: boolean;
  username?: string;
  password?: string;
  api_key?: string;
  cookies_path?: string;
  cookie_content?: string;
  external?: boolean;
  metadata?: boolean;
  filename?: string;
  directory?: string;
}

export interface PinterestSourceConfig {
  auto_enable_on_import?: boolean;
  domain?: string;
  stories?: boolean;
  videos?: boolean;
  sections?: boolean;
  cookies_path?: string;
  cookie_content?: string;
  filename?: string;
  directory?: string;
}

export interface LofterSourceConfig {
  auto_enable_on_import?: boolean;
  cookies_path?: string;
  cookie_content?: string;
  filename?: string;
  directory?: string;
}

export interface WeiboSourceConfig {
  auto_enable_on_import?: boolean;
  cookies_path?: string;
  cookie_content?: string;
  videos?: boolean;
  retweets?: boolean;
  gifs?: boolean;
  livephoto?: boolean;
  movies?: boolean;
  text?: boolean;
  include?: string;
  filename?: string;
  directory?: string;
}

export interface BilibiliSourceConfig {
  auto_enable_on_import?: boolean;
  livephoto?: boolean;
  sleep_request?: string;
  filename?: string;
  directory?: string;
}

export interface GalleryDLMultiConfig {
  pixiv: PixivSourceConfig;
  twitter: TwitterSourceConfig;
  iwara: IwaraSourceConfig;
  danbooru: DanbooruSourceConfig;
  pinterest: PinterestSourceConfig;
  lofter: LofterSourceConfig;
  weibo: WeiboSourceConfig;
  bilibili: BilibiliSourceConfig;
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
  auth_status?: string | null;
  auth_error_reason?: string | null;
  last_auth_checked_at?: string | null;
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

  workbench: () => request<WorkbenchSummary>("/api/v1/system/workbench"),

  schedulerDecisions: () => request<SchedulerDecisionsResponse>("/api/v1/system/scheduler-decisions"),

  systemLogs: (limit?: number, level?: string, nameFilter?: string) => {
    const params = new URLSearchParams();
    if (limit) params.set("limit", String(limit));
    if (level) params.set("level", level);
    if (nameFilter) params.set("name", nameFilter);
    return request<{ entries: { ts: string; level: string; name: string; msg: string }[]; total: number; levels: string[] }>(`/api/v1/system/logs?${params}`);
  },

  storageStats: () => request<{
    downloads: { path: string; size_bytes: number; file_count: number };
    library: { path: string; size_bytes: number; file_count: number };
    disk: { total_bytes: number; free_bytes: number; used_bytes: number };
  }>("/api/v1/system/storage"),

  queueStats: () => request<{
    default_queue: number;
    scheduled_queue: number;
    failed_jobs: number;
    scheduler_enabled?: boolean;
    scheduler_mode?: string;
    scheduler_timezone?: string;
    scheduled_times?: string;
    scheduler_scan_interval_minutes?: number;
    next_sync_scan_at?: string | null;
  }>("/api/v1/system/queue-stats"),

  // Sources
  sources: () => request<{ sources: ProviderInfo[] }>("/api/v1/sources"),

  // Creators

  countCreators: () => request<{ count: number }>("/api/v1/creators/count"),
  countSubscriptions: () => request<{ count: number }>("/api/v1/subscriptions/count"),
  listCreators: (offset = 0, limit = 50, filters?: {
    search?: string; is_active?: boolean; has_danbooru?: boolean;
    has_subscription?: boolean; is_favorite?: boolean;
  }) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters?.search) params.set("search", filters.search);
    if (filters?.is_active !== undefined) params.set("is_active", String(filters.is_active));
    if (filters?.has_danbooru !== undefined) params.set("has_danbooru", String(filters.has_danbooru));
    if (filters?.has_subscription !== undefined) params.set("has_subscription", String(filters.has_subscription));
    if (filters?.is_favorite !== undefined) params.set("is_favorite", String(filters.is_favorite));
    return request<Creator[]>(`/api/v1/creators?${params.toString()}`);
  },

  getCreator: (id: string) => request<Creator>(`/api/v1/creators/${id}`),
  getCreatorTimeline: (creatorId: string, fromDate?: string, toDate?: string) => {
      const q = new URLSearchParams();
      if (fromDate) q.set("from_date", fromDate);
      if (toDate) q.set("to_date", toDate);
      return request<{ creator_id: string; sources: string[]; days: { date: string; total: number; [source: string]: number | string }[]; total: number }>(`/api/v1/creators/${creatorId}/timeline?${q.toString()}`);
    },

  getCreatorStats: (id: string) =>
    request<{
      creator_id: string; total_works: number; total_assets: number; total_tags: number;
      source_breakdown: { source: string; count: number }[];
      tag_distribution: { tag: string; count: number }[];
      monthly_frequency: { month: string; count: number }[];
    }>(`/api/v1/creators/${id}/stats`),

  getCreatorSubscriptionOverview: (id: string) =>
    request<CreatorSubscriptionOverview>(`/api/v1/creators/${id}/subscription-overview`),

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

  toggleCreatorFavorite: (id: string) =>
    request<Creator>(`/api/v1/creators/${id}/favorite`, { method: "POST" }),

  deleteCreatorLink: (creatorId: string, linkId: string) =>
    request<void>(`/api/v1/creators/${creatorId}/links/${linkId}`, { method: "DELETE" }),

  // Subscriptions
  listSubscriptions: (offset = 0, limit = 50, filters?: {
    search?: string; is_active?: boolean; sync_enabled?: boolean; never_synced?: boolean;
  }) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters?.search) params.set("search", filters.search);
    if (filters?.is_active !== undefined) params.set("is_active", String(filters.is_active));
    if (filters?.sync_enabled !== undefined) params.set("sync_enabled", String(filters.sync_enabled));
    if (filters?.never_synced !== undefined) params.set("never_synced", String(filters.never_synced));
    return request<Subscription[]>(`/api/v1/subscriptions?${params.toString()}`);
  },

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

  syncNowSubscription: (id: string) =>
    request<{ status: string; message: string; job_ids: string[] }>(`/api/v1/subscriptions/${id}/sync-now`, { method: "POST" }),

  listSubscriptionSources: (subId: string) =>
    request<SubscriptionSource[]>(`/api/v1/subscriptions/${subId}/sources`),

  createSubscriptionSource: (subId: string, data: { source: string; source_creator_id?: string; source_url?: string; is_enabled?: boolean }) =>
    request<SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources`, { method: "POST", body: JSON.stringify(data) }),

  updateSubscriptionSource: (subId: string, ssId: string, data: Record<string, unknown>) =>
    request<SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "PATCH", body: JSON.stringify(data) }),

  deleteSubscriptionSource: (subId: string, ssId: string) =>
    request<void>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "DELETE" }),

  // Download Jobs
  listDownloadJobs: (params?: { status?: string; source?: string; subscription_id?: string; sort_by?: string; sort_order?: string; offset?: number; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.source) q.set("source", params.source);
      if (params?.subscription_id) q.set("subscription_id", params.subscription_id);
      if (params?.sort_by) q.set("sort_by", params.sort_by);
      if (params?.sort_order) q.set("sort_order", params.sort_order);
      q.set("offset", String(params?.offset || 0));
      q.set("limit", String(params?.limit || 50));
      return request<DownloadJob[]>(`/api/v1/download-jobs?${q.toString()}`);
    },

  getDownloadJob: (id: string) =>
    request<DownloadJob>(`/api/v1/download-jobs/${id}`),

  createDownloadJob: (data: { subscription_id: string; subscription_source_id?: string; source: string; source_url: string }) =>
    request<{ job_id: string; status: string }>("/api/v1/download-jobs", { method: "POST", body: JSON.stringify(data) }),

  retryDownloadJob: (id: string) =>
    request<{ job_id: string; status: string }>(`/api/v1/download-jobs/${id}/retry`, { method: "POST" }),

  getDownloadJobImports: (jobId: string) =>
    request<any[]>(`/api/v1/download-jobs/${jobId}/imports`),

  clearDownloadJobs: (statuses: string[]) =>
    request<{ status: string; deleted: number }>(`/api/v1/download-jobs/clear`, { method: "POST", body: JSON.stringify({ statuses }) }),

  killStuckJobs: () =>
    request<{ status: string; killed: number }>(`/api/v1/download-jobs/kill-stuck`, { method: "POST" }),

  retryAllFailedJobs: () =>
    request<{ status: string; succeeded: number; failed: number }>(`/api/v1/download-jobs/retry-all`, { method: "POST" }),

  pauseDownloadJob: (id: string) =>
    request<{ job_id: string; status: string }>(`/api/v1/download-jobs/${id}/pause`, { method: "POST" }),

  resumeDownloadJob: (id: string) =>
    request<{ job_id: string; status: string }>(`/api/v1/download-jobs/${id}/resume`, { method: "POST" }),

  batchDownloadJobs: (ids: string[], action: string) =>
    request<{ succeeded: number; failed: number; errors?: { id: string; error: string }[] }>("/api/v1/download-jobs/batch", { method: "POST", body: JSON.stringify({ ids, action }) }),

  listDownloadJobImports: (jobId: string) =>
    request<{ id: string; download_job_id: string; status: string; error_log?: string }[]>(`/api/v1/download-jobs/${jobId}/imports`),

  // Works
  listWorks: (offset = 0, limit = 50, filters?: { search?: string; source?: string; creator_id?: string; tag?: string; is_nsfw?: boolean; is_favorite?: boolean; is_ai_generated?: boolean; sort_by?: string; sort_order?: string }) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filters?.search) params.set("search", filters.search);
    if (filters?.source) params.set("source", filters.source);
    if (filters?.creator_id) params.set("creator_id", filters.creator_id);
    if (filters?.tag) params.set("tag", filters.tag);
    if (filters?.is_nsfw !== undefined) params.set("is_nsfw", String(filters.is_nsfw));
    if (filters?.is_favorite !== undefined) params.set("is_favorite", String(filters.is_favorite));
    if (filters?.is_ai_generated !== undefined) params.set("is_ai_generated", String(filters.is_ai_generated));
    if (filters?.sort_by) params.set("sort_by", filters.sort_by);
    if (filters?.sort_order) params.set("sort_order", filters.sort_order);
    return request<{ total: number; items: WorkListItem[] }>(`/api/v1/works?${params.toString()}`);
  },

  deleteWork: (id: string) =>
    request<void>(`/api/v1/works/${id}`, { method: "DELETE" }),

  getWork: (id: string) => request<Work>(`/api/v1/works/${id}`),

  toggleWorkFavorite: (id: string) =>
    request<Work>(`/api/v1/works/${id}/favorite`, { method: "POST" }),

  getWorkSources: (id: string) => request<unknown[]>(`/api/v1/works/${id}/sources`),

  getWorkAssets: (id: string) => request<{id:string;file_name:string;file_path:string;width?:number;height?:number;mime_type?:string;thumb_sm_path?:string;thumb_md_path?:string;created_at:string}[]>(`/api/v1/works/${id}/assets`),

  getWorkTags: (id: string) => request<{id:string;normalized_name:string;category?:string}[]>(`/api/v1/works/${id}/tags`),

  // Tags
  listTags: (offset = 0, limit = 100, sortBy = "usage_count", sortOrder = "desc") =>
    request<Tag[]>(`/api/v1/tags?offset=${offset}&limit=${limit}&sort_by=${sortBy}&sort_order=${sortOrder}`),

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
  search: (q: string, offset = 0, limit = 20) => request<{ results: SearchWorkResult[]; total: number; creators?: { id: string; name: string; display_name?: string }[]; tags?: { id: string; normalized_name: string; category?: string }[] }>(`/api/v1/search?q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`),

  // Import Jobs
  listImportJobs: (status?: string, offset = 0, limit = 50) =>
    request<{ total: number; items: { id: string; download_job_id: string; status: string; error_log?: string; created_at: string }[] }>(`/api/v1/import-jobs?offset=${offset}&limit=${limit}${status ? `&status=${status}` : ""}`),

  scanImports: () => request<{ status: string; message: string }>("/api/v1/import-jobs/scan", { method: "POST" }),

  retryImportJob: (id: string) =>
    request<{ status: string; message: string }>(`/api/v1/import-jobs/${id}/retry`, { method: "POST" }),

  deleteDownloadJob: (id: string) =>
    request<{ status: string }>(`/api/v1/download-jobs/${id}`, { method: "DELETE" }),

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
    proxy_reachable: boolean | null;
    proxy_reachable_error: string;
    proxy_config: { http: string; https: string };
    results: { name: string; url: string; direct_ok: boolean; direct_ms: number; direct_error: string; proxy_ok: boolean | null; proxy_ms: number | null; proxy_error: string }[];
  }>("/api/v1/admin/proxy/test", { method: "POST" }),

  getSystemInfo: () => request<{ version: string; downloads_size_mb: number; library_size_mb: number; downloads_free_gb: number; archives_kb: Record<string, number> }>("/api/v1/admin/system-info"),
  getImportProgress: () => request<{ running: number; pending: number; complete: number; failed: number; recent: { id: string; status: string; error: string }[] }>("/api/v1/admin/import-progress"),
  cleanupMetadataJSONs: () => request<{ status: string; removed: number }>("/api/v1/admin/cleanup-metadata-jsons", { method: "POST" }),
  getStorageBreakdown: () => request<{
    sources: Record<string, { size_mb: number; creator_count: number; work_count: number }>;
    creators: { name: string; display_name: string; source: string; size_mb: number; work_count: number; creator_id?: string }[];
    layers?: Record<string, { path: string; size_mb: number; description: string }>;
  }>("/api/v1/admin/storage-breakdown"),
  getIntegrityCheck: () => request<{
    issues: { type: string; severity: string; count: number; description: string; items: any[] }[];
    db_stats: Record<string, number>;
    checked_at: string;
  }>("/api/v1/admin/integrity-check"),
  clearEntity: (entity: string) =>
    request<{ status: string; message: string; deleted?: Record<string, number> }>(`/api/v1/admin/clear/${entity}`, { method: "POST" }),

  resetSettings: () =>
    request<{ status: string; message: string }>("/api/v1/admin/reset-settings", { method: "POST" }),

  triggerSyncNow: () =>
    request<{ status: string; message: string; job_id: string }>("/api/v1/admin/scheduler/sync-now", { method: "POST" }),

  clearFailedJobs: () =>
    request<{ status: string; message: string }>("/api/v1/system/clear-failed-jobs", { method: "POST" }),

  listDuplicates: () => request<{ duplicates: { source: string; source_work_id: string; count: number; work_ids: string[] }[]; total: number }>("/api/v1/admin/dedup/duplicates"),
  scanDuplicates: () => request<{ status: string; unique_works: number; total_source_records: number; message: string }>("/api/v1/admin/dedup/scan", { method: "POST" }),
  listMergeCandidates: () => request<{ candidates: { title: string; source_count: number; sources: string[]; work_ids: string[] }[]; total: number }>("/api/v1/admin/merge-candidates"),

  // Danbooru Reference
  getDanbooruArtist: (artistId: number) =>
    request<{
      artist: { id: number; name: string; other_names: string[]; pixiv_display_name?: string | null };
    }>(`/api/v1/reference/danbooru/artist/${artistId}`),
  previewDanbooruArtist: (params: { url?: string; pixiv_id?: string; name?: string }) =>
    request<{
      status: string; found?: boolean; message?: string;
      artist?: { id: number; name: string; other_names: string[]; post_count?: number;
                 notes?: string; is_active?: boolean; created_at?: string;
                 pixiv_display_name?: string | null;
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
      status: string; message: string; job_id: string; total: number;
      duplicates_removed?: number;
      already_exists?: { pixiv_id: string; creator_name: string; creator_id: string }[];
    }>(
      "/api/v1/reference/danbooru/artist/batch-import",
      { method: "POST", body: JSON.stringify({ pixiv_ids: pixivIds }) }),

  urlBatchImportDanbooru: (urls: string[]) =>
    request<{
      status: string; message: string; job_id: string; batch_id: string; total: number;
    }>(
      "/api/v1/reference/danbooru/url-batch-import",
      { method: "POST", body: JSON.stringify({ urls }) }),

  syncDanbooruFavorites: () =>
    request<{
      status: string; message?: string;
      total_favorites?: number; created: number; matched: number; errors: number;
      details?: { artist_name: string; danbooru_id?: number; action: string;
                  creator_id?: string; post_count?: number; error?: string }[];
    }>("/api/v1/reference/danbooru/favorites/sync", { method: "POST" }),

  getBatchImportStatus: (jobId?: string) =>
    request<{
      status: string; progress: { current: number; total: number; imported: number; errors: number } | null;
      result: {
        total: number; imported_count: number; low_confidence_count: number;
        not_found_count: number; error_count: number;
        imported: { pixiv_id: string; creator_id: string; artist_name: string;
                    artist_id: number; links_imported: number; sources_created: number;
                    downloadable_urls: string[]; merged?: boolean }[];
        low_confidence: { pixiv_id: string; artist_name: string; artist_id: number;
                          url_count: number; message: string }[];
        not_found: { pixiv_id: string; message: string }[];
        errors: { pixiv_id: string; error: string }[];
      } | null;
      job_status: string | null;
    }>(`/api/v1/reference/danbooru/artist/batch-import/status${jobId ? `?job_id=${jobId}` : ""}`),

  // gallery-dl Config
  getGalleryDLConfig: (source?: string) => request<GalleryDLMultiConfig>(`/api/v1/admin/gallerydl-config${source ? `?source=${source}` : ""}`),
  getEffectiveGalleryDLConfig: (source: string, subscriptionSourceId?: string) => {
    const q = new URLSearchParams({ source });
    if (subscriptionSourceId) q.set("subscription_source_id", subscriptionSourceId);
    return request<{ source: string; extractor: string; source_url?: string | null; url_valid?: boolean | null; naming_template?: string | null; config: Record<string, unknown> }>(`/api/v1/admin/gallerydl-config/effective?${q.toString()}`);
  },

  updateGalleryDLConfig: (data: { pixiv?: Partial<PixivSourceConfig>; twitter?: Partial<TwitterSourceConfig>; iwara?: Partial<IwaraSourceConfig>; danbooru?: Partial<DanbooruSourceConfig>; pinterest?: Partial<PinterestSourceConfig>; lofter?: Partial<LofterSourceConfig>; weibo?: Partial<WeiboSourceConfig>; bilibili?: Partial<BilibiliSourceConfig> }) =>
    request<{ status: string; message: string; path: string }>("/api/v1/admin/gallerydl-config", { method: "PUT", body: JSON.stringify(data) }),

  testGalleryDLConnection: (source: string) =>
    request<{ source: string; success: boolean; message: string; details: string }>("/api/v1/admin/gallerydl-config/test-connection", { method: "POST", body: JSON.stringify({ source }) }),

  // Naming Templates
  listNamingTemplates: () => request<{ id: string; name: string; source?: string; template: string; is_default: boolean }[]>("/api/v1/admin/naming-templates"),

  createNamingTemplate: (data: { name: string; source?: string; template: string; is_default?: boolean }) =>
    request<Record<string, unknown>>("/api/v1/admin/naming-templates", { method: "POST", body: JSON.stringify(data) }),

  updateNamingTemplate: (id: string, data: Record<string, unknown>) =>
    request<Record<string, unknown>>(`/api/v1/admin/naming-templates/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteNamingTemplate: (id: string) =>
    request<void>(`/api/v1/admin/naming-templates/${id}`, { method: "DELETE" }),

  // Backup & Restore
  createBackup: (contents?: string[]) =>
    request<{ status: string; filename: string; size_bytes: number; size_mb: number; contents: string[]; component_sizes: Record<string, number> }>(
      "/api/v1/admin/backup", { method: "POST", body: JSON.stringify({ contents: contents || ["database", "gallerydl-config", "app-config", "download-archives", "library-metadata"] }) }),

  listBackups: () =>
    request<{ backups: { filename: string; size_mb: number; created_at: string; contents: string[]; component_sizes?: Record<string, number>; version?: string }[] }>(
      "/api/v1/admin/backup/list"),

  estimateBackupSizes: () =>
    request<{ components: Record<string, number> }>("/api/v1/admin/backup/estimate"),

  restoreBackup: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return fetch("/api/v1/admin/backup/restore", { method: "POST", body: formData }).then(r => r.json()) as Promise<{ status: string; restored: string[]; errors: string[]; manifest: any }>;
  },

  deleteBackup: (filename: string) =>
    request<{ status: string; message: string }>(`/api/v1/admin/backup/${encodeURIComponent(filename)}`, { method: "DELETE" }),

  downloadBackup: (filename?: string) => {
    const params = filename ? `?filename=${encodeURIComponent(filename)}` : "";
    return `${BASE}/api/v1/admin/backup/download${params}`;
  },
};

// ── Query Key Factory ──

export const queryKeys = {
  health: ["health"] as const,
  workbench: ["system", "workbench"] as const,
  schedulerDecisions: ["system", "scheduler-decisions"] as const,
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

// ── Auth ──────────────────────────────────────────────────────────────────────

export interface AuthTokenResponse {
  access_token: string;
  token_type: string;
}

export interface AuthUser {
  id: number;
  username: string;
  display_name: string | null;
  is_active: boolean;
}

export async function authLogin(username: string, password: string): Promise<AuthTokenResponse> {
  return request<AuthTokenResponse>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function authMe(): Promise<AuthUser> {
  return request<AuthUser>("/api/v1/auth/me");
}

export async function authChangePassword(currentPassword: string, newPassword: string): Promise<void> {
  return request<void>("/api/v1/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}
