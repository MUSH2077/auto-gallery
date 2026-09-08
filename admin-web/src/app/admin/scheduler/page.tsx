"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  EmptyState,
  ErrorState,
  PageHeader,
  PageShell,
  Pagination,
  PermissionGuard,
  RowActionMenu,
  SmartSearchInput,
  SourceBadge,
  StatusBadge,
} from "@/components";
import { useToast } from "@/components/Toast";
import { adminRoutes } from "@/lib/adminRoutes";
import {
  ApiError,
  api,
  queryKeys,
  type SchedulerBatchItem,
  type SchedulerBatchResult,
  type SchedulerDecisionItem,
  type SchedulerSyncMode,
  type TaskRun,
} from "@/lib/api";
import { useT, type TFunction } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import { useJobWebSocket } from "@/lib/useWebSocket";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import { secureRandomUuid } from "@/lib/random";
import {
  scheduleModeLabel,
  schedulerDecisionLabel,
  useI18nFormat,
} from "@/lib/i18n-format";

const PLAN_PAGE_SIZE = 25;
const BATCH_ITEM_PAGE_SIZE = 50;
const BATCH_STORAGE_PREFIX = "auto-gallery-scheduler-batch-v1";

interface StoredBatchIntent {
  version: 1;
  userId: number;
  requestId: string;
  mode: SchedulerSyncMode;
  trackedMode?: SchedulerSyncMode | null;
  taskId?: string;
  finalNotified?: boolean;
}

const BATCH_MODES = new Set<SchedulerSyncMode>(["force_eligible", "due_scan", "manual_all_enabled"]);

function batchStorageKey(userId: number) {
  return `${BATCH_STORAGE_PREFIX}:${userId}`;
}

