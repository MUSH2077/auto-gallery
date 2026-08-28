import type { components } from "./types.generated";

// ── Types ──

export interface HealthResponse {
  status: string;
  version: string;
  services: Record<string, string>;
  disk?: string;
  resource_pressure?: ResourcePressureSnapshot;
  business?: {
    queues?: Record<string, number>;
    jobs?: {
      downloads?: Record<string, number>;
      imports?: Record<string, number>;
      auth_unhealthy_sources?: number;
    };
    scheduler?: Record<string, string | null>;
    gallerydl?: Record<string, unknown>;
    outboxes?: Record<string, OutboxHealthSnapshot>;
  };
}

export interface OutboxHealthSnapshot {
  waiting?: number | null;
  processing?: number | null;
  failed?: number | null;
  oldest_age_seconds?: number | null;
}

export interface ResourceProfileBudget {
  allowed?: boolean;
  would_allow?: boolean;
  enforced?: boolean;
  reason?: string | null;
  memory_reservation_bytes?: number | null;
  slice_seconds?: number | null;
  work_units?: number | null;
  active_grants?: number | null;
  hard_gate_enforced?: boolean;
  soft_budget_enforced?: boolean;
  token_scope?: string | null;
  reservation_capacity_bytes?: number | null;
}

export interface ResourceBudgetSnapshot {
  algorithm?: string;
  governance_mode?: "shadow" | "enforce" | string;
  enforced_profiles?: string[];
  generation?: number | null;
  valid_for_seconds?: number | null;
  valid_until?: string | null;
  valid_until_epoch?: number | null;
  throughput_scale?: number | null;
  computed_throughput_scale?: number | null;
  effective_throughput_scale?: number | null;
  rollout_max_scale?: number | null;
  read_bytes_per_second?: number | null;
  write_bytes_per_second?: number | null;
  burst_seconds?: number | null;
  profile_aliases?: Record<string, string>;
  profile_grants?: Record<string, ResourceProfileBudget>;
  profiles?: Record<string, ResourceProfileBudget>;
  grants?: Record<string, number | ResourceProfileBudget>;
  active_leases?: Record<string, unknown> | unknown[];
  reservation?: {
    mode?: string;
    hard_memory_floor_bytes?: number | null;
    capacity_bytes?: number | null;
    active_leases?: Array<Record<string, unknown>>;
    active_count?: number | null;
    reserved_bytes?: number | null;
    network_active?: number | boolean | null;
    disk_active?: number | boolean | null;
    maintenance_active?: number | boolean | null;
    error?: string | null;
  };
}

