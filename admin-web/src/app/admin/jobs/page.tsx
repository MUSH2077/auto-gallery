"use client";
import Link from "next/link";
import { Suspense, useState, useEffect, useMemo, useRef, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useToast } from "@/components/Toast";
import { useT, type TFunction } from "@/lib/i18n";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, JobProgress, queryKeys, TaskRun } from "@/lib/api";
import { PageHeader, PageShell, EmptyState, ErrorState, ConfirmDialog, SourceBadge, RealProgressBar, StatusBadge, StatCard, PermissionGuard } from "@/components";
import { TaskDetailDrawer, JobDetailDrawer, shortId } from "@/components/JobDrawers";
import { useJobWebSocket } from "@/lib/useWebSocket";
import { statusLabel, useI18nFormat } from "@/lib/i18n-format";
import { classifyJob, categoryBorderClass, estimatedRetryBackoff } from "@/lib/jobCategory";
import { POLL_ACTIVE_MS as REFETCH_ACTIVE_MS, POLL_IDLE_MS as REFETCH_IDLE_MS } from "@/lib/polling";
import { useStaggeredEntrance, type StaggeredEntranceProps } from "@/lib/motion";


const PAGE_LIMIT = 200;

const STATUS_OPTIONS = ["", "enqueued", "downloading", "paused", "downloaded", "importing", "complete", "failed", "stale", "cancelled"];
const IMPORT_STATUS_OPTIONS = ["", "enqueued", "running", "complete", "failed", "stale"];
const TASK_STATUS_OPTIONS = ["", "enqueued", "running", "paused", "recovering", "complete", "failed", "cancelled", "stale"];
const SOURCE_OPTIONS = ["", "pixiv", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili"];
type JobsTab = "all" | "downloads" | "imports" | "admin";
type BatchAction = "retry" | "pause" | "resume" | "cancel" | "delete";
const JOBS_TABS: { value: JobsTab; labelKey: string }[] = [
  { value: "all", labelKey: "jobs.tab_all" },
  { value: "downloads", labelKey: "jobs.tab_downloads" },
  { value: "imports", labelKey: "jobs.tab_imports" },
  { value: "admin", labelKey: "jobs.tab_admin" },
];
const BATCH_ACTIONS_BY_TAB: Record<JobsTab, BatchAction[]> = {
  all: ["retry", "pause", "resume", "cancel"],
  downloads: ["retry", "pause", "resume", "cancel", "delete"],
  imports: ["retry", "cancel", "delete"],
  admin: ["retry"],
};
const DOWNLOAD_BATCH_ALLOWED: Record<BatchAction, string[]> = {
  retry: ["failed", "stale", "downloading", "complete"],
  pause: ["enqueued", "downloading", "downloaded", "importing", "failed", "stale"],
  resume: ["paused"],
  cancel: ["enqueued", "downloading", "downloaded", "importing", "failed", "stale", "paused"],
  delete: ["enqueued", "downloading", "downloaded", "failed", "stale", "complete", "paused", "cancelled"],
};
const IMPORT_BATCH_ALLOWED: Record<BatchAction, string[]> = {
  retry: ["failed", "stale"],
  pause: [],
  resume: [],
  cancel: ["enqueued", "running", "failed", "stale"],
  delete: ["enqueued", "running", "complete", "failed", "stale", "cancelled"],
};

function isActiveDownload(status: string) {
  return ["pending", "enqueued", "downloading", "downloaded", "importing"].includes(status);
}

function isActiveImport(status: string) {
  return ["pending", "enqueued", "running"].includes(status);
}

function isActiveTask(status: string) {
  return ["enqueued", "running", "recovering"].includes(status);
}

function isAttentionStatus(status: string) {
  return ["enqueued", "running", "recovering", "downloading", "downloaded", "importing", "failed", "stale"].includes(status);
}

function fallbackProgress(stage: string): JobProgress {
  return { stage };
}

function Elapsed({ since, active }: { since: string; active: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, [active]);
  const seconds = Math.floor((now - new Date(since).getTime()) / 1000);
  if (seconds < 60) return <span className="text-xs text-accent font-mono">{seconds}s</span>;
  if (seconds < 3600) return <span className="text-xs text-accent font-mono">{Math.floor(seconds / 60)}m {seconds % 60}s</span>;
  return <span className="text-xs text-accent font-mono">{Math.floor(seconds / 3600)}h {Math.floor((seconds % 3600) / 60)}m</span>;
}

// ProgressBar replaced by RealProgressBar from components — data-driven with actual percent/current/total

function JobLifecycle({ status }: { status: string }) {
  const t = useT();
  const steps = ["created", "downloading", "downloaded", "importing", status === "failed" || status === "stale" ? status : "complete"];
  const activeIndex = status === "pending" ? 0
    : status === "downloading" ? 1
      : status === "downloaded" ? 2
        : status === "importing" ? 3
          : status === "complete" ? 4
            : status === "failed" || status === "stale" ? 4
              : 0;
  const failed = status === "failed" || status === "stale";
  return (
    <div className="hidden min-w-[240px] items-center gap-1 lg:flex">
      {steps.map((step, index) => {
        const done = index < activeIndex || (index === activeIndex && status === "complete");
        const active = index === activeIndex && status !== "complete";
        const danger = failed && index === activeIndex;
        return (
          <div key={`${step}-${index}`} className="flex min-w-0 flex-1 items-center gap-1">
            <span
              title={t(`jobs.lifecycle_${step}`, step)}
              className={`h-2 w-2 shrink-0 rounded-full transition-colors duration-slow ${
                danger ? "bg-danger" : active ? "animate-pulse bg-accent" : done ? "bg-success" : "bg-border dark:bg-border"
              }`}
            />
            {index < steps.length - 1 && <span className={`h-px flex-1 transition-colors duration-slow ${done ? "bg-success" : "bg-border dark:bg-border"}`} />}
          </div>
        );
      })}
    </div>
  );
}

function ErrorExcerpt({ value }: { value?: string | null }) {
  if (!value) return null;
  const firstLine = value.split("\n").find(Boolean) || value;
  const isNoNewContent = /no new content since last sync|already archived or empty/i.test(firstLine);
  const className = isNoNewContent
    ? "line-clamp-1 rounded-md border border-border bg-subtle px-2 py-1 text-xs text-muted dark:border-border dark:bg-subtle dark:text-muted"
    : "line-clamp-1 rounded-md border border-danger/20 bg-danger-subtle px-2 py-1 text-xs text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger";
  return (
    <div className={className}>
      {firstLine.slice(0, 180)}
    </div>
  );
}

function operationLabel(t: TFunction, operationType?: string | null, kind?: string | null) {
  if (operationType === "admin-rebuild") return t("jobs.op_admin_rebuild");
  if (operationType === "admin-disk-import") return t("jobs.op_admin_disk_import");
  if (operationType === "admin-clear") return t("jobs.op_admin_clear");
  if (operationType === "subscription-sync-batch") return t("jobs.op_subscription_sync_batch");
  if (operationType === "library_rebuild") return t("jobs.op_library_rebuild");
  if (kind === "download") return t("jobs.download");
  if (kind === "import") return t("jobs.import");
  return operationType || kind || "—";
}

function JobsTabBar({
  activeTab,
  onChange,
}: {
  activeTab: JobsTab;
  onChange: (tab: JobsTab) => void;
}) {
  const t = useT();
  return (
    <div className="inline-flex gap-1 rounded-md bg-subtle p-1 dark:bg-subtle" role="tablist" aria-label={t("jobs.tabs_label")}>
      {JOBS_TABS.map((tab) => {
        const active = activeTab === tab.value;
        return (
          <button
            key={tab.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.value)}
            className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
              active ? "bg-surface text-fg shadow-sm dark:bg-border" : "text-muted hover:bg-surface/60 hover:text-fg"
            }`}
          >
            {t(tab.labelKey)}
          </button>
        );
      })}
    </div>
  );
}

function RowMeta({
  primary,
  secondary,
}: {
  primary: ReactNode;
  secondary?: ReactNode;
}) {
  return (
    <div className="min-w-0 leading-tight">
      <div className="truncate text-xs font-medium text-fg">{primary || "—"}</div>
      {secondary && <div className="mt-0.5 truncate text-[11px] text-muted">{secondary}</div>}
    </div>
  );
}

function RowActions({ children }: { children?: ReactNode }) {
  if (!children) return <div className="min-w-0" />;
  return (
    <div className="flex min-w-0 flex-nowrap items-center justify-end gap-1 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
      {children}
    </div>
  );
}

function RowButton({
  children,
  tone = "ghost",
  onClick,
  disabled,
}: {
  children: ReactNode;
  tone?: "ghost" | "primary" | "danger";
  onClick?: () => void;
  disabled?: boolean;
}) {
  const toneClass = tone === "primary"
    ? "border-primary bg-primary text-on-primary hover:bg-primary-hover"
    : tone === "danger"
      ? "border-danger/40 bg-surface text-danger hover:bg-danger/10"
      : "border-border bg-surface text-fg hover:bg-subtle";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex h-8 min-w-[46px] items-center justify-center rounded-md border px-2 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50 ${toneClass}`}
    >
      {children}
    </button>
  );
}

function JobRowShell({
  select,
  status,
  typeLabel,
  id,
  source,
  primary,
  secondary,
  detail,
  progress,
  activeSince,
  timestamp,
  actions,
  error,
  result,
  className = "",
  entrance,
  onClick,
}: {
  select?: ReactNode;
  status: string;
  typeLabel: ReactNode;
  id: string;
  source?: ReactNode;
  primary: ReactNode;
  secondary?: ReactNode;
  detail?: ReactNode;
  progress?: JobProgress | null;
  activeSince?: string | null;
  timestamp?: ReactNode;
  actions?: ReactNode;
  error?: string | null;
  result?: ReactNode;
  className?: string;
  entrance?: StaggeredEntranceProps;
  onClick: () => void;
}) {
  const progressOrTime = progress ? <RealProgressBar progress={progress} /> : activeSince ? <Elapsed since={activeSince} active /> : timestamp;
  return (
    <div className={entrance?.className} style={entrance?.style}>
      <div
        onClick={onClick}
        className={`card grid min-h-[64px] w-full min-w-0 cursor-pointer grid-cols-[28px_minmax(78px,0.55fr)_minmax(78px,0.55fr)_60px_minmax(68px,0.45fr)_minmax(150px,1fr)_minmax(220px,1.25fr)_minmax(220px,0.9fr)_minmax(152px,max-content)] items-center gap-2 p-2 text-sm hover:border-accent/50 ${className}`}
      >
        <div className="flex items-center justify-center">{select || <span aria-hidden className="h-4 w-4" />}</div>
        <div className="min-w-0"><StatusBadge status={status} /></div>
        <div className="truncate text-xs font-medium text-fg" title={typeof typeLabel === "string" ? typeLabel : undefined}>{typeLabel}</div>
        <span className="font-mono text-xs text-muted">{shortId(id)}</span>
        <div className="min-w-0">{source || <span className="text-xs text-muted">—</span>}</div>
        <RowMeta primary={primary} secondary={secondary} />
        <div className="min-w-0 truncate text-xs text-muted" title={typeof detail === "string" ? detail : undefined}>{detail || "—"}</div>
        <div className="flex min-w-0 justify-start text-xs text-muted">{progressOrTime || "—"}</div>
        <RowActions>{actions}</RowActions>
      </div>
      {(error || result) && (
        <div className="mt-1 w-full">
          {error ? <ErrorExcerpt value={error} /> : <div className="line-clamp-1 rounded-md border border-border bg-subtle px-2 py-1 text-xs text-muted">{result}</div>}
        </div>
      )}
    </div>
  );
}

function JobsFilterPanel({
  activeTab,
  activeFilterCount,
  lastUpdatedLabel,
  search,
  status,
  dlSource,
  subscriptionSourceId,
  downloadJobId,
  dlSort,
  dlOrder,
  selectAll,
  statusOptions,
  onTabChange,
  onUpdateParams,
  onClearFilters,
  onSelectAll,
}: {
  activeTab: JobsTab;
  activeFilterCount: number;
  lastUpdatedLabel: string;
  search: string;
  status: string;
  dlSource: string;
  subscriptionSourceId: string;
  downloadJobId: string;
  dlSort: string;
  dlOrder: string;
  selectAll: boolean;
  statusOptions: string[];
  onTabChange: (tab: JobsTab) => void;
  onUpdateParams: (updates: Record<string, string | null>) => void;
  onClearFilters: () => void;
  onSelectAll: () => void;
}) {
  const t = useT();
  return (
    <div className="mb-4 flex flex-col gap-2 rounded-md border border-border bg-surface p-2.5 dark:border-border dark:bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <JobsTabBar activeTab={activeTab} onChange={onTabChange} />
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          {activeFilterCount > 0 && <span className="rounded-full bg-accent-subtle px-2 py-0.5 font-medium text-accent dark:bg-accent-subtle dark:text-accent">{t("jobs.active_filters", { count: activeFilterCount })}</span>}
          <span>{lastUpdatedLabel}</span>
          {activeFilterCount > 0 && <button onClick={onClearFilters} className="text-accent hover:underline dark:text-accent">{t("jobs.clear_filters")}</button>}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input value={search} onChange={(e) => onUpdateParams({ q: e.target.value || null })} className="input h-9 min-h-9 min-w-[220px] px-3 py-0 text-xs" placeholder={t("jobs.search_placeholder")} aria-label={t("jobs.search_placeholder")} />
        <select value={status} onChange={(e) => onUpdateParams({ status: e.target.value || null })} className="select h-9 min-h-9 px-2 py-0 text-xs">
          <option value="">{t("jobs.filter_all_status")}</option>
          {statusOptions.filter(Boolean).map((s) => <option key={s} value={s}>{statusLabel(t, s)}</option>)}
        </select>
        {activeTab !== "imports" && activeTab !== "admin" && (
          <select value={dlSource} onChange={(e) => onUpdateParams({ source: e.target.value || null })} className="select h-9 min-h-9 px-2 py-0 text-xs">
            <option value="">{t("jobs.filter_all_source")}</option>
            {SOURCE_OPTIONS.filter(Boolean).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        {subscriptionSourceId && <span className="rounded-md border border-border px-2 py-1 text-xs font-mono dark:border-border">{t("jobs.repository")} {shortId(subscriptionSourceId)}</span>}
        {downloadJobId && <span className="rounded-md border border-border px-2 py-1 text-xs font-mono dark:border-border">{t("jobs.download_job")} {shortId(downloadJobId)}</span>}
        {activeTab === "downloads" && (
          <select value={`${dlSort}-${dlOrder}`} onChange={(e) => { const [k, o] = e.target.value.split("-"); onUpdateParams({ sort: k === "created_at" && o === "desc" ? null : k, order: o === "desc" ? null : o }); }} className="select h-9 min-h-9 px-2 py-0 text-xs">
            <option value="created_at-desc">{t("jobs.sort_newest")}</option>
            <option value="created_at-asc">{t("jobs.sort_oldest")}</option>
            <option value="status-asc">{t("jobs.sort_status")}</option>
            <option value="source-asc">{t("jobs.sort_source")}</option>
          </select>
        )}
        <button onClick={onSelectAll} className="inline-flex h-9 items-center justify-center rounded-md border border-border bg-surface px-3 text-xs font-medium text-fg hover:bg-subtle">{selectAll ? t("common.deselect_all") : t("common.select_all")}</button>
      </div>
    </div>
  );
}

function JobsBatchToolbar({
  activeTab,
  selectedCount,
  onRefresh,
  onApply,
  onClear,
  onKillStuck,
  onRetryAllFailed,
  connected,
  wsStatus,
  isApplying,
  isClearing,
  isKillingStuck,
  isRetryingAll,
}: {
  activeTab: JobsTab;
  selectedCount: number;
  onRefresh: () => void;
  onApply: (action: BatchAction, filters: { source?: string; status?: string }) => void;
  onClear: (statuses: string[]) => void;
  onKillStuck: () => void;
  onRetryAllFailed: () => void;
  connected: boolean;
  wsStatus: string;
  isApplying: boolean;
  isClearing: boolean;
  isKillingStuck: boolean;
  isRetryingAll: boolean;
}) {
  const t = useT();
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const [action, setAction] = useState<BatchAction | "">("");
  const actions = BATCH_ACTIONS_BY_TAB[activeTab];
  const statusOptions = activeTab === "imports" ? IMPORT_STATUS_OPTIONS : activeTab === "downloads" ? STATUS_OPTIONS : TASK_STATUS_OPTIONS;
  const canFilterBatch = activeTab === "downloads" || activeTab === "imports";
  const hasSourceFilter = activeTab === "downloads";

  useEffect(() => {
    setSource("");
    setStatus("");
    setAction((current) => current && !BATCH_ACTIONS_BY_TAB[activeTab].includes(current) ? "" : current);
  }, [activeTab]);

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <button onClick={onRefresh} className="btn-ghost text-xs">{t("jobs.refresh")}</button>
      {selectedCount > 0 && (
        <span className="inline-flex h-9 items-center rounded-md border border-border bg-subtle px-3 text-xs font-medium text-fg">
          {t("common.selected_count", { count: selectedCount })}
        </span>
      )}
      {hasSourceFilter && (
        <select value={source} onChange={(e) => setSource(e.target.value)} className="select h-9 min-h-9 px-2 py-0 text-xs" aria-label={t("jobs.filter_all_source")}>
          <option value="">{t("jobs.filter_all_source")}</option>
          {SOURCE_OPTIONS.filter(Boolean).map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      )}
      {canFilterBatch && (
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="select h-9 min-h-9 px-2 py-0 text-xs" aria-label={t("jobs.filter_all_status")}>
          <option value="">{t("jobs.filter_all_status")}</option>
          {statusOptions.filter(Boolean).map((s) => <option key={s} value={s}>{statusLabel(t, s)}</option>)}
        </select>
      )}
      <select value={action} onChange={(e) => setAction(e.target.value as BatchAction | "")} className="select h-9 min-h-9 px-2 py-0 text-xs" aria-label={t("jobs.batch_action")}>
        <option value="">{t("jobs.batch_action_placeholder")}</option>
        {actions.map((item) => (
          <option key={item} value={item}>{t(`jobs.batch_action_${item}`)}</option>
        ))}
      </select>
      <button onClick={() => action && onApply(action, { source, status })} disabled={!action || isApplying} className="btn-primary text-xs">
        {isApplying ? t("jobs.batch_running") : t("jobs.batch_apply")}
      </button>
      {activeTab === "downloads" && (
        <>
          <button onClick={() => onClear(["failed", "stale"])} disabled={isClearing} className="btn-danger text-xs">{t("jobs.clear_failed")}</button>
          <button onClick={() => onClear(["complete"])} disabled={isClearing} className="btn-ghost text-xs">{t("jobs.clear_complete")}</button>
          <button onClick={onKillStuck} disabled={isKillingStuck} className="btn-ghost text-xs">{t("jobs.kill_stuck")}</button>
          <button onClick={onRetryAllFailed} disabled={isRetryingAll} className="btn-primary text-xs">{t("jobs.retry_all_failed")}</button>
        </>
      )}
      <span
        className={`inline-flex h-9 items-center gap-1 rounded-full border px-3 text-xs ${
          connected
            ? "border-success/30 bg-success-subtle text-success dark:border-success/30 dark:bg-success-subtle dark:text-success"
            : "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted"
        }`}
        title={connected ? t("jobs.live_connected") : t("jobs.live_polling_title")}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-current" : "animate-pulse bg-current"}`} />
        {connected ? t("jobs.live_connected") : wsStatus === "connecting" ? t("jobs.live_connecting") : t("jobs.live_polling")}
      </span>
    </div>
  );
}

type TaskNode = {
  task: TaskRun;
  children: TaskNode[];
};

function taskRunProgress(task: TaskRun): JobProgress | null {
  const progress = task.progress_data as JobProgress | null;
  if (progress) return progress;
  if (!task.progress_stage) return null;
  return {
    stage: task.progress_stage,
    current: task.progress_current || undefined,
    total: task.progress_total || undefined,
  };
}

function flattenTaskNode(node: TaskNode): TaskRun[] {
  return [node.task, ...node.children.flatMap(flattenTaskNode)];
}

function buildTaskTree(tasks: TaskRun[]): TaskNode[] {
  const nodes = new Map<string, TaskNode>();
  for (const task of tasks) nodes.set(task.id, { task, children: [] });
  const roots: TaskNode[] = [];
  for (const task of tasks) {
    const node = nodes.get(task.id)!;
    const parent = task.parent_task_id ? nodes.get(task.parent_task_id) : null;
    if (parent && parent.task.id !== task.id) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

function aggregateTaskGroup(node: TaskNode): { status: string; progress: JobProgress | null; active: number; failed: number; complete: number; total: number } {
  const tasks = flattenTaskNode(node).filter((task) => task.id !== node.task.id);
  if (!tasks.length) {
    return {
      status: node.task.status,
      progress: taskRunProgress(node.task),
      active: 0,
      failed: 0,
      complete: node.task.status === "complete" ? 1 : 0,
      total: 1,
    };
  }
  const failed = tasks.filter((task) => ["failed", "stale", "cancelled"].includes(task.status)).length;
  const active = tasks.filter((task) => isAttentionStatus(task.status) && !["failed", "stale", "cancelled"].includes(task.status)).length;
  const complete = tasks.filter((task) => task.status === "complete").length;
  const total = tasks.length;
  const status = failed > 0 ? "failed" : active > 0 ? "running" : complete === total ? "complete" : node.task.status;
  return {
    status,
    progress: {
      stage: status,
      current: complete,
      total,
      percent: total ? (complete / total) * 100 : undefined,
      message: `${complete}/${total}`,
    },
    active,
    failed,
    complete,
    total,
  };
}

function TaskRunRow({
  task,
  selected,
  onToggleSelect,
  openTaskDetail,
  openDownloadDetail,
  openImportDetail,
  entrance,
  indent = 0,
}: {
  task: TaskRun;
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
  openTaskDetail: (id: string) => void;
  openDownloadDetail: (id: string) => void;
  openImportDetail: (id: string) => void;
  entrance?: StaggeredEntranceProps;
  indent?: number;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const progress = taskRunProgress(task);
  const subjectId = task.subject_id;
  const clickableDownload = task.subject_type === "download_job" && subjectId;
  const clickableImport = task.subject_type === "import_job" && subjectId;
  const isActive = isActiveTask(task.status);
  return (
    <div className={indent ? "pl-6" : undefined}>
      <JobRowShell
        id={task.id}
        entrance={entrance}
        status={task.status}
        typeLabel={operationLabel(t, task.operation_type, task.kind)}
        select={<input type="checkbox" checked={selected.has(task.id)} onClick={(e) => e.stopPropagation()} onChange={() => onToggleSelect(task.id)} className="h-4 w-4 shrink-0 rounded border-border" aria-label={t("jobs.select_task", { id: shortId(task.id) })} />}
        source={task.source ? <SourceBadge source={task.source} /> : undefined}
        primary={task.title || task.operation_type || task.kind}
        secondary={task.subject_type && task.subject_id ? `${task.subject_type} · ${shortId(task.subject_id)}` : task.queue_name || task.rq_job_id}
        detail={task.source_url || task.queue_name || task.rq_job_id || task.subject_type}
        progress={isActive ? progress || fallbackProgress(task.progress_stage || task.status) : progress}
        activeSince={isActive && !progress && task.created_at ? task.created_at : null}
        timestamp={task.created_at ? fmt.time(task.created_at) : "—"}
        error={task.error_log}
        result={task.result_data?.message}
        onClick={() => openTaskDetail(task.id)}
        actions={(
          <>
            {clickableDownload && <RowButton onClick={() => openDownloadDetail(subjectId)}>{t("jobs.open_download")}</RowButton>}
            {clickableImport && <RowButton onClick={() => openImportDetail(subjectId)}>{t("jobs.import_detail")}</RowButton>}
          </>
        )}
      />
    </div>
  );
}

function UnifiedTaskList({
  tasks,
  isLoading,
  error,
  selected,
  onRetry,
  onToggleSelect,
  openTaskDetail,
  openDownloadDetail,
  openImportDetail,
}: {
  tasks?: { total: number; items: TaskRun[] };
  isLoading: boolean;
  error: unknown;
  selected: Set<string>;
  onRetry: () => void;
  onToggleSelect: (id: string) => void;
  openTaskDetail: (id: string) => void;
  openDownloadDetail: (id: string) => void;
  openImportDetail: (id: string) => void;
}) {
  const t = useT();
  const rows = tasks?.items || [];
  const rowEntrance = useStaggeredEntrance(rows.map((task) => task.id));

  return (
    <section className="mb-8">
      <h3 className="mb-2 flex items-center gap-3 text-base font-semibold">
        {t("jobs.title")}
        <span className="text-xs font-normal text-muted">{tasks?.total ?? 0} {t("common.items")}</span>
      </h3>
      {isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div>}
      {Boolean(error) && <ErrorState message={(error as Error).message} onRetry={onRetry} />}
      {!isLoading && !error && rows.length === 0 && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
      {rows.length > 0 && (
        <div className="space-y-1">
          {rows.map((task, index) => (
            <TaskRunRow
              key={task.id}
              task={task}
              entrance={rowEntrance(task.id, index)}
              selected={selected}
              onToggleSelect={onToggleSelect}
              openTaskDetail={openTaskDetail}
              openDownloadDetail={openDownloadDetail}
              openImportDetail={openImportDetail}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function TaskTreeChildren({
  nodes,
  selected,
  onToggleSelect,
  openTaskDetail,
  openDownloadDetail,
  openImportDetail,
  indent,
}: {
  nodes: TaskNode[];
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
  openTaskDetail: (id: string) => void;
  openDownloadDetail: (id: string) => void;
  openImportDetail: (id: string) => void;
  indent: number;
}) {
  const nodeEntrance = useStaggeredEntrance(nodes.map((node) => node.task.id));
  return (
    <div className="space-y-1">
      {nodes.map((node, index) => (
        <div key={node.task.id} className="space-y-1">
          <TaskRunRow
            task={node.task}
            entrance={nodeEntrance(node.task.id, index)}
            selected={selected}
            onToggleSelect={onToggleSelect}
            openTaskDetail={openTaskDetail}
            openDownloadDetail={openDownloadDetail}
            openImportDetail={openImportDetail}
            indent={indent}
          />
          {node.children.length > 0 && (
            <TaskTreeChildren
              nodes={node.children}
              selected={selected}
              onToggleSelect={onToggleSelect}
              openTaskDetail={openTaskDetail}
              openDownloadDetail={openDownloadDetail}
              openImportDetail={openImportDetail}
              indent={indent + 1}
            />
          )}
        </div>
      ))}
    </div>
  );
}

function GroupedTaskList({
  tasks,
  isLoading,
  error,
  selected,
  onRetry,
  onToggleSelect,
  openTaskDetail,
  openDownloadDetail,
  openImportDetail,
}: {
  tasks?: { total: number; items: TaskRun[] };
  isLoading: boolean;
  error: unknown;
  selected: Set<string>;
  onRetry: () => void;
  onToggleSelect: (id: string) => void;
  openTaskDetail: (id: string) => void;
  openDownloadDetail: (id: string) => void;
  openImportDetail: (id: string) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const rows = useMemo(() => tasks?.items ?? [], [tasks?.items]);
  const roots = useMemo(() => buildTaskTree(rows), [rows]);
  const rootEntrance = useStaggeredEntrance(roots.map((node) => node.task.id));
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    setExpanded((prev) => {
      const next = new Set(prev);
      let changed = false;
      for (const node of roots) {
        if (node.children.length > 0 && isAttentionStatus(aggregateTaskGroup(node).status)) {
          if (!next.has(node.task.id)) changed = true;
          next.add(node.task.id);
        }
      }
      return changed ? next : prev;
    });
  }, [roots]);

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <section className="mb-8">
      <h3 className="mb-2 flex items-center gap-3 text-base font-semibold">
        {t("jobs.title")}
        <span className="text-xs font-normal text-muted">{tasks?.total ?? 0} {t("common.items")}</span>
      </h3>
      {isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div>}
      {Boolean(error) && <ErrorState message={(error as Error).message} onRetry={onRetry} />}
      {!isLoading && !error && roots.length === 0 && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
      {roots.length > 0 && (
        <div className="space-y-2">
          {roots.map((node, index) => {
            if (node.children.length === 0) {
              return (
                <TaskRunRow
                  key={node.task.id}
                  task={node.task}
                  entrance={rootEntrance(node.task.id, index)}
                  selected={selected}
                  onToggleSelect={onToggleSelect}
                  openTaskDetail={openTaskDetail}
                  openDownloadDetail={openDownloadDetail}
                  openImportDetail={openImportDetail}
                />
              );
            }
            const aggregate = aggregateTaskGroup(node);
            const isOpen = expanded.has(node.task.id);
            const childCount = flattenTaskNode(node).length - 1;
            const source = node.task.source || flattenTaskNode(node).find((task) => task.source)?.source;
            const summary = t("jobs.group_summary", {
              active: aggregate.active,
              failed: aggregate.failed,
              complete: aggregate.complete,
              total: aggregate.total,
            });
            return (
              <div key={node.task.id} className="space-y-1">
                <JobRowShell
                  id={node.task.id}
                  entrance={rootEntrance(node.task.id, index)}
                  status={aggregate.status}
                  typeLabel={operationLabel(t, node.task.operation_type, node.task.kind)}
                  select={(
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); toggleExpanded(node.task.id); }}
                      className="flex h-6 w-6 items-center justify-center rounded border border-border text-xs text-muted hover:bg-subtle"
                      aria-label={isOpen ? t("jobs.collapse_group") : t("jobs.expand_group")}
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>
                  )}
                  source={source ? <SourceBadge source={source} /> : undefined}
                  primary={node.task.title || operationLabel(t, node.task.operation_type, node.task.kind)}
                  secondary={t("jobs.group_children", { count: childCount })}
                  detail={node.task.source_url || node.task.queue_name || summary}
                  progress={aggregate.progress}
                  timestamp={node.task.updated_at ? fmt.time(node.task.updated_at) : node.task.created_at ? fmt.time(node.task.created_at) : "—"}
                  result={summary}
                  onClick={() => openTaskDetail(node.task.id)}
                  actions={<RowButton onClick={() => openTaskDetail(node.task.id)}>{t("jobs.task_detail")}</RowButton>}
                />
                {isOpen && (
                  <TaskTreeChildren
                    nodes={node.children}
                    selected={selected}
                    onToggleSelect={onToggleSelect}
                    openTaskDetail={openTaskDetail}
                    openDownloadDetail={openDownloadDetail}
                    openImportDetail={openImportDetail}
                    indent={1}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function JobsContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const [downloadProgress, setDownloadProgress] = useState<Record<string, JobProgress>>({});
  const [importProgress, setImportProgress] = useState<Record<string, JobProgress>>({});

  // One-shot row flash on WS status changes — visual confirmation of which
  // job just transitioned. Set clears itself after the animation window.
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
  const flashTimers = useRef<Map<string, number>>(new Map());
  const triggerFlash = (jobId: string) => {
    setFlashIds((prev) => new Set(prev).add(jobId));
    const existing = flashTimers.current.get(jobId);
    if (existing) window.clearTimeout(existing);
    flashTimers.current.set(jobId, window.setTimeout(() => {
      setFlashIds((prev) => { const next = new Set(prev); next.delete(jobId); return next; });
      flashTimers.current.delete(jobId);
    }, 1200));
  };
  useEffect(() => {
    const timers = flashTimers.current;
    return () => { timers.forEach((timer) => window.clearTimeout(timer)); };
  }, []);

  // WebSocket: invalidate queries on status change, update progress on progress events
  const { connected, status: wsStatus } = useJobWebSocket({
    onStatusChange: (msg) => {
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: ["workbench"] });
      if (msg.task_id && msg.new_status) {
        triggerFlash(msg.task_id);
        toast.info(`${msg.task_id.slice(0, 8)}: ${msg.old_status} → ${msg.new_status}`);
      }
    },
    onProgress: (msg) => {
      const taskId = msg.task_id;
      if (taskId && msg.task_type === "download" && msg.progress) {
        setDownloadProgress(prev => ({ ...prev, [taskId]: msg.progress as JobProgress }));
      }
      if (taskId && msg.task_type === "import" && msg.progress) {
        setImportProgress(prev => ({ ...prev, [taskId]: msg.progress as JobProgress }));
      }
    },
  });
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();

  const tabParam = sp.get("tab");
  const activeTab = (tabParam === "downloads" || tabParam === "imports" || tabParam === "admin" ? tabParam : "all") as JobsTab;
  const status = sp.get("status") || "";
  const dlSource = sp.get("source") || "";
  const subscriptionSourceId = sp.get("subscription_source_id") || "";
  const downloadJobId = sp.get("download_job_id") || "";
  const search = sp.get("q") || "";
  const dlSort = sp.get("sort") || "created_at";
  const dlOrder = sp.get("order") || "desc";
  const selectedDownloadJobId = sp.get("job");
  const selectedImportJobId = sp.get("import_job");
  const selectedTaskId = sp.get("task");

  const [retryId, setRetryId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleteType, setDeleteType] = useState<"dl" | "im">("dl");
  const [expandedImports, setExpandedImports] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const updateParams = (updates: Record<string, string | null>, replace = true) => {
    const next = new URLSearchParams(sp.toString());
    Object.entries(updates).forEach(([key, value]) => {
      if (!value) next.delete(key); else next.set(key, value);
    });
    const href = next.toString() ? `${pathname}?${next.toString()}` : pathname;
    if (replace) router.replace(href, { scroll: false }); else router.push(href, { scroll: false });
  };

  const openTaskDetail = (id: string) => updateParams({ task: id, job: null, import_job: null }, false);
  const openDownloadDetail = (id: string) => updateParams({ tab: "downloads", job: id, import_job: null, task: null }, false);
  const openImportDetail = (id: string) => updateParams({ tab: "imports", import_job: id, job: null, task: null }, false);
  const closeDetail = () => updateParams({ job: null, import_job: null, task: null });
  const handleTabChange = (tab: JobsTab) => {
    setSelected(new Set());
    if (tab === "all") updateParams({ tab: null, status: null, job: null, import_job: null, task: null });
    if (tab === "downloads") updateParams({ tab: "downloads", status: null, import_job: null, task: null });
    if (tab === "imports") updateParams({ tab: "imports", status: null, job: null, task: null, source: null });
    if (tab === "admin") updateParams({ tab: "admin", status: null, job: null, import_job: null, task: null, source: null });
  };

  const dlParams = useMemo(() => ({
    status: activeTab === "downloads" ? status || undefined : undefined,
    source: dlSource || undefined,
    subscription_source_id: subscriptionSourceId || undefined,
    q: search || undefined,
    sort_by: dlSort,
    sort_order: dlOrder,
    offset: 0, limit: PAGE_LIMIT,
  }), [activeTab, status, dlSource, subscriptionSourceId, search, dlSort, dlOrder]);

  const downloads = useQuery({
    queryKey: [...queryKeys.downloadJobs.all, dlParams],
    queryFn: () => api.listDownloadJobs(dlParams),
    enabled: activeTab === "downloads",
    refetchInterval: (!status || isActiveDownload(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  const workbench = useQuery({
    queryKey: queryKeys.workbench,
    queryFn: api.workbench,
    refetchInterval: (query) => {
      const active = (query.state.data?.queue.active_download_count || 0) + (query.state.data?.queue.active_import_count || 0);
      return active > 0 ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS;
    },
  });

  const imports = useQuery({
    queryKey: [...queryKeys.importJobs.all, activeTab, status, downloadJobId, search],
    queryFn: () => api.listImportJobs({
      status: activeTab === "imports" ? status || undefined : undefined,
      download_job_id: downloadJobId || undefined,
      q: search || undefined,
      offset: 0,
      limit: PAGE_LIMIT,
    }),
    enabled: activeTab === "imports",
    refetchInterval: (!status || isActiveImport(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });
  const downloadEntrance = useStaggeredEntrance((downloads.data ?? []).map((job) => job.id));
  const importEntrance = useStaggeredEntrance((imports.data?.items ?? []).map((job) => job.id));

  const taskParams = useMemo(() => ({
    kind: activeTab === "admin" ? "admin" : undefined,
    status: status || undefined,
    source: activeTab === "admin" ? undefined : dlSource || undefined,
    q: search || undefined,
    offset: 0,
    limit: PAGE_LIMIT,
  }), [activeTab, status, dlSource, search]);

  const tasks = useQuery({
    queryKey: [...queryKeys.tasks.all, taskParams],
    queryFn: () => api.listTasks(taskParams),
    enabled: activeTab === "all" || activeTab === "admin",
    refetchInterval: (!status || isActiveTask(status)) ? REFETCH_ACTIVE_MS : REFETCH_IDLE_MS,
  });

  useEffect(() => {
    const rows = downloads.data ?? [];
    setDownloadProgress((prev) => {
      const next = { ...prev };
      const visibleIds = new Set(rows.map((job) => job.id));
      for (const id of Object.keys(next)) {
        if (!visibleIds.has(id)) delete next[id];
      }
      for (const job of rows) {
        if (!isActiveDownload(job.status)) {
          delete next[job.id];
        } else if (job.progress_data) {
          next[job.id] = job.progress_data;
        } else if (job.pipeline_stage && !next[job.id]) {
          next[job.id] = fallbackProgress(job.pipeline_stage);
        }
      }
      return next;
    });
  }, [downloads.data]);

  useEffect(() => {
    const rows = imports.data?.items ?? [];
    setImportProgress((prev) => {
      const next = { ...prev };
      const visibleIds = new Set(rows.map((job) => job.id));
      for (const id of Object.keys(next)) {
        if (!visibleIds.has(id)) delete next[id];
      }
      for (const job of rows) {
        if (!isActiveImport(job.status)) {
          delete next[job.id];
        } else if (job.progress_data) {
          next[job.id] = job.progress_data;
        } else if (job.progress_stage && !next[job.id]) {
          next[job.id] = fallbackProgress(job.progress_stage);
        }
      }
      return next;
    });
  }, [imports.data]);

  const activeFilterCount = [
    status,
    activeTab === "admin" ? "" : dlSource,
    subscriptionSourceId,
    downloadJobId,
    search,
    activeTab === "downloads" && (dlSort !== "created_at" || dlOrder !== "desc"),
  ].filter(Boolean).length;
  const lastUpdated = Math.max(downloads.dataUpdatedAt || 0, imports.dataUpdatedAt || 0, tasks.dataUpdatedAt || 0, workbench.dataUpdatedAt || 0);
  const currentRows = useMemo(() => {
    if (activeTab === "downloads") return downloads.data ?? [];
    if (activeTab === "imports") return imports.data?.items ?? [];
    return tasks.data?.items ?? [];
  }, [activeTab, downloads.data, imports.data?.items, tasks.data?.items]);
  const currentPageIds = useMemo(() => currentRows.map((row: { id: string }) => row.id), [currentRows]);
  const pageAllSelected = currentPageIds.length > 0 && currentPageIds.every((id) => selected.has(id));

  useEffect(() => {
    const visible = new Set(currentPageIds);
    setSelected((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (visible.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [currentPageIds]);

  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    qc.invalidateQueries({ queryKey: queryKeys.workbench });
  };
  const clearFilters = () => updateParams({
    status: null,
    source: null,
    subscription_source_id: null,
    download_job_id: null,
    q: null,
    sort: null,
    order: null,
  });

  // --- Mutations ---
  const retryDL = useMutation({
    mutationFn: (id: string) => api.retryDownloadJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
  });
  const retryIM = useMutation({
    mutationFn: (id: string) => api.retryImportJob(id),
    onSuccess: () => { setRetryId(null); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
  });
  const retryTask = useMutation({
    mutationFn: (id: string) => api.retryTask(id),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.workbench });
      toast.info(t("jobs.retry_queued"));
    },
    onError: (err) => toast.error((err as Error).message),
  });
  const pauseDL = useMutation({
    mutationFn: (id: string) => api.pauseDownloadJob(id),
    onMutate: (id) => {
      qc.setQueriesData({ queryKey: queryKeys.downloadJobs.all }, (old: any) => {
        if (!Array.isArray(old)) return old;
        return old.map((j: any) => j.id === id ? { ...j, status: "paused" } : j);
      });
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
  });
  const resumeDL = useMutation({
    mutationFn: (id: string) => api.resumeDownloadJob(id),
    onMutate: (id) => {
      qc.setQueriesData({ queryKey: queryKeys.downloadJobs.all }, (old: any) => {
        if (!Array.isArray(old)) return old;
        return old.map((j: any) => j.id === id ? { ...j, status: "enqueued" } : j);
      });
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.detail(selectedDownloadJobId || "") }); },
  });
  const deleteDL = useMutation({
    mutationFn: (id: string) => api.deleteDownloadJob(id),
    onSuccess: () => { setDeleteId(null); closeDetail(); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const deleteIM = useMutation({
    mutationFn: (id: string) => api.deleteImportJob(id),
    onSuccess: () => { setDeleteId(null); closeDetail(); qc.invalidateQueries({ queryKey: queryKeys.importJobs.all }); },
  });

  const clearDL = useMutation({
    mutationFn: (statuses: string[]) => api.clearDownloadJobs(statuses),
    onSuccess: () => { setSelected(new Set()); qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }); },
  });
  const killStuck = useMutation({
    mutationFn: () => api.killStuckJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });
  const retryAllFailed = useMutation({
    mutationFn: () => api.retryAllFailedJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all }),
  });

  const runSelectedTaskBatch = async (ids: string[], action: BatchAction) => {
    const actionMap = {
      retry: api.retryTask,
      pause: api.pauseTask,
      resume: api.resumeTask,
      cancel: api.cancelTask,
      delete: null,
    } satisfies Record<BatchAction, ((id: string) => Promise<unknown>) | null>;
    const runner = actionMap[action];
    if (!runner) return { succeeded: 0, failed: ids.length };
    const settled = await Promise.allSettled(ids.map((id) => runner(id)));
    return {
      succeeded: settled.filter((item) => item.status === "fulfilled").length,
      failed: settled.filter((item) => item.status === "rejected").length,
    };
  };

  const batchJobs = useMutation({
    mutationFn: async ({ action, filters }: { action: BatchAction; filters: { source?: string; status?: string } }) => {
      if (selected.size > 0) {
        const selectedRows = currentRows.filter((row: { id: string }) => selected.has(row.id)) as Array<{ id: string; status?: string }>;
        if (activeTab === "downloads") {
          const allowed = DOWNLOAD_BATCH_ALLOWED[action] || [];
          const ids = selectedRows.filter((row) => allowed.includes(row.status || "")).map((row) => row.id);
          if (!ids.length) return { succeeded: 0, failed: selected.size, total_matched: selected.size };
          if (ids.length < selected.size && !confirm(t("common.partial_selected"))) return { succeeded: 0, failed: 0, total_matched: selected.size };
          return api.batchDownloadJobs(ids, action);
        }
        if (activeTab === "imports") {
          const allowed = IMPORT_BATCH_ALLOWED[action] || [];
          const ids = selectedRows.filter((row) => allowed.includes(row.status || "")).map((row) => row.id);
          if (!ids.length) return { succeeded: 0, failed: selected.size, total_matched: selected.size };
          if (ids.length < selected.size && !confirm(t("common.partial_selected"))) return { succeeded: 0, failed: 0, total_matched: selected.size };
          return api.batchImportJobsByFilter({ ids }, action);
        }
        return runSelectedTaskBatch(selectedRows.map((row) => row.id), action);
      }

      if (activeTab === "downloads") {
        const batchFilters: Record<string, string> = {};
        if (filters.source) batchFilters.source = filters.source;
        if (filters.status) batchFilters.status = filters.status;
        return api.batchDownloadJobsByFilter(batchFilters, action);
      }
      if (activeTab === "imports") {
        const batchFilters: Record<string, string> = {};
        if (filters.status) batchFilters.status = filters.status;
        return api.batchImportJobsByFilter(batchFilters, action);
      }
      toast.warning({ message: t("jobs.select_rows_first") });
      return { succeeded: 0, failed: 0, total_matched: 0 };
    },
    onSuccess: (data: any) => {
      toast.info(t("jobs.batch_result", { succeeded: data.succeeded ?? 0, failed: data.failed ?? 0 }));
      setSelected(new Set());
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.workbench });
    },
    onError: (err) => toast.error((err as Error).message),
  });

  const handleSelectAll = () => {
    if (pageAllSelected) {
      setSelected((prev) => {
        const next = new Set(prev);
        currentPageIds.forEach((id) => next.delete(id));
        return next;
      });
      return;
    }
    setSelected((prev) => new Set([...prev, ...currentPageIds]));
  };

  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  };

  // Clear helpers
  const handleClear = (statuses: string[]) => {
    if (confirm(t("jobs.delete_all_confirm", { statuses: statuses.map((s) => statusLabel(t, s)).join(", ") }))) clearDL.mutate(statuses);
  };

  const statusOptions = activeTab === "imports"
    ? IMPORT_STATUS_OPTIONS
    : activeTab === "downloads"
      ? STATUS_OPTIONS
      : TASK_STATUS_OPTIONS;

  return (
    <PageShell size="wide">
      <PageHeader title={t("jobs.title")} description={t("jobs.desc")}>
        <JobsBatchToolbar
          activeTab={activeTab}
          selectedCount={selected.size}
          onRefresh={refreshAll}
          onApply={(action, filters) => batchJobs.mutate({ action, filters })}
          onClear={handleClear}
          onKillStuck={() => killStuck.mutate()}
          onRetryAllFailed={() => retryAllFailed.mutate()}
          connected={connected}
          wsStatus={wsStatus}
          isApplying={batchJobs.isPending}
          isClearing={clearDL.isPending}
          isKillingStuck={killStuck.isPending}
          isRetryingAll={retryAllFailed.isPending}
        />
      </PageHeader>

      {workbench.data && (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-5">
          <StatCard label={t("jobs.summary_active")} value={workbench.data.queue.active_download_count + workbench.data.queue.active_import_count} sub={t("jobs.summary_active_sub")} tone="active" />
          <StatCard label={t("jobs.summary_queued")} value={workbench.data.queue.default} sub={t("jobs.summary_queued_sub")} />
          <StatCard label={t("jobs.summary_importing")} value={workbench.data.queue.active_import_count} sub={t("jobs.summary_importing_sub")} tone={workbench.data.queue.active_import_count ? "active" : "neutral"} />
          <StatCard label={t("jobs.summary_failed")} value={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count} tone={workbench.data.queue.failed_download_count + workbench.data.queue.failed_import_count ? "danger" : "neutral"} />
          <StatCard label={t("jobs.summary_stale")} value={workbench.data.queue.stale_count} sub={t("jobs.summary_stale_sub")} tone={workbench.data.queue.stale_count ? "warning" : "neutral"} />
        </div>
      )}

      <JobsFilterPanel
        activeTab={activeTab}
        activeFilterCount={activeFilterCount}
        lastUpdatedLabel={t("jobs.last_refreshed", { time: lastUpdated ? fmt.time(new Date(lastUpdated).toISOString()) : "—" })}
        search={search}
        status={status}
        dlSource={dlSource}
        subscriptionSourceId={subscriptionSourceId}
        downloadJobId={downloadJobId}
        dlSort={dlSort}
        dlOrder={dlOrder}
        selectAll={pageAllSelected}
        statusOptions={statusOptions}
        onTabChange={handleTabChange}
        onUpdateParams={updateParams}
        onClearFilters={clearFilters}
        onSelectAll={handleSelectAll}
      />

      {activeTab === "all" && (
        <GroupedTaskList
          tasks={tasks.data}
          isLoading={tasks.isLoading}
          error={tasks.error}
          selected={selected}
          onRetry={() => tasks.refetch()}
          onToggleSelect={toggleSelect}
          openTaskDetail={openTaskDetail}
          openDownloadDetail={openDownloadDetail}
          openImportDetail={openImportDetail}
        />
      )}

      {activeTab === "admin" && (
        <UnifiedTaskList
          tasks={tasks.data}
          isLoading={tasks.isLoading}
          error={tasks.error}
          selected={selected}
          onRetry={() => tasks.refetch()}
          onToggleSelect={toggleSelect}
          openTaskDetail={openTaskDetail}
          openDownloadDetail={openDownloadDetail}
          openImportDetail={openImportDetail}
        />
      )}

      {/* Download Jobs list */}
      {activeTab === "downloads" && <section className="mb-8">
        <h3 className="mb-2 flex items-center gap-3 text-base font-semibold">
          {t("jobs.download")}
          <span className="text-xs font-normal text-muted">{downloads.data?.length ?? 0} {t("common.items")}</span>
        </h3>
        {downloads.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
        {downloads.error && <ErrorState message={(downloads.error as Error).message} onRetry={() => downloads.refetch()} />}
        {downloads.data && !downloads.data?.length && <EmptyState title={t("jobs.no_dl")} description={t("jobs.no_dl_desc")} />}
        {downloads.data && downloads.data?.length > 0 && (
          <div className="space-y-1">
            {downloads.data.map((j: any, index: number) => {
              const active = isActiveDownload(j.status);
              const progress = active
                ? downloadProgress[j.id] || (j.progress_data as JobProgress | null) || fallbackProgress(j.pipeline_stage || j.status)
                : null;
              const subscriptionLabel = j.creator_name || j.subscription_name || shortId(j.subscription_id);
              const subscriptionSecondary = j.subscription_name || (j.subscription_id ? `${t("jobs.subscription")} ${shortId(j.subscription_id)}` : t("jobs.subscription"));
              return (
                <div key={j.id}>
                  <JobRowShell
                    entrance={downloadEntrance(j.id, index)}
                    id={j.id}
                    status={j.status}
                    typeLabel={operationLabel(t, j.operation_type, "download")}
                    select={<input type="checkbox" checked={selected.has(j.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggleSelect(j.id)} className="h-4 w-4 shrink-0 rounded border-border" aria-label={t("jobs.select_download", { id: shortId(j.id) })} />}
                    source={j.source ? <SourceBadge source={j.source} /> : undefined}
                    primary={j.subscription_id ? (
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="text-accent hover:underline dark:text-accent"
                        title={j.creator_name || j.subscription_name || j.subscription_id}
                      >
                        {subscriptionLabel}
                      </Link>
                    ) : subscriptionLabel}
                    secondary={j.subscription_id ? (
                      <Link
                        href={`/admin/subscriptions/${j.subscription_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="text-muted hover:underline dark:text-muted"
                        title={j.subscription_name || j.subscription_id}
                      >
                        {subscriptionSecondary}
                      </Link>
                    ) : subscriptionSecondary}
                    detail={j.source_url}
                    progress={progress}
                    activeSince={active ? j.created_at : null}
                    timestamp={classifyJob(j.status, j.retry_count, 3) === "retrying" ? (
                      <span className="text-accent">
                        ↻ {t("jobs.recovery_retry", { current: String(j.retry_count), max: "3" })}
                        {estimatedRetryBackoff(j.retry_count, 60) != null && (
                          <span className="block text-[10px] text-muted">{t("jobs.recovery_waiting", { seconds: String(estimatedRetryBackoff(j.retry_count, 60)) })}</span>
                        )}
                      </span>
                    ) : (
                      <>
                        {j.retry_count > 0 && <span className="mr-1">↻{j.retry_count}</span>}
                        {fmt.time(j.created_at)}
                      </>
                    )}
                    error={j.error_log}
                    className={(() => { const cls = categoryBorderClass(classifyJob(j.status, j.retry_count, 3)); return `${cls ? `border-l-2 ${cls}` : ""} ${flashIds.has(j.id) ? "row-flash" : ""}`.trim(); })()}
                    onClick={() => openDownloadDetail(j.id)}
                    actions={(
                      <>
                        <RowButton onClick={() => setExpandedImports(expandedImports === j.id ? null : j.id)}>{t("jobs.imports")}</RowButton>
                        {["enqueued","downloading","downloaded","importing","failed","stale"].includes(j.status) && (
                          <RowButton onClick={() => pauseDL.mutate(j.id)} disabled={pauseDL.isPending}>{t("jobs.pause")}</RowButton>
                        )}
                        {j.status === "paused" && (
                          <RowButton onClick={() => resumeDL.mutate(j.id)} disabled={resumeDL.isPending}>{t("jobs.resume")}</RowButton>
                        )}
                        {(j.status === "failed" || j.status === "stale" || j.status === "complete") && (
                          <RowButton tone="primary" onClick={() => { setRetryId(j.id); retryDL.mutate(j.id); }} disabled={retryDL.isPending}>{t("jobs.retry")}</RowButton>
                        )}
                        <RowButton tone="danger" onClick={() => { setDeleteId(j.id); setDeleteType("dl"); }}>{t("jobs.del")}</RowButton>
                      </>
                    )}
                  />
                  {expandedImports === j.id && (
                    <ImportJobsList downloadJobId={j.id} />
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>}

      {/* Import Jobs */}
      {activeTab === "imports" && <section className="mb-8">
        <h3 className="text-base font-semibold mb-2 flex items-center gap-3">
          {t("jobs.import")}
          <span className="text-xs font-normal text-muted">{imports.data?.total ?? 0} {t("common.items")}</span>
        </h3>
        {imports.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 rounded-md bg-subtle dark:bg-subtle animate-pulse" />)}</div>}
        {imports.data?.items && !imports.data?.items.length && <p className="text-sm text-muted">{t("jobs.no_im")}</p>}
        {imports.data?.items && imports.data?.items.length > 0 && (
          <div className="space-y-1">
            {imports.data?.items?.map((j: any, index: number) => {
              const active = isActiveImport(j.status);
              const progress = active
                ? importProgress[j.id] || (j.progress_data as JobProgress | null) || fallbackProgress(j.progress_stage || j.status)
                : null;
              const worksDone = j.progress_works_done;
              const worksTotal = j.progress_works_total;
              return (
                <div key={j.id}>
                  <JobRowShell
                    entrance={importEntrance(j.id, index)}
                    id={j.id}
                    status={j.status}
                    typeLabel={operationLabel(t, j.operation_type, "import")}
                    select={<input type="checkbox" checked={selected.has(j.id)} onClick={(e) => e.stopPropagation()} onChange={() => toggleSelect(j.id)} className="h-4 w-4 shrink-0 rounded border-border" aria-label={t("jobs.select_import", { id: shortId(j.id) })} />}
                    source={j.source ? <SourceBadge source={j.source} /> : undefined}
                    primary={j.subscription_id ? (
                      <Link href={`/admin/subscriptions/${j.subscription_id}`} onClick={(e) => e.stopPropagation()} className="text-accent hover:underline dark:text-accent" title={j.creator_name || j.subscription_name || undefined}>
                        {j.creator_name || j.subscription_name || shortId(j.subscription_id)}
                      </Link>
                    ) : "—"}
                    secondary={j.subscription_name || `${t("jobs.import")} · ${shortId(j.download_job_id)}`}
                    detail={(
                      <span title={j.source_url || undefined}>
                        {j.source_url || "-"}
                        {(worksTotal != null || worksDone != null) && (
                          <span className="ml-2 font-mono text-[11px] tabular-nums" title={t("jobs.works")}>{worksDone ?? 0}/{worksTotal ?? "?"}</span>
                        )}
                      </span>
                    )}
                    progress={progress}
                    activeSince={active ? j.created_at : null}
                    timestamp={fmt.time(j.created_at)}
                    error={j.error_log}
                    className={`${active ? "border-l-2 border-l-accent" : j.status === "failed" ? "border-l-2 border-l-danger" : ""} ${flashIds.has(j.id) ? "row-flash" : ""}`.trim()}
                    onClick={() => openImportDetail(j.id)}
                    actions={(
                      <>
                        <Link href={`/admin/jobs?tab=downloads&job=${j.download_job_id}`} className="inline-flex h-8 min-w-[46px] items-center justify-center rounded-md border border-border bg-surface px-2 text-xs font-medium text-fg hover:bg-subtle">{t("jobs.open_download")}</Link>
                        {(j.status === "failed" || j.status === "stale") && <RowButton tone="primary" onClick={() => { setRetryId(j.id); retryIM.mutate(j.id); }} disabled={retryIM.isPending}>{t("jobs.retry")}</RowButton>}
                        <RowButton tone="danger" onClick={() => { setDeleteId(j.id); setDeleteType("im"); }}>{t("jobs.del")}</RowButton>
                      </>
                    )}
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>}

      <TaskDetailDrawer
        id={selectedTaskId}
        onClose={closeDetail}
        onRetryTask={(id) => retryTask.mutate(id)}
        onOpenDownload={openDownloadDetail}
        onOpenImport={openImportDetail}
      />

      <JobDetailDrawer
        kind={selectedImportJobId ? "import" : "download"}
        id={selectedTaskId ? null : selectedImportJobId || selectedDownloadJobId}
        onClose={closeDetail}
        onRetryDownload={(id) => retryDL.mutate(id)}
        onPauseDownload={(id) => pauseDL.mutate(id)}
        onResumeDownload={(id) => resumeDL.mutate(id)}
        onDeleteDownload={(id) => { setDeleteId(id); setDeleteType("dl"); }}
        onRetryImport={(id) => retryIM.mutate(id)}
        onDeleteImport={(id) => { setDeleteId(id); setDeleteType("im"); }}
      />

      {(deleteId && deleteType === "dl") && (
        <ConfirmDialog open title={t("jobs.delete_dl_title")} message={t("jobs.delete_dl_msg")} onConfirm={() => deleteDL.mutate(deleteId!)} onCancel={() => setDeleteId(null)} isPending={deleteDL.isPending} error={(deleteDL.error as Error)?.message} />
      )}
      {(deleteId && deleteType === "im") && (
        <ConfirmDialog open title={t("jobs.delete_im_title")} message={t("jobs.delete_im_msg")} onConfirm={() => deleteIM.mutate(deleteId!)} onCancel={() => setDeleteId(null)} isPending={deleteIM.isPending} error={(deleteIM.error as Error)?.message} />
      )}
    </PageShell>
  );
}

// Import jobs for a specific download job
function ImportJobsList({ downloadJobId }: { downloadJobId: string }) {
  const t = useT();
  const imports = useQuery({
    queryKey: ["import-jobs", downloadJobId],
    queryFn: () => api.getDownloadJobImports(downloadJobId),
  });
  const entrance = useStaggeredEntrance((imports.data ?? []).map((item) => item.id));
  if (imports.isLoading) return <div className="ml-8 mt-1 text-xs text-muted">{t("common.loading")}...</div>;
  if (!imports.data?.length) return <div className="ml-8 mt-1 text-xs text-muted">{t("jobs.no_imports_yet")}</div>;
  return (
    <div className="ml-8 mt-1 space-y-0.5">
      {imports.data?.map((imp: any, index: number) => (
        <div
          key={imp.id}
          className={`${entrance(imp.id, index).className ?? ""} flex items-center gap-2 rounded bg-subtle px-2 py-1 text-xs`}
          style={entrance(imp.id, index).style}
        >
          <span className="font-mono text-muted">{imp.id.slice(0, 8)}</span>
          <StatusBadge status={imp.status} className="px-2 py-0 text-[10px]" />
          {imp.error_log && <span className="max-w-xs truncate text-warning">{imp.error_log.slice(0, 100)}</span>}
        </div>
      ))}
    </div>
  );
}

export default function JobsPage() {
  return (
    <PermissionGuard module="tasks">
      <Suspense>
        <JobsContent />
      </Suspense>
    </PermissionGuard>
  );
}