function readStoredBatchIntent(userId: number): StoredBatchIntent | null {
  try {
    const raw = localStorage.getItem(batchStorageKey(userId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<StoredBatchIntent>;
    if (
      value.version !== 1
      || value.userId !== userId
      || typeof value.requestId !== "string"
      || !BATCH_MODES.has(value.mode as SchedulerSyncMode)
      || (value.trackedMode != null && !BATCH_MODES.has(value.trackedMode as SchedulerSyncMode))
    ) {
      return null;
    }
    return value as StoredBatchIntent;
  } catch {
    return null;
  }
}

function schedulerBatchResult(task?: TaskRun): SchedulerBatchResult {
  return (task?.result_data || {}) as SchedulerBatchResult;
}

function isSchedulerBatchMode(mode: unknown): mode is SchedulerSyncMode {
  return typeof mode === "string" && BATCH_MODES.has(mode as SchedulerSyncMode);
}

function schedulerBatchMode(task?: TaskRun): SchedulerSyncMode | null {
  const resultMode = schedulerBatchResult(task).mode;
  if (isSchedulerBatchMode(resultMode)) return resultMode;
  const admittedMode = task?.meta?.mode;
  return isSchedulerBatchMode(admittedMode) ? admittedMode : null;
}

function schedulerBatchSettled(task?: TaskRun) {
  if (!task) return false;
  const result = schedulerBatchResult(task);
  if (task.status === "cancelled") return result.cleanup_pending === false;
  return ["complete", "failed", "stale"].includes(task.status);
}

function schedulerCount(result: SchedulerBatchResult, key: keyof SchedulerBatchResult) {
  const value = result[key];
  return typeof value === "number" ? value : 0;
}

const BATCH_REASON_KEYS: Record<string, string> = {
  auth_unhealthy: "scheduler.reason.auth_unhealthy",
  no_eligible_member_source: "scheduler.batch_reason.no_eligible_member_source",
  provider_not_downloadable: "scheduler.reason.provider_not_downloadable",
  scheduler_disabled: "scheduler.reason.scheduler_disabled",
  source_disabled: "scheduler.reason.source_disabled",
  source_not_found: "scheduler.batch_reason.source_not_found",
  source_url_empty: "scheduler.batch_reason.source_url_empty",
  subscription_inactive: "scheduler.reason.subscription_inactive",
  subscription_not_found: "scheduler.batch_reason.subscription_not_found",
  subscription_sync_disabled: "scheduler.reason.subscription_sync_disabled",
  unknown_provider: "scheduler.reason.unknown_provider",
  url_invalid: "scheduler.reason.url_invalid",
};

function schedulerBatchReasonLabel(t: TFunction, reason: string) {
  const key = BATCH_REASON_KEYS[reason];
  return key ? t(key) : t("scheduler.batch_reason.other", { reason });
}

function schedulerBatchProgressLabel(t: TFunction, task: TaskRun | undefined, result: SchedulerBatchResult) {
  if (!task) return t("scheduler.batch_accepted");
  if (task.status === "cancelled" && result.cleanup_pending !== false) return t("scheduler.batch_progress.cancelling");
  if (task.status === "cancelled") return t("scheduler.batch_progress.cancelled");
  if (task.status === "failed" || task.status === "stale" || result.status === "partial_error") return t("scheduler.batch_progress.failed");
  if (result.status === "noop") return t("scheduler.batch_progress.noop");
  if (task.status === "complete" || result.status === "complete") return t("scheduler.batch_progress.complete");
  if (schedulerCount(result, "importing_count") > 0) return t("scheduler.batch_progress.importing");
  if (schedulerCount(result, "downloading_count") > 0) return t("scheduler.batch_progress.downloading");
  if (result.waiting_reason === "infrastructure_unavailable") return t("scheduler.batch_progress.recovery");
  if (schedulerCount(result, "waiting_count") > 0) return t("scheduler.batch_progress.waiting");
  if (schedulerCount(result, "queued_count") > 0 || task.status === "enqueued") return t("scheduler.batch_progress.queued");
  if (schedulerCount(result, "pending_count") > 0) return t("scheduler.batch_progress.pending");
  return t("scheduler.batch_progress.running");
}

function isIrrecoverableTrackingError(error: Error | null | undefined) {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

function trackingErrorDetail(t: TFunction, error: Error) {
  if (error instanceof ApiError && error.kind === "network") return t("scheduler.batch_tracking_network");
  if (error instanceof ApiError) return t("scheduler.batch_tracking_http", { status: error.status });
  return t("scheduler.batch_tracking_unknown");
}

function SchedulerBatchStatus({
  intent,
  task,
  items,
  itemsTotal,
  isLoading,
  taskError,
  itemError,
  onCancel,
  onRetryTask,
  onRetryItems,
  onClearReference,
  onLoadMoreItems,
  cancelling,
  loadingMoreItems,
  hasMoreItems,
}: {
  intent: StoredBatchIntent;
  task?: TaskRun;
  items: SchedulerBatchItem[];
  itemsTotal: number;
  isLoading: boolean;
  taskError?: Error | null;
  itemError?: Error | null;
  onCancel: () => void;
  onRetryTask: () => void;
  onRetryItems: () => void;
  onClearReference: () => void;
  onLoadMoreItems: () => void;
  cancelling: boolean;
  loadingMoreItems: boolean;
  hasMoreItems: boolean;
}) {
  const t = useT();
  const result = schedulerBatchResult(task);
  const current = task?.progress_current ?? schedulerCount(result, "succeeded_count") + schedulerCount(result, "skipped_count") + schedulerCount(result, "failed_count") + schedulerCount(result, "cancelled_count");
  const total = task?.progress_total ?? schedulerCount(result, "candidate_count");
  const active = !schedulerBatchSettled(task);
  const cleanupPending = task?.status === "cancelled" && result.cleanup_pending !== false;
  const progressLabel = schedulerBatchProgressLabel(t, task, result);
  const trackedMode = schedulerBatchMode(task) || intent.trackedMode;
  const modeLabel = intent.taskId
    ? trackedMode ? t(`scheduler.batch_mode.${trackedMode}`) : t("scheduler.batch_mode_loading")
    : t(`scheduler.batch_mode.${intent.mode}`);
  const diagnosticLabel = typeof task?.progress_data?.label === "string" ? task.progress_data.label : undefined;
  const taskReferenceIrrecoverable = isIrrecoverableTrackingError(taskError);
  const itemReferenceIrrecoverable = isIrrecoverableTrackingError(itemError);
  const counts: Array<[keyof SchedulerBatchResult, string]> = [
    ["pending_count", "pending"],
    ["queued_count", "queued"],
    ["waiting_count", "waiting"],
    ["downloading_count", "downloading"],
    ["importing_count", "importing"],
    ["succeeded_count", "succeeded"],
    ["skipped_count", "skipped"],
    ["failed_count", "failed"],
    ["cancelled_count", "cancelled"],
  ];
  const notableItems = items.filter((item) => ["failed", "skipped", "cancelled"].includes(item.status));

  return (
    <section className="mb-5 rounded-md border border-border bg-surface" aria-live="polite">
      <div className="flex flex-col gap-3 p-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{t("scheduler.batch_current")}</h2>
            {taskError ? <StatusBadge status="failed" /> : task ? <StatusBadge status={cleanupPending ? "running" : task.status} /> : <StatusBadge status="enqueued" />}
          </div>
          <p className="mt-1 text-xs text-muted">
            {modeLabel} · <span className="font-mono">{intent.taskId?.slice(0, 8) || intent.requestId.slice(0, 8)}</span>
          </p>
          <p className="mt-1 text-xs text-muted">{t("scheduler.batch_completion_boundary")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {intent.taskId && (
            <Link className="btn-ghost min-h-11 text-xs" href={`${adminRoutes.jobs}?tab=admin&task=${encodeURIComponent(intent.taskId)}`}>
              {t("jobs.open_task")}
            </Link>
          )}
          {intent.taskId && active && task?.status !== "cancelled" && (
            <button type="button" className="btn-secondary min-h-11 text-xs" onClick={onCancel} disabled={cancelling}>
              {cancelling ? t("scheduler.batch_cancelling") : t("scheduler.batch_cancel")}
            </button>
          )}
        </div>
      </div>

      {cleanupPending && (
        <p className="border-t border-warning/30 bg-warning-subtle px-3 py-2 text-xs text-warning">
          {t("scheduler.batch_cleanup_pending")}
        </p>
      )}

      {taskError && (
        <div className="border-t border-danger/30 bg-danger-subtle px-3 py-3 text-sm text-danger" role="alert">
          <p className="font-medium">
            {taskReferenceIrrecoverable ? t("scheduler.batch_tracking_irrecoverable") : t("scheduler.batch_task_error")}
          </p>
          <p className="mt-1 text-xs" title={taskError.message}>{trackingErrorDetail(t, taskError)}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {!taskReferenceIrrecoverable && <button type="button" className="btn-danger min-h-11 text-xs" onClick={onRetryTask}>{t("scheduler.batch_retry_task")}</button>}
            {taskReferenceIrrecoverable && <button type="button" className="btn-danger min-h-11 text-xs" onClick={onClearReference}>{t("scheduler.batch_clear_reference")}</button>}
          </div>
        </div>
      )}

      <div className="border-t border-border p-3">
        {isLoading && !task && !taskError ? <div className="h-16 animate-pulse rounded bg-subtle" /> : !task && taskError ? null : (
          <>
            <div className="flex items-center justify-between gap-3 text-xs">
              <span className="font-medium" title={diagnosticLabel}>{progressLabel}</span>
              <span className="text-muted">{current} / {total || "—"}</span>
            </div>
            <progress className="mt-2 h-2 w-full accent-accent" max={Math.max(total, 1)} value={Math.min(current, Math.max(total, 1))} aria-label={t("jobs.progress")} />
            <div className="mt-3 flex flex-wrap gap-2">
              {counts.map(([key, status]) => {
                const count = schedulerCount(result, key);
                if (!count) return null;
                return <span key={status} className="badge">{t(`scheduler.batch_count.${status}`, { count })}</span>;
              })}
              {!total && <span className="text-xs text-muted">{t("scheduler.batch_waiting_snapshot")}</span>}
            </div>
            {Object.entries(result.skipped_reasons || {}).length > 0 && (
              <div className="mt-3 border-t border-border pt-3 text-xs">
                <p className="font-medium">{t("scheduler.batch_skipped_reasons")}</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {Object.entries(result.skipped_reasons || {}).map(([reason, count]) => (
                    <span key={reason} className="badge">{schedulerBatchReasonLabel(t, reason)}: {count}</span>
                  ))}
                </div>
              </div>
            )}
            {itemError && (
              <div className="mt-3 rounded-md border border-danger/30 bg-danger-subtle p-3 text-sm text-danger" role="alert">
                <p className="font-medium">
                  {itemReferenceIrrecoverable ? t("scheduler.batch_tracking_irrecoverable") : t("scheduler.batch_items_error")}
                </p>
                <p className="mt-1 text-xs" title={itemError.message}>{trackingErrorDetail(t, itemError)}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {!itemReferenceIrrecoverable && <button type="button" className="btn-danger min-h-11 text-xs" onClick={onRetryItems}>{t("scheduler.batch_retry_items")}</button>}
                  {itemReferenceIrrecoverable && <button type="button" className="btn-danger min-h-11 text-xs" onClick={onClearReference}>{t("scheduler.batch_clear_reference")}</button>}
                </div>
              </div>
            )}
            {notableItems.length > 0 && (
              <div className="mt-3 space-y-1 border-t border-border pt-3">
                {notableItems.map((item) => (
                  <div key={item.id} className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
                    <SourceBadge source={item.source || "unknown"} />
                    <span className="font-mono text-muted">{item.source_id.slice(0, 8)}</span>
                    <StatusBadge status={item.status} />
                    <span className={item.status === "failed" ? "text-danger" : "text-muted"}>
                      {item.error || (item.reason_code ? schedulerBatchReasonLabel(t, item.reason_code) : "—")}
                    </span>
                  </div>
                ))}
              </div>
            )}
            {itemsTotal > 0 && (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-xs text-muted">
                <span>{t("scheduler.batch_items_loaded", { loaded: items.length, total: itemsTotal })}</span>
                {hasMoreItems && (
                  <button type="button" className="btn-ghost min-h-11 text-xs" onClick={onLoadMoreItems} disabled={loadingMoreItems}>
                    {loadingMoreItems ? t("common.loading") : t("scheduler.batch_load_more")}
                  </button>
                )}
              </div>
            )}
            {schedulerBatchSettled(task) && (
              <Link href={`${adminRoutes.system}?tab=services`} className="mt-3 inline-flex min-h-11 items-center text-xs text-accent hover:underline">
                {t("scheduler.batch_background_link")}
              </Link>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function loopTone(status?: string | null) {
  if (status === "stalled") return "border-danger/30 bg-danger-subtle text-danger";
  if (status === "recovering") return "border-warning/30 bg-warning-subtle text-warning";
  return "border-success/30 bg-success-subtle text-success";
}

function AttentionRow({ item }: { item: SchedulerDecisionItem }) {
  const t = useT();
  const fmt = useI18nFormat();
  return (
    <article className="grid min-w-0 gap-3 border-t border-border px-3 py-3 sm:grid-cols-[minmax(0,1.3fr)_minmax(10rem,0.8fr)_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <SourceBadge source={item.source} />
          <Link href={adminRoutes.repository(item.source_id)} className="truncate font-medium text-accent hover:underline">
            {item.creator_name || item.subscription_name || item.source_id}
          </Link>
          {item.is_overdue && <span className="badge text-warning">{t("scheduler.overdue")}</span>}
        </div>
        <p className="mt-1 truncate font-mono text-xs text-muted" title={item.source_url || undefined}>{item.source_url || "—"}</p>
      </div>
      <div className="min-w-0 text-xs">
        <p className="font-medium text-danger">{schedulerDecisionLabel(t, item.suppression_reason || item.reason, item.due)}</p>
        <p className="mt-1 text-muted">
          {item.next_due_at ? t("scheduler.next_at", { time: fmt.dateTime(item.next_due_at) }) : t("scheduler.no_scan")}
        </p>
      </div>
      <RowActionMenu
        label={t("common.more_actions")}
        items={[
          { label: t("scheduler.open_repository"), href: adminRoutes.repository(item.source_id) },
          { label: t("scheduler.open_jobs"), href: `${adminRoutes.jobs}?tab=downloads&q=${encodeURIComponent(`repo:${item.source_id}`)}` },
          { label: t("scheduler.manage"), href: adminRoutes.subscription(item.subscription_id) },
        ]}
      />
    </article>
  );
}

function PlanRow({ item }: { item: SchedulerDecisionItem }) {
  const t = useT();
  const fmt = useI18nFormat();
  return (
    <div className="grid min-w-0 gap-2 border-t border-border px-3 py-3 text-xs sm:grid-cols-[minmax(0,1.2fr)_minmax(8rem,0.7fr)_minmax(10rem,1fr)_auto] sm:items-center">
      <div className="flex min-w-0 items-center gap-2">
        <SourceBadge source={item.source} />
        <Link href={adminRoutes.repository(item.source_id)} className="truncate font-medium text-accent hover:underline">
          {item.creator_name || item.subscription_name}
        </Link>
      </div>
      <span className="text-muted">{scheduleModeLabel(t, item.effective_mode)}</span>
      <span className="text-muted">{item.next_due_at ? fmt.dateTime(item.next_due_at) : "—"}</span>
      <span className="text-muted">{schedulerDecisionLabel(t, item.suppression_reason || item.reason, item.due)}</span>
    </div>
  );
}

function SchedulerContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { user } = usePermissions();
  const [plansOpen, setPlansOpen] = useState(false);
  const [batchIntent, setBatchIntent] = useState<StoredBatchIntent | null>(null);
  const [intentLoaded, setIntentLoaded] = useState(false);
  const restoredPendingRef = useRef(false);
  const finalToastRef = useRef<string | null>(null);
  const finalItemRefreshRef = useRef<string | null>(null);

  const persistBatchIntent = useCallback((next: StoredBatchIntent | null) => {
    setBatchIntent(next);
    if (!user?.id) return;
    try {
      if (next) localStorage.setItem(batchStorageKey(user.id), JSON.stringify(next));
      else localStorage.removeItem(batchStorageKey(user.id));
    } catch {}
  }, [user?.id]);

  useEffect(() => {
    if (!user?.id) return;
    const stored = readStoredBatchIntent(user.id);
    if (stored && !stored.taskId) restoredPendingRef.current = true;
    setBatchIntent(stored);
    setIntentLoaded(true);
  }, [user?.id]);

  const search = searchParams.get("q") || "";
  const stateFilter = searchParams.get("state") || "all";
  const parsedPage = Number.parseInt(searchParams.get("page") || "1", 10);
  const page = Number.isFinite(parsedPage) && parsedPage > 0 ? parsedPage : 1;

  const updateParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams.toString());
    Object.entries(updates).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key));
    router.replace(next.toString() ? `${pathname}?${next}` : pathname, { scroll: false });
  };

  const queue = useQuery({
    queryKey: queryKeys.system.queueStats,
    queryFn: api.queueStats,
    refetchInterval: (query) => {
      const active = query.state.data?.scheduler_loop?.active;
      return active && (active.started > 0 || active.queued > 0) ? 10_000 : 30_000;
    },
  });
  const attention = useQuery({
    queryKey: [...queryKeys.schedulerDecisions, "attention"],
    queryFn: () => api.schedulerDecisionsView("attention", 0, 500),
    refetchInterval: 30_000,
  });
  const plans = useQuery({
    queryKey: [...queryKeys.schedulerDecisions, "all"],
    queryFn: () => api.schedulerDecisionsView("all", 0, 500),
    enabled: plansOpen,
    staleTime: 30_000,
  });

  const batchTask = useQuery({
    queryKey: queryKeys.tasks.detail(batchIntent?.taskId || ""),
    queryFn: () => api.getTask(batchIntent?.taskId || ""),
    enabled: !!batchIntent?.taskId,
    retry: false,
    refetchInterval: (query) => schedulerBatchSettled(query.state.data) ? false : POLL_ACTIVE_MS,
    refetchIntervalInBackground: true,
  });

  const batchItems = useInfiniteQuery({
    queryKey: ["scheduler-batch-items", batchIntent?.taskId || ""],
    queryFn: ({ pageParam }) => api.getSchedulerBatchItems(batchIntent?.taskId || "", pageParam, BATCH_ITEM_PAGE_SIZE),
    initialPageParam: 0,
    getNextPageParam: (lastPage, _pages, lastPageParam) => {
      const nextOffset = lastPageParam + lastPage.items.length;
      return lastPage.items.length > 0 && nextOffset < lastPage.total ? nextOffset : undefined;
    },
    enabled: !!batchIntent?.taskId && !batchTask.error,
    retry: false,
    refetchInterval: () => schedulerBatchSettled(batchTask.data) ? false : POLL_ACTIVE_MS,
    refetchIntervalInBackground: true,
  });
  const batchItemList = useMemo(
    () => batchItems.data?.pages.flatMap((page) => page.items) || [],
    [batchItems.data?.pages],
  );
  const batchItemTotal = batchItems.data?.pages[0]?.total || 0;
  const batchItemsLoaded = (batchItems.data?.pages.length || 0) > 0;
  const refetchBatchItems = batchItems.refetch;
  const batchSettled = schedulerBatchSettled(batchTask.data);
  const cleanupComplete = schedulerBatchResult(batchTask.data).cleanup_pending === false;

  useEffect(() => {
    const taskId = batchIntent?.taskId;
    const taskStatus = batchTask.data?.status;
    if (!taskId || !taskStatus || !batchSettled || !batchItemsLoaded || batchItems.isFetching) return;
    const signature = `${taskId}:${taskStatus}:${cleanupComplete}`;
    if (finalItemRefreshRef.current === signature) return;
    finalItemRefreshRef.current = signature;
    void refetchBatchItems();
  }, [batchIntent?.taskId, batchItems.isFetching, batchItemsLoaded, batchSettled, batchTask.data?.status, cleanupComplete, refetchBatchItems]);

  const authoritativeBatchMode = schedulerBatchMode(batchTask.data);
  useEffect(() => {
    if (!batchIntent?.taskId || !authoritativeBatchMode || batchIntent.trackedMode === authoritativeBatchMode) return;
    persistBatchIntent({ ...batchIntent, trackedMode: authoritativeBatchMode });
  }, [authoritativeBatchMode, batchIntent, persistBatchIntent]);

  useJobWebSocket({
    enabled: !!batchIntent?.taskId && !schedulerBatchSettled(batchTask.data),
    onStatusChange: (message) => {
      const taskId = batchIntent?.taskId;
      if (!taskId || message.task_id !== taskId) return;
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(taskId) });
      void queryClient.invalidateQueries({ queryKey: ["scheduler-batch-items", taskId] });
    },
    onProgress: (message) => {
      const taskId = batchIntent?.taskId;
      if (!taskId || message.task_id !== taskId) return;
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(taskId) });
    },
  });

  const submitBatch = useMutation({
    mutationFn: (intent: StoredBatchIntent) => api.triggerSyncNow(intent.mode, intent.requestId),
    onSuccess: (data, intent) => {
      const accepted: StoredBatchIntent = { ...intent, trackedMode: data.mode, taskId: data.task_id, finalNotified: false };
      persistBatchIntent(accepted);
      toast.info({
        message: t("scheduler.batch_accepted"),
        action: { label: t("jobs.open_task"), onClick: () => router.push(`${adminRoutes.jobs}?tab=admin&task=${data.task_id}`) },
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.system.queueStats });
      queryClient.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (error: Error, intent) => {
      const apiError = error instanceof ApiError ? error : null;
      const detail = apiError?.detail && typeof apiError.detail === "object"
        ? apiError.detail as { task_id?: unknown }
        : null;
      if (apiError?.status === 409 && typeof detail?.task_id === "string") {
        persistBatchIntent({ ...intent, trackedMode: null, taskId: detail.task_id, finalNotified: false });
        toast.info({
          message: t("scheduler.batch_existing"),
          action: { label: t("jobs.open_task"), onClick: () => router.push(`${adminRoutes.jobs}?tab=admin&task=${detail.task_id}`) },
        });
        return;
      }
      if (apiError && apiError.status >= 400 && apiError.status < 500) persistBatchIntent(null);
      toast.error(error.message);
    },
  });

  useEffect(() => {
    if (!intentLoaded || !batchIntent || batchIntent.taskId || !restoredPendingRef.current || submitBatch.isPending) return;
    restoredPendingRef.current = false;
    submitBatch.mutate(batchIntent);
  }, [batchIntent, intentLoaded, submitBatch]);

  const cancelBatch = useMutation({
    mutationFn: () => api.cancelTask(batchIntent?.taskId || ""),
    onSuccess: (data) => {
      if (!batchIntent?.taskId) return;
      queryClient.setQueryData<TaskRun>(queryKeys.tasks.detail(batchIntent.taskId), (current) => current ? {
        ...current,
        status: data.status,
        progress_stage: data.cleanup_pending ? "cancelling" : "cancelled",
        result_data: {
          ...(current.result_data || {}),
          cleanup_pending: data.cleanup_pending,
          cleanup_task_id: data.cleanup_task_id,
          status: "cancelled",
        },
      } : current);
      void queryClient.invalidateQueries({ queryKey: queryKeys.tasks.detail(batchIntent.taskId) });
      void queryClient.invalidateQueries({ queryKey: ["scheduler-batch-items", batchIntent.taskId] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const startBatch = (mode: SchedulerSyncMode) => {
    if (!user?.id || submitBatch.isPending) return;
    if (batchIntent?.taskId && !schedulerBatchSettled(batchTask.data)) {
      router.push(`${adminRoutes.jobs}?tab=admin&task=${batchIntent.taskId}`);
      return;
    }
    const intent = !batchIntent?.taskId && batchIntent?.mode === mode
      ? batchIntent
      : { version: 1 as const, userId: user.id, requestId: secureRandomUuid(), mode };
    persistBatchIntent(intent);
    submitBatch.mutate(intent);
  };

  const clearBatchReference = () => {
    const taskId = batchIntent?.taskId;
    persistBatchIntent(null);
    finalToastRef.current = null;
    finalItemRefreshRef.current = null;
    restoredPendingRef.current = false;
    if (taskId) {
      queryClient.removeQueries({ queryKey: queryKeys.tasks.detail(taskId) });
      queryClient.removeQueries({ queryKey: ["scheduler-batch-items", taskId] });
    }
  };

  useEffect(() => {
    const task = batchTask.data;
    if (!batchIntent?.taskId || batchIntent.finalNotified || !schedulerBatchSettled(task)) return;
    const signature = `${batchIntent.taskId}:${task?.status}:${task?.updated_at || ""}`;
    if (finalToastRef.current === signature) return;
    finalToastRef.current = signature;
    persistBatchIntent({ ...batchIntent, trackedMode: schedulerBatchMode(task) || batchIntent.trackedMode, finalNotified: true });
    const result = schedulerBatchResult(task);
    const summary = t("scheduler.batch_final_summary", {
      succeeded: schedulerCount(result, "succeeded_count"),
      skipped: schedulerCount(result, "skipped_count"),
      failed: schedulerCount(result, "failed_count"),
      cancelled: schedulerCount(result, "cancelled_count"),
    });
    const options = {
      message: summary,
      action: { label: t("jobs.open_task"), onClick: () => router.push(`${adminRoutes.jobs}?tab=admin&task=${batchIntent.taskId}`) },
    };
    if (task?.status === "failed" || task?.status === "stale" || schedulerCount(result, "failed_count") > 0) toast.error(options);
    else if (schedulerCount(result, "skipped_count") > 0 || schedulerCount(result, "cancelled_count") > 0 || result.status === "noop") toast.warning(options);
    else toast.success(options);
  }, [batchIntent, batchTask.data, persistBatchIntent, router, t, toast]);

  const loop = queue.data?.scheduler_loop;
  const attentionItems = attention.data?.items || [];
  const blockedCount = attentionItems.filter((item) => !item.is_overdue).length;
  const overdueCount = attentionItems.filter((item) => item.is_overdue).length;
  const oldestOverdueAt = attentionItems
    .filter((item) => item.is_overdue && item.next_due_at)
    .map((item) => item.next_due_at as string)
    .sort()[0] || null;
  const visibleAttention = attentionItems;

  const filteredPlans = useMemo(() => {
    const normalized = search.trim().toLowerCase();
    return (plans.data?.items || []).filter((item) => {
      const matchesSearch = !normalized || [
        item.creator_name,
        item.subscription_name,
        item.source,
        item.source_url,
      ].some((value) => (value || "").toLowerCase().includes(normalized));
      const matchesState = stateFilter === "all"
        || (stateFilter === "due" && item.due)
        || (stateFilter === "manual" && item.reason === "manual_mode")
        || (stateFilter === "disabled" && ["source_disabled", "subscription_sync_disabled", "subscription_inactive"].includes(item.reason));
      return matchesSearch && matchesState;
    });
  }, [plans.data?.items, search, stateFilter]);
  const planPage = filteredPlans.slice((page - 1) * PLAN_PAGE_SIZE, page * PLAN_PAGE_SIZE);
  const batchActive = !!batchIntent?.taskId && !schedulerBatchSettled(batchTask.data);

  return (
    <PageShell>
        <PageHeader title={t("scheduler.title")} description={t("scheduler.compact_desc")} />

        {intentLoaded && batchIntent && (
          <SchedulerBatchStatus
            intent={batchIntent}
            task={batchTask.data}
            items={batchItemList}
            itemsTotal={batchItemTotal}
            isLoading={batchTask.isLoading}
            taskError={batchTask.error}
            itemError={batchItems.error}
            onCancel={() => cancelBatch.mutate()}
            onRetryTask={() => { void batchTask.refetch(); }}
            onRetryItems={() => { void batchItems.refetch(); }}
            onClearReference={clearBatchReference}
            onLoadMoreItems={() => { void batchItems.fetchNextPage(); }}
            cancelling={cancelBatch.isPending}
            loadingMoreItems={batchItems.isFetchingNextPage}
            hasMoreItems={batchItems.hasNextPage}
          />
        )}

        {queue.data?.scheduler_enabled === false && (
          <div className="mb-4 rounded-md border border-warning/30 bg-warning-subtle px-3 py-3 text-sm text-warning" role="status">
            <strong>{t("scheduler.paused_title")}</strong>
            <p className="mt-1 text-xs">{t("scheduler.paused_desc", { count: attention.data?.suppressed_count || 0 })}</p>
          </div>
        )}

        <section data-page-primary-content className="mb-4 rounded-md border border-border bg-surface">
          <div className="flex flex-col gap-3 p-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs" aria-live="polite">
              <span className={`inline-flex min-h-7 items-center rounded-full border px-2.5 font-medium ${loopTone(loop?.status)}`}>
                {t(`scheduler.loop_${loop?.status || "unknown"}`)}
              </span>
              <span><span className="text-muted">{t("scheduler.last_scan")}</span> <strong>{fmt.dateTime(loop?.last_finished_at)}</strong></span>
              <span><span className="text-muted">{t("scheduler.next_scan")}</span> <strong>{fmt.dateTime(loop?.next_scan_at || queue.data?.next_sync_scan_at)}</strong></span>
              <span><span className="text-muted">{t("scheduler.overdue")}</span> <strong className={overdueCount ? "text-warning" : ""}>{overdueCount}</strong></span>
              {oldestOverdueAt && <span><span className="text-muted">{t("scheduler.oldest_due")}</span> <strong className="text-warning">{fmt.dateTime(oldestOverdueAt)}</strong></span>}
              <span><span className="text-muted">{t("scheduler.blocked")}</span> <strong className={blockedCount ? "text-danger" : ""}>{blockedCount}</strong></span>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="btn-secondary min-h-11" onClick={() => startBatch("due_scan")} disabled={submitBatch.isPending || batchActive}>
                {submitBatch.isPending && batchIntent?.mode === "due_scan" ? t("scheduler.scanning") : t("scheduler.run_due_scan")}
              </button>
              <button type="button" className="btn-primary min-h-11" onClick={() => startBatch("manual_all_enabled")} disabled={submitBatch.isPending || batchActive}>
                {submitBatch.isPending && batchIntent?.mode === "manual_all_enabled" ? t("scheduler.syncing_all") : t("scheduler.sync_all_enabled")}
              </button>
              <RowActionMenu
                label={t("common.more_actions")}
                items={[
                  { label: t("scheduler.defaults_title"), href: adminRoutes.settingsSection("scheduler-defaults") },
                ]}
              />
            </div>
          </div>
          {loop?.last_error && <p className="border-t border-danger/20 bg-danger-subtle px-3 py-2 text-xs text-danger">{loop.last_error}</p>}
        </section>

        {queue.data && (
          <Link href={adminRoutes.settingsSection("scheduler-defaults")} className="mb-5 flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-subtle px-3 py-2 text-xs text-muted hover:border-accent/40 hover:text-fg">
            <strong className="text-fg">{t("scheduler.config_snapshot")}</strong>
            <span>{scheduleModeLabel(t, queue.data.scheduler_mode)}</span>
            <span>{queue.data.scheduler_timezone || "UTC"}</span>
            <span>{t("scheduler.scan_interval_value", { minutes: queue.data.scheduler_scan_interval_minutes || 60 })}</span>
            <span className="ml-auto text-accent">{t("common.edit")}</span>
          </Link>
        )}

        <section id="auth-status" className="mb-6 scroll-mt-20 rounded-md border border-border bg-surface">
          <div className="px-3 py-3">
            <h2 className="text-base font-semibold">{t("scheduler.attention_title")}</h2>
            <p className="mt-1 text-xs text-muted">{t("scheduler.attention_desc")}</p>
          </div>
          {(queue.isLoading || attention.isLoading) && <div className="h-20 animate-pulse border-t border-border bg-subtle" />}
          {(queue.error || attention.error) && <div className="border-t border-border p-3"><ErrorState message={((queue.error || attention.error) as Error).message} onRetry={() => { queue.refetch(); attention.refetch(); }} /></div>}
          {!queue.isLoading && !attention.isLoading && !queue.error && !attention.error && loop?.status !== "stalled" && visibleAttention.length === 0 && (
            <div className="border-t border-border p-3"><EmptyState title={t("scheduler.system_healthy")} description={t("scheduler.system_healthy_desc")} /></div>
          )}
          {loop?.status === "stalled" && (
            <div className="border-t border-danger/20 bg-danger-subtle px-3 py-3 text-sm text-danger">
              <strong>{t("scheduler.loop_stalled")}</strong>
              <p className="mt-1 text-xs">{t("scheduler.loop_stalled_desc")}</p>
            </div>
          )}
          {visibleAttention.map((item) => <AttentionRow key={item.source_id} item={item} />)}
        </section>

        <details className="mb-8 rounded-md border border-border bg-surface" onToggle={(event) => setPlansOpen(event.currentTarget.open)}>
          <summary className="flex min-h-11 cursor-pointer items-center justify-between gap-3 px-3 py-2 text-sm font-medium">
            <span>{t("scheduler.normal_plans")}</span>
            <span className="text-xs font-normal text-muted">{plans.data?.total ?? "—"}</span>
          </summary>
          <div className="border-t border-border">
            <div className="sticky top-0 z-10 flex flex-col gap-2 border-b border-border bg-surface/95 p-3 backdrop-blur sm:flex-row">
              <SmartSearchInput value={search} onChange={(value) => updateParams({ q: value || null, page: null })} scope="scheduler" className="w-full sm:max-w-lg" placeholder={t("scheduler.search_placeholder")} />
              <select className="select min-h-11 text-xs" value={stateFilter} onChange={(event) => updateParams({ state: event.target.value === "all" ? null : event.target.value, page: null })} aria-label={t("scheduler.filter_all")}>
                <option value="all">{t("scheduler.filter_all")}</option>
                <option value="due">{t("scheduler.filter_due")}</option>
                <option value="manual">{t("scheduler.filter_manual")}</option>
                <option value="disabled">{t("scheduler.filter_disabled")}</option>
              </select>
            </div>
            {plans.isLoading && <div className="h-24 animate-pulse bg-subtle" />}
            {plans.error && <div className="p-3"><ErrorState message={(plans.error as Error).message} onRetry={() => plans.refetch()} /></div>}
            {!plans.isLoading && !plans.error && planPage.length === 0 && <div className="p-3"><EmptyState title={t("scheduler.no_sources")} description={t("scheduler.no_sources_desc")} /></div>}
            {planPage.map((item) => <PlanRow key={item.source_id} item={item} />)}
            <Pagination page={page} pageSize={PLAN_PAGE_SIZE} total={filteredPlans.length} onPageChange={(next) => updateParams({ page: next === 1 ? null : String(next) })} />
          </div>
        </details>
    </PageShell>
  );
}

export default function SchedulerPage() {
  return (
    <PermissionGuard module="system">
      <SchedulerContent />
    </PermissionGuard>
  );
}