export interface ResourcePressureSnapshot {
  status: "normal" | "warning" | "paused" | string;
  controller_mode?: "normal" | "constrained" | "critical" | string;
  controller?: {
    mode?: "normal" | "constrained" | "critical" | string;
    legacy_status?: "normal" | "warning" | "paused" | string;
    governance_mode?: "shadow" | "enforce" | string;
    enforced_profiles?: string[];
    algorithm?: string;
    generation?: number | null;
    computed_throughput_scale?: number | null;
    effective_throughput_scale?: number | null;
    rollout_max_scale?: number | null;
    hard_gate_active?: boolean;
    psi_feedback_only?: boolean;
    hard_limits?: Record<string, number | boolean | null>;
    device_calibration?: {
      memory_reserve_mode?: "auto" | "fixed" | string;
      memory_total_bytes?: number | null;
      memory_reserve_ratio?: number | null;
      memory_reserve_min_bytes?: number | null;
      memory_reserve_max_bytes?: number | null;
      memory_reserve_bytes?: number | null;
      grantable_memory_capacity_bytes?: number | null;
      warning_available_bytes?: number | null;
      resume_available_bytes?: number | null;
      source?: string;
    };
    recovery_conditions?: Record<string, string | number | boolean | null>;
  };
  project_cgroup_contribution?: {
    scope?: string;
    backend?: Record<string, unknown>;
    workers?: Record<string, unknown>;
  };
  reasons: string[];
  hard_reasons?: string[];
  soft_reasons?: string[];
  sampled_at?: string | null;
  memory?: {
    available_bytes?: number | null;
    total_bytes?: number | null;
    available_ratio?: number | null;
    available_change_bytes_per_second?: number | null;
  };
  swap?: {
    free_bytes?: number | null;
    total_bytes?: number | null;
    free_ratio?: number | null;
    in_bytes_per_second?: number | null;
    out_bytes_per_second?: number | null;
    activity_bytes_per_second?: number | null;
  };
  psi?: {
    memory_full_avg10?: number | null;
    io_full_avg10?: number | null;
    memory_full_avg60?: number | null;
    memory_full_avg300?: number | null;
    io_full_avg60?: number | null;
    io_full_avg300?: number | null;
    memory_soft_trigger?: number | null;
    io_soft_trigger?: number | null;
    feedback_only?: boolean;
  };
  budget?: ResourceBudgetSnapshot;
  grants?: Record<string, unknown>;
  active_leases?: Record<string, unknown> | unknown[];
  trends?: Record<string, unknown>;
  baseline?: Record<string, unknown>;
  foreground?: {
    p95_ms?: number | null;
    sample_count?: number | null;
    window_seconds?: number | null;
    soft_limit_ms?: number | null;
    feedback_only?: boolean;
  };
  cgroups?: Record<string, Record<string, number | string | null>>;
  cgroup?: Record<string, Record<string, number | string | null>>;
  cgroup_memory_events?: {
    max?: number | null;
    oom?: number | null;
    oom_kill?: number | null;
    max_delta?: number | null;
    oom_delta?: number | null;
    oom_kill_delta?: number | null;
    scope?: string;
  };
  redis?: {
    used_memory_bytes?: number | null;
    maxmemory_bytes?: number | null;
    usage_ratio?: number | null;
    writable?: boolean | null;
    rejected_writes?: number | null;
    oom_rejected_writes?: number | null;
    application_rejected_enqueues?: number | null;
  };
  queues?: Record<string, number>;
  queue_activity?: Record<string, {
    queued?: number | null;
    scheduled?: number | null;
    deferred?: number | null;
    waiting?: number | null;
    running?: number | null;
  }>;
  workers?: {
    cgroup_contribution?: Record<string, unknown>;
    resource_leases?: Record<string, unknown>;
    [key: string]: unknown;
  };
  download_concurrency?: {
    configured: number;
    cap: number;
    effective: number;
    desired_effective?: number;
    restart_required?: boolean;
  };
}

