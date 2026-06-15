import { request } from "./client";
import { worksApi } from "./endpoints";
import type * as T from "./types";
export * from "./client";
export * from "./types";
export * from "./endpoints";

// ── API ──

export const api = {
  // System
  health: () => request<T.HealthResponse>("/api/v1/system/health"),

  workbench: () => request<T.WorkbenchSummary>("/api/v1/system/workbench"),

  schedulerDecisions: () => request<T.SchedulerDecisionsResponse>("/api/v1/system/scheduler-decisions"),

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

  queueStats: () => request<T.QueueStatsResponse>("/api/v1/system/queue-stats"),

  // Sources
  sources: () => request<{ sources: T.ProviderInfo[] }>("/api/v1/sources"),

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
    return request<T.CreatorListResponse>(`/api/v1/creators?${params.toString()}`);
  },

  getCreator: (id: string) => request<T.Creator>(`/api/v1/creators/${id}`),
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
    request<T.CreatorSubscriptionOverview>(`/api/v1/creators/${id}/subscription-overview`),

  createCreator: (data: { name: string; display_name?: string; description?: string; thumbnail_url?: string }) =>
    request<T.Creator>("/api/v1/creators", { method: "POST", body: JSON.stringify(data) }),

  updateCreator: (id: string, data: Record<string, unknown>) =>
    request<T.Creator>(`/api/v1/creators/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

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
    request<T.CreatorLink[]>(`/api/v1/creators/${creatorId}/links`),

  createCreatorLink: (creatorId: string, data: { url: string; link_type: string; source?: string; confidence?: number }) =>
    request<T.CreatorLink>(`/api/v1/creators/${creatorId}/links`, { method: "POST", body: JSON.stringify(data) }),

  updateCreatorLink: (creatorId: string, linkId: string, data: Record<string, unknown>) =>
    request<T.CreatorLink>(`/api/v1/creators/${creatorId}/links/${linkId}`, { method: "PATCH", body: JSON.stringify(data) }),

  listSourceCreators: (creatorId: string) =>
    request<T.SourceCreator[]>(`/api/v1/creators/${creatorId}/sources`),

  createSourceCreator: (creatorId: string, data: { source: string; source_creator_id: string; source_url?: string; display_name?: string }) =>
    request<T.SourceCreator>(`/api/v1/creators/${creatorId}/sources`, { method: "POST", body: JSON.stringify(data) }),

  toggleCreatorFavorite: (id: string) =>
    request<T.Creator>(`/api/v1/creators/${id}/favorite`, { method: "POST" }),

  curateCreator: (id: string, action: "archive" | "restore", reason?: string, message?: string) =>
    request<T.CurationCommit>(`/api/v1/creators/${id}/curation`, { method: "POST", body: JSON.stringify({ action, reason, message }) }),

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
    return request<T.Subscription[]>(`/api/v1/subscriptions?${params.toString()}`);
  },

  getSubscription: (id: string) => request<T.Subscription>(`/api/v1/subscriptions/${id}`),

  createSubscription: (data: { creator_id: string; name?: string; is_active?: boolean; sync_enabled?: boolean }) =>
    request<T.Subscription>("/api/v1/subscriptions", { method: "POST", body: JSON.stringify(data) }),

  updateSubscription: (id: string, data: Record<string, unknown>) =>
    request<T.Subscription>(`/api/v1/subscriptions/${id}`, { method: "PATCH", body: JSON.stringify(data) }),

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
    request<T.SubscriptionSource[]>(`/api/v1/subscriptions/${subId}/sources`),

  createSubscriptionSource: (subId: string, data: { source: string; source_creator_id?: string; source_url?: string; is_enabled?: boolean }) =>
    request<T.SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources`, { method: "POST", body: JSON.stringify(data) }),

  updateSubscriptionSource: (subId: string, ssId: string, data: Record<string, unknown>) =>
    request<T.SubscriptionSource>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "PATCH", body: JSON.stringify(data) }),

  deleteSubscriptionSource: (subId: string, ssId: string) =>
    request<void>(`/api/v1/subscriptions/${subId}/sources/${ssId}`, { method: "DELETE" }),

  // Download Jobs
  listDownloadJobs: (params?: { status?: string; source?: string; subscription_id?: string; subscription_source_id?: string; q?: string; sort_by?: string; sort_order?: string; offset?: number; limit?: number }) => {
      const q = new URLSearchParams();
      if (params?.status) q.set("status", params.status);
      if (params?.source) q.set("source", params.source);
      if (params?.subscription_id) q.set("subscription_id", params.subscription_id);
      if (params?.subscription_source_id) q.set("subscription_source_id", params.subscription_source_id);
      if (params?.q) q.set("q", params.q);
      if (params?.sort_by) q.set("sort_by", params.sort_by);
      if (params?.sort_order) q.set("sort_order", params.sort_order);
      q.set("offset", String(params?.offset || 0));
      q.set("limit", String(params?.limit || 50));
      return request<T.DownloadJob[]>(`/api/v1/download-jobs?${q.toString()}`);
    },

  getDownloadJob: (id: string) =>
    request<T.DownloadJob>(`/api/v1/download-jobs/${id}`),

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

  // Repositories
  getRepository: (id: string) =>
    request<T.RepositoryDetailResponse>(`/api/v1/repositories/${id}`),

  syncRepository: (id: string) =>
    request<{ status: string; message?: string; job_id?: string; reason?: string }>(`/api/v1/repositories/${id}/sync-now`, { method: "POST" }),

  getRepositoryCurationGraph: (id: string, offset = 0, limit = 100, params?: { trigger?: string; include_baseline?: boolean }) => {
    const q = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (params?.trigger) q.set("trigger", params.trigger);
    if (params?.include_baseline !== undefined) q.set("include_baseline", String(params.include_baseline));
    return request<T.RepositoryGraphResponse>(`/api/v1/repositories/${id}/curation-graph?${q.toString()}`);
  },

  // Curation
  listCurationCommits: (params?: { offset?: number; limit?: number; subject_type?: string; subject_id?: string; trigger?: string; include_baseline?: boolean }) => {
    const q = new URLSearchParams();
    q.set("offset", String(params?.offset || 0));
    q.set("limit", String(params?.limit || 50));
    if (params?.subject_type) q.set("subject_type", params.subject_type);
    if (params?.subject_id) q.set("subject_id", params.subject_id);
    if (params?.trigger) q.set("trigger", params.trigger);
    if (params?.include_baseline !== undefined) q.set("include_baseline", String(params.include_baseline));
    return request<T.CurationCommitListResponse>(`/api/v1/curation/commits?${q.toString()}`);
  },

  getCurationCommit: (id: string) =>
    request<T.CurationCommit>(`/api/v1/curation/commits/${id}`),

  revertCurationCommit: (id: string) =>
    request<T.CurationRevertResponse>(`/api/v1/curation/commits/${id}/revert`, { method: "POST" }),

  previewPurge: (workIds?: string[]) =>
    request<T.PurgePreviewResponse>("/api/v1/curation/purge/preview", { method: "POST", body: JSON.stringify({ work_ids: workIds }) }),

  purgeWorks: (workIds?: string[], message?: string) =>
    request<T.CurationCommit>("/api/v1/curation/purge", { method: "POST", body: JSON.stringify({ work_ids: workIds, message }) }),

  curationRuleSuggestions: () =>
    request<T.RuleSuggestion[]>("/api/v1/curation/rule-suggestions"),

  getCurationBackfillStatus: () =>
    request<T.CurationBackfillStatus>("/api/v1/curation/backfill/status"),

  runCurationBackfill: () =>
    request<T.CurationBackfillRunResponse>("/api/v1/curation/backfill", { method: "POST" }),

  // Works
  ...worksApi,

  // Tags
  listTags: (offset = 0, limit = 100, sortBy = "usage_count", sortOrder = "desc") =>
    request<T.Tag[]>(`/api/v1/tags?offset=${offset}&limit=${limit}&sort_by=${sortBy}&sort_order=${sortOrder}`),

  createTag: (data: { normalized_name: string; category?: string }) =>
    request<T.Tag>("/api/v1/tags", { method: "POST", body: JSON.stringify(data) }),

  updateTag: (id: string, data: { normalized_name?: string; category?: string }) =>
    request<T.Tag>(`/api/v1/tags/${id}`, { method: "PUT", body: JSON.stringify(data) }),

  deleteTag: (id: string) =>
    request<{ status: string }>(`/api/v1/tags/${id}`, { method: "DELETE" }),

  // Search
  search: (q: string, offset = 0, limit = 20) => request<{ results: T.SearchWorkResult[]; total: number; creators?: { id: string; name: string; display_name?: string }[]; tags?: { id: string; normalized_name: string; category?: string }[] }>(`/api/v1/search?q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`),

  // Import Jobs
  listImportJobs: (statusOrParams?: string | { status?: string; download_job_id?: string; q?: string; offset?: number; limit?: number }, offset = 0, limit = 50) => {
    const params = typeof statusOrParams === "string" ? { status: statusOrParams, offset, limit } : (statusOrParams || { offset, limit });
    const q = new URLSearchParams();
    q.set("offset", String(params.offset ?? offset));
    q.set("limit", String(params.limit ?? limit));
    if (params.status) q.set("status", params.status);
    if (params.download_job_id) q.set("download_job_id", params.download_job_id);
    if (params.q) q.set("q", params.q);
    return request<{ total: number; items: T.ImportJob[] }>(`/api/v1/import-jobs?${q.toString()}`);
  },

  getImportJob: (id: string) =>
    request<T.ImportJob>(`/api/v1/import-jobs/${id}`),

  scanImports: () => request<{ status: string; message: string }>("/api/v1/import-jobs/scan", { method: "POST" }),

  retryImportJob: (id: string) =>
    request<{ status: string; message: string }>(`/api/v1/import-jobs/${id}/retry`, { method: "POST" }),

  deleteDownloadJob: (id: string) =>
    request<{ status: string }>(`/api/v1/download-jobs/${id}`, { method: "DELETE" }),

  deleteImportJob: (id: string) =>
    request<{ status: string }>(`/api/v1/import-jobs/${id}`, { method: "DELETE" }),

  // Admin
  getAdminSettings: () => request<T.AdminSettings>("/api/v1/admin/settings"),

  updateAdminSettings: (data: {
    dedup?: Partial<T.DedupSettings>;
    subscription_defaults?: Partial<T.SubscriptionDefaults>;
    download_defaults?: Partial<T.DownloadDefaults>;
    proxy?: Partial<T.ProxySettings>;
  }) =>
    request<{ status: string; message: string }>("/api/v1/admin/settings", { method: "PUT", body: JSON.stringify(data) }),

  reindexSearch: () => request<{ status: string; message: string }>("/api/v1/admin/search/reindex", { method: "POST" }),

  getAuthStatus: () => request<T.AuthStatusResponse>("/api/v1/admin/auth-status"),

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
    db_stats?: Record<string, number>;
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

  previewBatchImport: (pixivIds: string[]) =>
    request<{
      total: number; unique_count: number; duplicates_removed: number;
      duplicate_ids: string[]; new_count: number;
      already_exists: { pixiv_id: string; creator_name: string; creator_id: string }[];
    }>(
      "/api/v1/reference/danbooru/artist/batch-import/preview",
      { method: "POST", body: JSON.stringify({ pixiv_ids: pixivIds }) }),

  batchImportDanbooru: (pixivIds: string[]) =>
    request<{
      status: string; message: string; job_id: string; total: number;
      duplicates_removed?: number;
      already_exists?: { pixiv_id: string; creator_name: string; creator_id: string }[];
    }>(
      "/api/v1/reference/danbooru/artist/batch-import",
      { method: "POST", body: JSON.stringify({ pixiv_ids: pixivIds }) }),

  previewUrlBatchImport: (urls: string[]) =>
    request<{
      total: number; unique_count: number; duplicates_removed: number;
      duplicate_urls: string[];
    }>(
      "/api/v1/reference/danbooru/url-batch-import/preview",
      { method: "POST", body: JSON.stringify({ urls }) }),

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
  getGalleryDLConfig: (source?: string) => request<T.GalleryDLMultiConfig>(`/api/v1/admin/gallerydl-config${source ? `?source=${source}` : ""}`),
  getEffectiveGalleryDLConfig: (source: string, subscriptionSourceId?: string) => {
    const q = new URLSearchParams({ source });
    if (subscriptionSourceId) q.set("subscription_source_id", subscriptionSourceId);
    return request<{ source: string; extractor: string; source_url?: string | null; url_valid?: boolean | null; naming_template?: string | null; config: Record<string, unknown> }>(`/api/v1/admin/gallerydl-config/effective?${q.toString()}`);
  },

  updateGalleryDLConfig: (data: { pixiv?: Partial<T.PixivSourceConfig>; twitter?: Partial<T.TwitterSourceConfig>; iwara?: Partial<T.IwaraSourceConfig>; danbooru?: Partial<T.DanbooruSourceConfig>; pinterest?: Partial<T.PinterestSourceConfig>; lofter?: Partial<T.LofterSourceConfig>; weibo?: Partial<T.WeiboSourceConfig>; bilibili?: Partial<T.BilibiliSourceConfig> }) =>
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
    return fetch("/api/v1/admin/backup/restore?confirm=DELETE-EVERYTHING", { method: "POST", body: formData }).then(r => r.json()) as Promise<{ status: string; restored: string[]; errors: string[]; manifest: any }>;
  },

  deleteBackup: (filename: string) =>
    request<{ status: string; message: string }>(`/api/v1/admin/backup/${encodeURIComponent(filename)}`, { method: "DELETE" }),

  downloadBackup: (filename?: string) => {
    const params = filename ? `?filename=${encodeURIComponent(filename)}` : "";
    return `/api/v1/admin/backup/download${params}`;
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
  repositories: {
    detail: (id: string) => ["repositories", id] as const,
    graph: (id: string, offset = 0, params?: unknown) => ["repositories", id, "curation-graph", offset, params || {}] as const,
  },
  curation: {
    all: ["curation"] as const,
    commits: (params?: unknown) => ["curation", "commits", params || {}] as const,
    subject: (type: string, id: string) => ["curation", "subject", type, id] as const,
    suggestions: ["curation", "rule-suggestions"] as const,
    backfillStatus: ["curation", "backfill", "status"] as const,
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
  must_change_password: boolean;
}

export interface AuthUser {
  id: number;
  username: string;
  display_name: string | null;
  is_active: boolean;
  must_change_password: boolean;
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

export async function authChangePassword(currentPassword: string, newPassword: string): Promise<AuthTokenResponse> {
  return request<AuthTokenResponse>("/api/v1/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}
