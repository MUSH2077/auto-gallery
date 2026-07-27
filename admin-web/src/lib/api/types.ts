import type { components } from "./types.generated";

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

export interface TaskRun {
  id: string;
  kind: "download" | "import" | "admin" | string;
  operation_type?: string | null;
  subject_type?: string | null;
  subject_id?: string | null;
  parent_task_id?: string | null;
  status: string;
  queue_name?: string | null;
  rq_job_id?: string | null;
  title?: string | null;
  source?: string | null;
  source_url?: string | null;
  progress_stage?: string | null;
  progress_current?: number | null;
  progress_total?: number | null;
  progress_data?: Record<string, any> | null;
  result_data?: Record<string, any> | null;
  error_log?: string | null;
  meta?: Record<string, any> | null;
  enqueued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  last_heartbeat_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  events?: {
    id: number;
    event_type: string;
    from_status?: string | null;
    to_status?: string | null;
    message?: string | null;
    payload?: Record<string, any> | null;
    created_at?: string | null;
  }[];
}

export interface TaskRunListResponse {
  total: number;
  items: TaskRun[];
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
  curation_state?: CurationState;
  created_at: string;
  updated_at: string;
}

export interface CreatorListResponse {
  items: Creator[];
  total: number;
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
  running_job_count?: number;
  failed_job_count?: number;
  latest_job_id?: string;
  latest_job_status?: string;
  latest_job_created_at?: string;
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

export interface RepositoryRecentJob {
  id: string;
  subscription_id: string;
  subscription_source_id?: string | null;
  source: string;
  source_url: string;
  status: string;
  retry_count: number;
  error_log_excerpt?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RepositoryRecentWork {
  id: string;
  title?: string | null;
  posted_at?: string | null;
  thumbnail_asset_id?: string | null;
  asset_count: number;
  is_nsfw: boolean;
  is_ai_generated: boolean;
  is_favorite: boolean;
  created_at?: string | null;
  source?: string | null;
  creator_name?: string | null;
  creator_id?: string | null;
}

export interface RepositoryDetailResponse {
  repository: CreatorRepository;
  creator: {
    id: string;
    name: string;
    display_name?: string | null;
    thumbnail_url?: string | null;
    is_favorite: boolean;
  };
  subscription: {
    id: string;
    name?: string | null;
    is_active: boolean;
    sync_enabled: boolean;
    sync_interval_hours: number;
    schedule_mode?: string | null;
    scheduled_times?: string | null;
    last_synced_at?: string | null;
  };
  provider: {
    source: string;
    display_name: string;
    normalized_url?: string | null;
    url_valid: boolean;
    capabilities: ProviderInfo["capabilities"];
  };
  recent_jobs: RepositoryRecentJob[];
  recent_works: RepositoryRecentWork[];
}

export interface CurationState {
  visibility: "visible" | "trashed" | "purged" | "archived" | string;
  reason?: string | null;
  trashed_at?: string | null;
  purged_at?: string | null;
  archived_at?: string | null;
}

export interface CurationChange {
  id: string;
  commit_id: string;
  subject_type: string;
  subject_id: string;
  action: string;
  before_state?: Record<string, unknown> | null;
  after_state?: Record<string, unknown> | null;
  diff?: Record<string, { before?: unknown; after?: unknown }> | null;
  impact?: Record<string, unknown> | null;
  created_at: string;
}

export interface CurationCommit {
  id: string;
  parent_commit_id?: string | null;
  actor_type: string;
  actor_id?: string | null;
  message: string;
  trigger: string;
  dedupe_key?: string | null;
  occurred_at: string;
  reverts_commit_id?: string | null;
  status: "active" | "reverted" | "partial_reverted" | string;
  stats?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  is_baseline?: boolean;
  revertible?: boolean;
  created_at: string;
  updated_at: string;
  changes: CurationChange[];
}

export interface CurationCommitListResponse {
  items: CurationCommit[];
  total: number;
}

export interface CurationRevertResponse {
  status: string;
  commit?: CurationCommit | null;
  reverted: number;
  skipped: number;
  conflicts: Record<string, unknown>[];
}

export interface PurgePreviewResponse {
  work_count: number;
  asset_count: number;
  bytes_reclaimable: number;
  works: { id: string; title?: string | null; thumbnail_asset_id?: string | null }[];
  assets: { id: string; file_name: string; file_size: number }[];
}

export interface RuleSuggestion {
  id: string;
  title: string;
  description: string;
  confidence: number;
  impact: Record<string, unknown>;
}

export interface CurationBackfillStatus {
  is_complete: boolean;
  expected: Record<string, number>;
  existing: Record<string, number>;
  missing: Record<string, number>;
}

export interface CurationBackfillRunResponse {
  status: string;
  created: Record<string, number>;
  skipped: Record<string, number>;
  expected: Record<string, number>;
}

export interface RepositoryGraphNode {
  id: string;
  message: string;
  trigger: string;
  occurred_at: string;
  status: string;
  is_baseline: boolean;
  stats?: Record<string, unknown> | null;
  thumbnails: string[];
  changes_summary: { action: string; count: number }[];
}

export interface ImportJob {
  id: string;
  download_job_id: string;
  status: string;
  error_log?: string | null;
  created_at: string;
  updated_at?: string;
  priority: number;
  user_note?: string | null;
  operator_name?: string | null;
  operator_action?: string | null;
  import_retry_count: number;
  max_import_retries: number;
  progress_stage?: string | null;
  progress_works_done?: number | null;
  progress_works_total?: number | null;
  progress_data?: JobProgress | null;
  // Parent download-job context (resolved server-side, aligns with download rows)
  source?: string | null;
  source_url?: string | null;
  subscription_id?: string | null;
  subscription_name?: string | null;
  creator_id?: string | null;
  creator_name?: string | null;
}

export interface RepositoryGraphEdge {
  from_id: string;
  to_id: string;
}

export interface RepositoryGraphResponse {
  repository_id: string;
  nodes: RepositoryGraphNode[];
  edges: RepositoryGraphEdge[];
  total: number;
  offset: number;
  limit: number;
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
    disk_total_bytes: number;
    disk_free_bytes: number;
    disk_used_bytes: number;
    disk_used_percent?: number | null;
    disk_free_percent?: number | null;
    risk_level: "ok" | "warning" | "critical" | "unknown" | string;
  };
  proxy_health?: {
    sources: Record<string, { status: string; last_check: string; warnings: string }>;
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

export interface QueueBreakdown {
  queued: number;
  scheduled: number;
  started: number;
  failed: number;
}

export interface QueueStatsResponse {
  default_queue: number;
  scheduled_queue: number;
  failed_jobs: number;
  started_jobs?: number;
  scheduler_enabled?: boolean;
  scheduler_mode?: string;
  scheduler_timezone?: string;
  scheduled_times?: string;
  scheduler_scan_interval_minutes?: number;
  next_sync_scan_at?: string | null;
  queues?: Record<"default" | "downloads" | "imports" | "scheduled", QueueBreakdown>;
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
  priority?: number;
  user_note?: string | null;
  operator_name?: string | null;
  operator_action?: string | null;
  last_heartbeat_at?: string | null;
  worker_pid?: number | null;
  pipeline_stage?: string | null;
  progress_data?: JobProgress | null;
}

export interface JobProgress {
  stage?: string;
  current?: number;
  total?: number;
  percent?: number;
  message?: string;
  assets?: number;
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
  curation_visibility?: string;
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
  creator_id?: string | null;
  creator_name?: string | null;
  curation_state?: CurationState;
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

export interface RepositoryTagsResponse {
  items: Tag[];
  total: number;
}

export interface StorageRepositoryNode {
  repository_id?: string | null;
  source: string;
  source_display_name: string;
  disk_source: string;
  directory_name: string;
  size_mb: number;
  logical_size_mb?: number;
  work_count: number;
}

export interface CreatorStorageNode {
  creator_id: string;
  display_name: string;
  size_mb: number;
  work_count: number;
  repository_count: number;
  repositories: StorageRepositoryNode[];
}

export interface StorageBreakdownResponse {
  sources: Record<
    string,
    {
      size_mb: number;
      logical_size_mb?: number;
      creator_count: number;
      work_count: number;
    }
  >;
  creators: {
    name: string;
    display_name: string;
    source: string;
    size_mb: number;
    work_count: number;
    creator_id?: string;
    repository_id?: string;
  }[];
  creator_tree: CreatorStorageNode[];
  unlinked_repositories: StorageRepositoryNode[];
  db_stats?: Record<string, number>;
  layers?: Record<string, { path: string; size_mb: number; description: string }>;
}

export interface CreatorRef {
  creator_id: string;
  creator_name: string;
  work_count: number;
}

export interface TagDetail {
  id: string;
  normalized_name: string;
  category?: string;
  usage_count: number;
  top_creators: CreatorRef[];
  created_at: string;
}

export interface CreatorSearchHit {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  is_active: boolean;
  created_at: string;
}

export interface TagSearchHit {
  id: string;
  normalized_name: string;
  category?: string;
  created_at: string;
}

export interface DedupSettings {
  auto_group_enabled: boolean;
  phash_threshold: number;
  ssim_threshold: number;
  aspect_ratio_tolerance: number;
  auto_group_score: number;
  review_score: number;
  quarantine_days: number;
}

export interface AssetDedupAsset {
  id: string;
  file_name: string;
  file_size?: number | null;
  mime_type?: string | null;
  width?: number | null;
  height?: number | null;
  sha256?: string | null;
  phash?: string | null;
  source?: string | null;
  source_work_id?: string | null;
  source_url?: string | null;
  work_id?: string | null;
  work_title?: string | null;
  creator_id?: string | null;
  creator_name?: string | null;
  posted_at?: string | null;
  thumb_url: string;
  preview_url: string;
  group_id?: string | null;
  is_representative: boolean;
}

export interface AssetDedupEvidence {
  id: string;
  algorithm_version: string;
  sha256_equal: boolean;
  phash_distance?: number | null;
  ssim_score?: number | null;
  aspect_ratio_delta?: number | null;
  visual_score: number;
  metadata_score: number;
  total_score: number;
  hard_gate_passed: boolean;
  facts: {
    metadata?: {
      same_canonical_creator?: boolean;
      min_posted_delta_hours?: number | null;
      left_sources?: string[];
      right_sources?: string[];
    };
    thresholds?: Record<string, number>;
  };
}

export interface AssetDedupCase {
  id: string;
  status: "pending" | "merged" | "separate" | "deferred";
  revision: number;
  left: AssetDedupAsset;
  right: AssetDedupAsset;
  evidence: AssetDedupEvidence;
  suggested_representative_asset_id?: string | null;
  created_at: string;
  decided_at?: string | null;
  decided_by?: string | null;
  decision_reason?: string | null;
}

export interface AssetDedupCasePage {
  items: AssetDedupCase[];
  total: number;
  offset: number;
  limit: number;
}

export interface AssetDedupDecision {
  decision_id: string;
  case_id: string;
  action: string;
  status: string;
  revision: number;
  representative_asset_id?: string | null;
  group_id?: string | null;
  storage_actions: number;
  bytes_reclaimable: number;
  curation_commit_id?: string | null;
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
  stall_timeout_seconds: number;
  max_retries: number;
  retry_backoff_base_seconds: number;
  max_posts: number;
  skip_ai_generated: boolean;
  gallerydl_retries: number;
  gallerydl_timeout: number;
  gallerydl_abort: number;
  download_concurrency: number;
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

// Gitllery (on-disk curation history projection)
export interface GitlleryRepoStatus {
  repository_id: string;
  source: string;
  creator_dir: string;
  exists: boolean;
  behind: number;
  object_integrity_ok: boolean;
  drift: string[];
  clean: boolean;
}

export interface GitlleryStatus {
  repositories: GitlleryRepoStatus[];
  missing_repos: number;
  behind_total: number;
  // Checkpoint absent — pending counts unknown until the queued sync job runs.
  needs_reconcile?: boolean;
}

// reconcile/backfill are queued operations now.
export interface GitlleryReconcileResponse {
  status: string;
  job_id: string;
}

export interface GitlleryLogEntry {
  commit: string;
  db_commit_id?: string | null;
  message: string;
  trigger?: string | null;
  actor?: string | null;
  occurred_at?: string | null;
  change_count: number;
}

export interface GitlleryLogResponse {
  repository_id: string;
  entries: GitlleryLogEntry[];
  total: number;
}

export interface GitlleryRebuildReport {
  commits_restored: number;
  commits_skipped_auto: number;
  commits_deduped: number;
  changes_unmapped: number;
  states_applied: number;
  dry_run: boolean;
  job_id?: string | null;
  status?: string | null;
}

// ── Users (multi-user management) ──

export interface UserAccount {
  id: number;
  username: string;
  display_name?: string | null;
  is_admin: boolean;
  is_active: boolean;
  permissions: string[];
  nsfw_visible: boolean;
  upload_quota_bytes: number | null;
  upload_used_bytes: number;
  must_change_password: boolean;
  last_login_at?: string | null;
  created_at: string;
}

export interface Me {
  id: number;
  username: string;
  display_name?: string | null;
  is_admin: boolean;
  is_active: boolean;
  permissions: string[];
  modules: Record<string, string>;
  preferences: Record<string, unknown>;
  nsfw_visible: boolean;
  upload_quota_bytes: number | null;
  upload_used_bytes: number;
  must_change_password: boolean;
}

// ── Manual Upload ──

export interface UploadResponse {
  work_id: string;
  download_job_id: string;
  import_job_id: string | null;
  used_bytes: number;
  quota_bytes: number | null;
}

export type GeneratedWork = components["schemas"]["WorkRead"];
export type GeneratedWorkList = components["schemas"]["WorkList"];
export type GeneratedAsset = components["schemas"]["AssetRead"];
export type GeneratedCreator = components["schemas"]["CreatorRead"];
export type GeneratedSubscription = components["schemas"]["SubscriptionRead"];

// ── Showcase ──

export interface ShowcaseItem {
  work_id: string;
  title: string | null;
  creator_name: string | null;
  source: string | null;
  asset_id: string;
  thumb_url: string;
  preview_url: string;
  width: number | null;
  height: number | null;
}

export interface ShowcaseSampleResponse {
  items: ShowcaseItem[];
}