export interface TaskRun {
  id: string;
  kind: "download" | "import" | "admin" | string;
  operation_type?: string | null;
  subject_type?: string | null;
  subject_id?: string | null;
  parent_task_id?: string | null;
  triggering_user_subscription_id?: string | null;
  triggering_remote_account_id?: string | null;
  status: string;
  resource_state?: "running" | "waiting" | "yielded" | string | null;
  resource_reason?: string | null;
  attention_state?: "none" | "open" | "resolved" | "acknowledged" | string;
  reason_code?: string | null;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
  compactable_at?: string | null;
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

export interface SyncOutcome {
  code: "new_content" | "no_changes" | "no_content";
  metadata_count: number;
  media_count: number;
  completed_at: string;
}

export interface TaskRunListResponse {
  total: number;
  items: TaskRun[];
}

export interface AdminOperationAccepted {
  task_id: string;
  job_id: string;
  status: "enqueued";
  operation_type: string;
}

export interface AdminOperationStatus<TResult = Record<string, unknown>> {
  task_id: string;
  job_id: string;
  rq_job_id?: string | null;
  status: "enqueued" | "running" | "recovering" | "paused" | "complete" | "failed" | "stale" | "cancelled" | string;
  operation_type: string;
  progress?: {
    phase?: string;
    label?: string;
    current?: number;
    total?: number;
    percent?: number;
  } | null;
  result?: TResult | null;
  error?: string | null;
  reason_code?: string | null;
  updated_at?: number | string | null;
}

export interface AdminOperationSnapshot<TResult = Record<string, unknown>> {
  task_id: string;
  job_id?: string | null;
  status: "complete";
  operation_type: string;
  progress?: AdminOperationStatus<TResult>["progress"];
  result: TResult;
  completed_at: string;
}

export interface AdminOperationCurrent {
  task_id: string;
  job_id?: string | null;
  status: "enqueued" | "running" | "recovering" | "paused";
  operation_type: string;
  progress?: AdminOperationStatus["progress"];
}

export interface AdminOperationSnapshotResponse<TResult = Record<string, unknown>> {
  snapshot: AdminOperationSnapshot<TResult> | null;
  current?: AdminOperationCurrent | null;
}

export interface RestoreUploadSession {
  upload_id: string;
  upload_token?: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  chunk_size: number;
  total_chunks: number;
  received_chunks: number;
  received_bytes: number;
  next_chunk: number;
  state: "uploading" | "uploaded" | "validating" | "validation_failed" | "ready" | string;
  validation_task_id?: string | null;
  request_id?: string | null;
  created_at: string;
  updated_at: string;
}

export interface RestoreValidationResult {
  state: "ready";
  request_id: string;
  host_command: string;
  manifest: {
    version: string;
    contents: string[];
    entries?: Record<string, { size: number; sha256: string }>;
  };
  message: string;
}

export interface RestoreReceipt {
  request_id: string;
  status: "pending" | "success" | "rolled_back" | "failed" | string;
  phase: string;
  started_at?: string;
  completed_at?: string;
  rollback_performed?: boolean;
  rollback_status?: string;
  rollback_components?: Record<string, { status: string; error?: string }>;
  diagnostic?: string;
  error?: string;
  rollback_command?: string;
}

export type DownloadConflictWinner = "canonical" | "staged";

export interface DownloadConflictEvidence {
  auto_eligible: boolean;
  recommended_winner: DownloadConflictWinner;
  checks: Record<"source" | "repository" | "work" | "creator" | "page" | "source_asset", boolean>;
  database_identity?: Record<string, unknown> | null;
  canonical_identity?: Record<string, unknown> | null;
  staged_identity?: Record<string, unknown> | null;
}

export interface DownloadConflictItem {
  relative_path: string;
  file_type: "metadata" | "media" | string;
  mime_type: string;
  canonical_size?: number | null;
  staged_size?: number | null;
  canonical_sha256?: string | null;
  staged_sha256?: string | null;
  evidence: DownloadConflictEvidence;
}

export interface DownloadConflictCase {
  task_id: string;
  download_job_id: string;
  source: string;
  source_url: string;
  status: string;
  reason_code?: string | null;
  resolution?: {
    resolution_id: string;
    state: string;
    expires_at: string;
  } | null;
  all_auto_eligible: boolean;
  items: DownloadConflictItem[];
}

export interface DownloadConflictResolution {
  resolution_id: string;
  task_id: string;
  download_job_id: string;
  state: string;
  automatic: boolean;
  resolved_at: string;
  expires_at: string;
  retry?: { status: string; message?: string };
}

export type OperationsView = "attention" | "active" | "resolved";
export type ClearEntity = "works" | "creators" | "subscriptions" | "tags" | "jobs" | "settings" | "all";

export interface ClearImpactPreview {
  entity: ClearEntity;
  confirmation_phrase: string;
  counts: Record<string, number>;
  preserves_repository_sync_receipts: boolean;
  deletes_media_files: boolean;
}

export interface OperationAttentionItem {
  id: string;
  type: string;
  severity: "critical" | "warning" | string;
  status: string;
  reason_code?: string | null;
  title: string;
  summary?: string | null;
  repository_id?: string | null;
  task_id?: string | null;
  occurred_at: string;
  source?: string | null;
  available_actions: string[];
  task?: TaskRun | null;
}

export interface OperationsOverviewResponse {
  view: OperationsView;
  total: number;
  summary: {
    attention: number;
    critical: number;
    warning: number;
    resolved: number;
    active: number;
    resource_limited: number;
  };
  items: OperationAttentionItem[];
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
    supports_download_cursor?: boolean;
    supports_remote_discovery?: boolean;
    discovery_auth_methods?: RemoteAuthMethod[];
    supports_collection_selectors?: boolean;
  };
}

export type RemoteDiscoverySource = "pixiv" | "x" | "bilibili";
export type RemoteAuthMethod = "refresh_token" | "oauth2" | "cookie" | "sessdata";
export type DiscoveryConfidence = "high" | "medium" | "low";
export type DiscoveryCandidateState = "pending" | "dismissed" | "imported" | "conflict";

export interface RemoteCollection {
  id: string;
  name: string;
  selector: Record<string, unknown>;
}

export interface RemoteAccountRead {
  id: string;
  user_id: number;
  source: RemoteDiscoverySource;
  remote_user_id?: string | null;
  remote_username?: string | null;
  auth_method?: RemoteAuthMethod | null;
  scopes: string[];
  collection_selectors: Record<string, unknown>[];
  is_enabled: boolean;
  auth_status?: string | null;
  auth_error_reason?: string | null;
  last_authenticated_at?: string | null;
  last_scan_started_at?: string | null;
  last_scan_completed_at?: string | null;
  next_scan_at?: string | null;
  scan_interval_hours: number;
  auto_import_enabled: boolean;
  auto_import_min_confidence: DiscoveryConfidence;
  auto_import_limit: number;
  has_credentials: boolean;
  credential_mask: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface RemoteAccountCreateInput {
  source: RemoteDiscoverySource;
  remote_user_id?: string;
  remote_username?: string;
  auth_method: RemoteAuthMethod;
  scopes?: string[];
  collection_selectors?: Record<string, unknown>[];
  is_enabled?: boolean;
  scan_interval_hours?: number;
  auto_import_enabled?: boolean;
  auto_import_min_confidence?: DiscoveryConfidence;
  auto_import_limit?: number;
  credentials: Record<string, string>;
}

export type RemoteAccountUpdateInput = Partial<Omit<RemoteAccountCreateInput, "source">>;

export interface DiscoveryCandidate {
  id: string;
  remote_account_id: string;
  user_id: number;
  source_creator_id: string;
  remote_url?: string | null;
  display_name?: string | null;
  metadata?: Record<string, unknown> | null;
  confidence: DiscoveryConfidence;
  confidence_reasons?: Array<string | Record<string, unknown>> | null;
  state: DiscoveryCandidateState;
  subscription_id?: string | null;
  user_subscription_id?: string | null;
  dismissed_at?: string | null;
  imported_at?: string | null;
  last_seen_at?: string | null;
  is_following: boolean;
  created_at: string;
  updated_at: string;
}

export interface DiscoveryCandidateListResponse {
  total: number;
  items: DiscoveryCandidate[];
}

export interface DiscoveryCandidateFilters {
  accountId?: string;
  state?: DiscoveryCandidateState;
  confidence?: DiscoveryConfidence;
  isFollowing?: boolean;
  offset?: number;
  limit?: number;
}

export interface DiscoveryCandidateBatchInput {
  ids: string[];
  action: "import" | "dismiss" | "restore";
  syncNow?: boolean;
}

export interface DiscoveryCandidateBatchResponse {
  items: DiscoveryCandidate[];
  immediate_sync: boolean;
  sync_results: unknown[];
}

export interface DiscoveryCandidateResolveInput {
  creatorId?: string;
  creatorName?: string;
  syncNow?: boolean;
}

export interface DiscoveryCandidateResolveResponse {
  candidate: DiscoveryCandidate;
  immediate_sync: boolean;
  sync_result?: unknown | null;
}

export interface XOAuthAuthorizeResponse {
  authorization_url: string;
  state: string;
  expires_in: number;
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

export type DeletionEntityType = "repository" | "subscription" | "creator";

export interface DeletionPreview {
  entity_type: DeletionEntityType;
  entity_ids: string[];
  mode: "soft" | "permanent";
  can_delete_files: boolean;
  active_task_count: number;
  active_job_count: number;
  active_task_ids: string[];
  affected_work_count: number;
  exclusive_work_count: number;
  shared_work_count: number;
  exclusive_asset_count: number;
}

export interface DeletionResult {
  status: "soft_deleted" | "enqueued";
  mode: "soft" | "permanent";
  entity_type: DeletionEntityType;
  entity_ids: string[];
  delete_files: boolean;
  task_id?: string | null;
  message?: string | null;
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
  schedule_mode?: "inherit" | "interval" | "calendar" | "manual" | null;
  schedule_rule?: CalendarScheduleRule | null;
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
  configured_mode?: string | null;
  effective_mode?: string | null;
  auto_enabled_source?: {
    id: string;
    source: string;
    source_url?: string | null;
    selection_reason: string;
  } | null;
  next_sync_at?: string | null;
}

export type SubscriptionLatestStateKind =
  | "active"
  | "attention"
  | "success"
  | "never_synced"
  | "manual"
  | "disabled";

export interface SubscriptionLatestState {
  state: SubscriptionLatestStateKind;
  status?: string | null;
  occurred_at?: string | null;
  outcome_code?: string | null;
  reason_code?: string | null;
  repository_id?: string | null;
  task_id?: string | null;
}

export interface SubscriptionScheduleSummary {
  configured_mode: string;
  effective_mode: string;
  inherited: boolean;
  timezone: string;
  scheduled_times?: string | null;
  schedule_rule?: CalendarScheduleRule | null;
  sync_interval_hours: number;
  next_due_at?: string | null;
  oldest_due_at?: string | null;
  due_sources: number;
  overdue_sources: number;
  blocked_sources: number;
}

export interface SubscriptionSummary {
  subscription_id: string;
  latest_state: SubscriptionLatestState;
  active_count: number;
  attention_count: number;
  source_count: number;
  enabled_source_count: number;
  schedule: SubscriptionScheduleSummary;
}

export interface SubscriptionSummariesResponse {
  items: SubscriptionSummary[];
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

export type RepositoryLatestJob = components["schemas"]["RepositoryRecentJob"];
export type CreatorRepository = components["schemas"]["RepositoryRead"];
export type RepositoryRecentJob = components["schemas"]["RepositoryRecentJob"];
export type RepositoryRecentWork = components["schemas"]["RepositoryRecentWork"];

export type RepositoryDetailResponse = components["schemas"]["RepositoryDetailResponse"];

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
    loop?: SchedulerLoopState | null;
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
      creator_id?: string | null;
      creator_name?: string | null;
      subscription_name?: string | null;
      status: string;
      pipeline_stage?: string | null;
      progress_data?: JobProgress | null;
      outcome?: SyncOutcome | null;
      created_at?: string | null;
      updated_at?: string | null;
      error_log_excerpt?: string | null;
    }[];
    import_jobs: {
      id: string;
      download_job_id: string;
      source?: string | null;
      source_url?: string | null;
      subscription_id?: string | null;
      subscription_name?: string | null;
      creator_id?: string | null;
      creator_name?: string | null;
      status: string;
      progress_stage?: string | null;
      progress_works_done?: number | null;
      progress_works_total?: number | null;
      progress_data?: JobProgress | null;
      created_at?: string | null;
      updated_at?: string | null;
      error_log_excerpt?: string | null;
    }[];
    works: {
      id: string;
      title?: string | null;
      thumbnail_asset_id?: string | null;
      has_video?: boolean;
      source?: string | null;
      creator_name?: string | null;
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
  schedule_rule?: CalendarScheduleRule | null;
  scheduler_scan_interval_minutes?: number;
  next_sync_scan_at?: string | null;
  scheduler_loop?: SchedulerLoopState | null;
  queues?: Record<"default" | "downloads" | "imports" | "scheduled", QueueBreakdown>;
}

export interface SchedulerLoopState {
  status: "scheduled" | "running" | "recovering" | "stalled" | "unknown" | string;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  next_scan_at?: string | null;
  watchdog_at?: string | null;
  last_error?: string | null;
  scan_interval_minutes?: number;
  active?: { queued: number; scheduled: number; started: number };
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
  schedule_rule?: CalendarScheduleRule | null;
  sync_interval_hours: number;
  last_synced_at?: string | null;
  last_attempted_at?: string | null;
  due: boolean;
  decision: string;
  reason: string;
  suppression_reason?: string | null;
  next_due_at?: string | null;
  window_start?: string | null;
  window_end?: string | null;
  auth_healthy: boolean;
  url_valid: boolean;
  can_download: boolean;
  is_overdue?: boolean;
  is_attention?: boolean;
}

export interface SchedulerDecisionsResponse {
  updated_at: string;
  scheduler_enabled: boolean;
  suppressed_count?: number;
  timezone: string;
  view?: "attention" | "all";
  total?: number;
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
  outcome?: SyncOutcome | null;
  reason_code?: string | null;
  conflict_details?: Array<{
    relative_path: string;
    file_type: "metadata" | "media" | string;
    classification: string;
    staged_sha256?: string;
    canonical_sha256?: string;
  }>;
  retryable?: boolean;
}

export interface JobProgress {
  stage?: string;
  current?: number;
  total?: number;
  percent?: number;
  message?: string;
  assets?: number;
  outcome_code?: SyncOutcome["code"];
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
  has_video?: boolean;
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

export type Tag = components["schemas"]["TagRead"];

export type TagSourceUsage = components["schemas"]["TagSourceUsage"];

export interface RepositoryTagsResponse {
  items: Tag[];
  total: number;
}

export type StorageRepositoryNode = components["schemas"]["StorageRepositoryNode"];
export type CreatorStorageNode = components["schemas"]["CreatorStorageNode"];
export type DataCenterPipelineStats = components["schemas"]["DataCenterPipelineStats"];
export type SystemInfoResponse = components["schemas"]["SystemInfoResponse"];
export type StorageBreakdownResponse = components["schemas"]["StorageBreakdownResponse"];

export interface CreatorRef {
  creator_id: string;
  creator_name: string;
  work_count: number;
}

export type TagDetail = components["schemas"]["TagDetail"];

export interface CreatorSearchHit {
  id: string;
  name: string;
  display_name: string;
  description?: string;
  is_active: boolean;
  is_favorite?: boolean;
  danbooru_artist_id?: number | null;
  subscription_count?: number;
  source_count?: number;
  repository_count?: number;
  last_synced_at?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface TagSearchHit {
  id: string;
  normalized_name: string;
  category?: string;
  created_at: string;
}

export type SearchScope =
  | "global"
  | "works"
  | "creators"
  | "tags"
  | "repositories"
  | "subscriptions"
  | "tasks"
  | "scheduler"
  | "creator-picker";

export type SearchTarget =
  | "works"
  | "creators"
  | "tags"
  | "repositories"
  | "subscriptions"
  | "tasks"
  | "scheduler";

export interface SearchTextToken {
  kind: "text";
  value: string;
  quoted: boolean;
  start: number;
  end: number;
}

export interface SearchQualifierToken {
  kind: "qualifier";
  key: string;
  value: string;
  negated: boolean;
  quoted: boolean;
  start: number;
  end: number;
}

export type SearchToken = SearchTextToken | SearchQualifierToken;

export interface SearchDiagnostic {
  code: string;
  message: string;
  start: number;
  end: number;
  token: string;
  suggestions: string[];
}

export interface SearchSuggestion {
  kind: "qualifier" | "value" | "repair";
  label: string;
  description: string;
  qualifier_key?: string;
  help_id?: string;
  example?: string;
  query: string;
}

export interface SearchParsedQuery {
  raw: string;
  canonical: string;
  scope: SearchScope;
  targets: SearchTarget[];
  tokens: SearchToken[];
}

export interface RepositorySearchHit {
  id: string;
  name: string;
  source: string;
  source_creator_id?: string | null;
  source_url?: string | null;
  creator_id: string;
  creator_name: string;
  subscription_id: string;
  subscription_name?: string | null;
  is_enabled: boolean;
  auth_healthy: boolean;
  auth_status?: string | null;
  last_synced_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SubscriptionSearchHit {
  id: string;
  name: string;
  creator_id: string;
  creator_name: string;
  creator_display_name?: string | null;
  is_active: boolean;
  sync_enabled: boolean;
  sync_interval_hours: number;
  schedule_mode?: string | null;
  schedule_rule?: CalendarScheduleRule | null;
  scheduled_times?: string | null;
  last_synced_at?: string | null;
  source_count: number;
  enabled_source_count: number;
  running_job_count: number;
  failed_job_count: number;
  latest_job_id?: string | null;
  latest_job_status?: string | null;
  latest_job_created_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SearchGroups {
  works?: {
    total: number;
    items: SearchWorkResult[];
    next_cursor?: string | null;
    previous_cursor?: string | null;
  };
  creators?: { total: number; items: CreatorSearchHit[] };
  tags?: { total: number; items: (TagSearchHit & { usage_count?: number })[] };
  repositories?: { total: number; items: RepositorySearchHit[] };
  subscriptions?: { total: number; items: SubscriptionSearchHit[] };
  tasks?: { total: number; items: TaskRun[] };
  scheduler?: { total: number; items: SchedulerDecisionItem[] };
}

export interface SearchResponse {
  query: string;
  canonical_query: string;
  parsed: SearchParsedQuery;
  groups: SearchGroups;
  total: number;
  next_cursor?: string | null;
  previous_cursor?: string | null;
  results: SearchWorkResult[];
  creators: CreatorSearchHit[];
  tags: (TagSearchHit & { usage_count?: number })[];
  repositories: RepositorySearchHit[];
  subscriptions: SubscriptionSearchHit[];
  execution: {
    winner: "postgresql" | "meilisearch" | string;
    hedged: boolean;
    consistency: "authoritative" | "fulltext_index_required" | Record<string, unknown> | string;
    index_status: string;
    index_lag?: number | null;
    elapsed_ms: number;
  };
}

export interface SearchAssistResponse {
  query: string;
  canonical_query?: string | null;
  parsed?: SearchParsedQuery | null;
  diagnostics: SearchDiagnostic[];
  suggestions: SearchSuggestion[];
  catalog: {
    key: string;
    negatable: boolean;
    values: string[];
    help_id: string;
    example: string;
    description: string;
  }[];
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
      creator_bonus?: number;
      time_bonus?: number;
      left_sources?: string[];
      right_sources?: string[];
    };
    scope?: {
      eligible?: boolean;
      reason?: string;
      assets?: Record<
        string,
        {
          sources?: string[];
          work_ids?: string[];
          work_source_ids?: string[];
        }
      >;
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

export type CalendarScheduleRule =
  | { frequency: "daily"; times: string[] }
  | { frequency: "weekly"; weekdays: number[]; times: string[] }
  | { frequency: "monthly"; month_days: number[]; times: string[]; overflow?: "last_day" };

export interface SubscriptionDefaults {
  default_sync_interval_hours: number;
  scheduler_scan_interval_minutes: number;
  scheduler_enabled: boolean;
  schedule_mode: "interval" | "calendar";
  schedule_rule?: CalendarScheduleRule | null;
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
  auto_resolve_upstream_conflicts: boolean;
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
  product_version: "v1";
  format_id: "gitllery-segment";
  format_revision: 1;
  projection_mode?: "shadow" | "active" | null;
  head_segment?: string | null;
  last_complete_commit_id?: string | null;
}

export interface GitlleryStatus {
  repositories: GitlleryRepoStatus[];
  missing_repos: number;
  behind_total: number;
  // Checkpoint absent — pending counts unknown until the queued sync job runs.
  needs_reconcile?: boolean;
  product_version: "v1";
  format_id: "gitllery-segment";
  format_revision: 1;
  projection_mode: "shadow" | "active";
}

export interface GitlleryCapability {
  enabled: boolean;
  reason?: "gitllery_shadow_only" | "gitllery_transfer_not_implemented" | string | null;
}

export interface GitllerySettings {
  product_name: "Gitllery";
  product_version: "v1";
  format_id: "gitllery-segment";
  format_revision: 1;
  projection_mode: "shadow" | "active";
  build_generation: string;
  managed_by: "deployment_environment";
  read_only: true;
  capabilities: {
    automatic_projection: GitlleryCapability;
    reconcile: GitlleryCapability;
    backfill: GitlleryCapability;
    rebuild: GitlleryCapability;
    push: GitlleryCapability;
    pull: GitlleryCapability;
    verify: GitlleryCapability;
    commit: GitlleryCapability;
  };
  cli: {
    max_works_per_commit: 25;
    max_operations_per_commit: 100;
    token_storage: "client_only";
    server_stores_cli_token: false;
    examples: Record<"config" | "login" | "status" | "log" | "verify" | "commit", string>;
  };
  governance_scope: {
    observation: "host_and_auto_gallery";
    enforcement: "auto_gallery_only";
    modifies_other_projects: false;
    modifies_host_configuration: false;
  };
  status: GitlleryStatus;
}

// reconcile/backfill are queued operations now.
export interface GitlleryReconcileResponse {
  status: string;
  ready: number;
  enqueued: number;
  deferred: number;
  projection_scope?: "library";
  repository_id?: string | null;
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
  status: string;
  dry_run?: boolean;
  repository_id?: string | null;
  captured?: number;
  ready?: number;
  enqueued?: number;
  deferred?: number;
  projection_mode: "shadow" | "active";
  legacy_layout_will_remain_read_only?: boolean;
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
export type GeneratedCreator = components["schemas"]["CreatorRead"];
export type GeneratedSubscription = components["schemas"]["SubscriptionRead"];
export type ImportFromDiskRequest = Omit<
  components["schemas"]["ImportFromDiskRequest"],
  "reset_ledger"
> & {
  // FastAPI accepts an omitted field and applies its Pydantic default, while
  // openapi-typescript currently marks a defaulted boolean as required.
  reset_ledger?: components["schemas"]["ImportFromDiskRequest"]["reset_ledger"];
};
