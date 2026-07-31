"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, QueueBreakdown, SchedulerDecisionItem, SEARCH_PAGE_SIZE } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { pollInterval, hasActiveTask } from "@/lib/polling";
import { scheduleModeLabel, schedulerDecisionLabel, useI18nFormat } from "@/lib/i18n-format";
import { useStaggeredEntrance } from "@/lib/motion";
import { AdaptiveToolbar, FilterBar, PageHeader, PageShell, Pagination, EmptyState, ErrorState, SourceBadge, StatCard, StatusBadge, PermissionGuard, SelectionBar, RowActionMenu, SmartSearchInput, useSearchComposer } from "@/components";
import { useToast } from "@/components/Toast";
import { useNotifications } from "@/components/NotificationCenter";
import { usePermissions } from "@/lib/usePermissions";
import { adminRoutes } from "@/lib/adminRoutes";

function decisionTone(item: SchedulerDecisionItem): string {
  if (item.due) return "border-accent/30 bg-accent-subtle text-accent dark:border-accent/30 dark:bg-accent-subtle dark:text-accent";
  if (["auth_unhealthy", "url_invalid", "unknown_provider", "scheduler_disabled"].includes(item.reason)) {
    return "border-danger/30 bg-danger-subtle text-danger dark:border-danger/30 dark:bg-danger-subtle dark:text-danger";
  }
  if (["already_attempted_in_window", "source_disabled", "subscription_sync_disabled"].includes(item.reason)) {
    return "border-warning/30 bg-warning-subtle text-warning dark:bg-warning-subtle dark:text-warning";
  }
  return "border-border bg-subtle text-muted dark:border-border dark:bg-subtle dark:text-muted";
}

function queueLabel(t: ReturnType<typeof useT>, key: string): string {
  const labels: Record<string, string> = {
    default: t("scheduler.queue_default"),
    downloads: t("scheduler.queue_downloads"),
    imports: t("scheduler.queue_imports"),
    scheduled: t("scheduler.queue_name_scheduled"),
  };
  return labels[key] || key;
}

function QueueRow({ name, stats }: { name: string; stats: QueueBreakdown }) {
  const t = useT();
  return (
    <div className="grid grid-cols-2 items-center gap-3 border-t border-border px-3 py-3 text-sm sm:grid-cols-[1.1fr_repeat(4,minmax(0,1fr))] sm:py-2">
      <div className="col-span-2 font-mono text-xs font-medium text-fg sm:col-span-1">{queueLabel(t, name)}</div>
      <div className="tabular text-xs text-muted"><span className="mr-1 text-placeholder sm:hidden">{t("scheduler.queue_queued")}:</span>{stats.queued}</div>
      <div className={`tabular text-xs ${stats.scheduled > 0 ? "font-semibold text-warning" : "text-muted"}`}><span className="mr-1 font-normal text-placeholder sm:hidden">{t("scheduler.queue_scheduled_count")}:</span>{stats.scheduled}</div>
      <div className="tabular text-xs text-muted"><span className="mr-1 text-placeholder sm:hidden">{t("scheduler.queue_started")}:</span>{stats.started}</div>
      <div className={`tabular text-xs ${stats.failed > 0 ? "font-semibold text-danger" : "text-muted"}`}><span className="mr-1 font-normal text-placeholder sm:hidden">{t("scheduler.queue_failed")}:</span>{stats.failed}</div>
    </div>
  );
}

function AuthStatusSection({ enabled }: { enabled: boolean }) {
  const t = useT();
  const fmt = useI18nFormat();
  const auth = useQuery({
    queryKey: queryKeys.admin.authStatus,
    queryFn: api.getAuthStatus,
    enabled,
    refetchInterval: enabled ? 30000 : false,
  });

  if (!enabled) return null;

  return (
    <section id="auth-status" aria-labelledby="auth-status-heading" className="mb-6 scroll-mt-20">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 id="auth-status-heading" className="text-base font-semibold">{t("auth.title")}</h2>
          <p className="mt-1 text-sm text-muted">{t("auth.desc")}</p>
        </div>
        <button
          type="button"
          onClick={() => auth.refetch()}
          disabled={auth.isFetching}
          className="btn-ghost min-h-11 self-start px-4 sm:self-auto"
        >
          {auth.isFetching ? t("common.loading") : t("scheduler.refresh")}
        </button>
      </div>

      {auth.isLoading && (
        <div aria-hidden="true" className="space-y-2">
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="h-16 animate-pulse rounded-md bg-subtle" />
          ))}
        </div>
      )}
      {auth.error && <ErrorState message={(auth.error as Error).message} onRetry={() => auth.refetch()} />}

      {auth.data && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label={t("auth.total")} value={auth.data.summary.total} />
            <StatCard label={t("auth.healthy")} value={auth.data.summary.healthy} tone="success" />
            <StatCard label={t("auth.unhealthy")} value={auth.data.summary.unhealthy} tone={auth.data.summary.unhealthy > 0 ? "danger" : "neutral"} />
            <StatCard label={t("auth.unknown")} value={auth.data.summary.unknown} />
          </div>

          {auth.data.sources.length === 0 ? (
            <EmptyState title={t("auth.no_sources")} description={t("auth.no_sources_desc")} />
          ) : (
            <div className="table-shell divide-y divide-border">
              {auth.data.sources.map((source) => (
                <div key={source.id} className="flex min-w-0 flex-col gap-3 px-4 py-3 text-sm sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex min-w-0 items-start gap-3">
                    <SourceBadge source={source.source} />
                    <div className="min-w-0">
                      <Link href={adminRoutes.creator(source.creator.id)} className="font-medium text-accent hover:underline">
                        {source.creator.display_name || source.creator.name}
                      </Link>
                      <div className="mt-0.5 break-all font-mono text-xs text-muted">{source.source_url}</div>
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
                    {!source.is_enabled && <span className="badge">{t("auth.disabled")}</span>}
                    {source.auth_healthy === true && (
                      <span className="inline-flex items-center gap-1 rounded bg-success-subtle px-2 py-1 text-xs text-success">
                        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-success" />{t("auth.healthy")}
                      </span>
                    )}
                    {source.auth_healthy === false && (
                      <span className="inline-flex items-center gap-1 rounded bg-danger-subtle px-2 py-1 text-xs text-danger">
                        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-danger" />{t("auth.unhealthy")}
                      </span>
                    )}
                    {source.auth_healthy === null && (
                      <span className="badge inline-flex items-center gap-1">
                        <span aria-hidden="true" className="h-2 w-2 rounded-full bg-muted" />{t("auth.unknown")}
                      </span>
                    )}
                    {source.last_successful_auth && (
                      <span className="text-xs text-muted">{t("auth.last_success")} {fmt.dateTime(source.last_successful_auth)}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          <p className="mt-4 rounded-md border border-accent/20 bg-accent-subtle p-4 text-sm text-accent">
            {t("auth.how")}
          </p>
        </>
      )}
    </section>
  );
}

function AdminOperationsSection() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const notify = useNotifications();
  const { has } = usePermissions();

  const ops = useQuery({
    queryKey: [...queryKeys.tasks.all, "admin", "scheduler"],
    queryFn: () => api.listTasks({ kind: "admin", limit: 10 }),
    refetchInterval: (query) => pollInterval(hasActiveTask((query.state.data as { items?: { status?: string | null }[] } | undefined)?.items)),
  });

  const rebuild = useMutation({
    mutationFn: () => api.rebuildLibrary(),
    onSuccess: (d) => {
      toast.success(d.message || t("scheduler.rebuild_enqueued"));
      notify.startOperationJob(d.job_id, "admin-rebuild", t("scheduler.rebuild_library"));
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      ops.refetch();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const activeOps = ops.data?.items || [];
  const operationEntrance = useStaggeredEntrance(activeOps.map((operation) => operation.id));

  return (
    <section className="card mb-6 p-4">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h2 className="text-base font-semibold">{t("scheduler.admin_operations")}</h2>
        <div className="flex flex-wrap gap-2">
          {has("system") && (
            <button onClick={() => rebuild.mutate()} disabled={rebuild.isPending}
              className="btn-ghost px-3 py-1.5 text-xs">
              {rebuild.isPending ? t("scheduler.rebuilding") : t("scheduler.rebuild_library")}
            </button>
          )}
          <button onClick={() => ops.refetch()} className="btn-ghost px-2 py-1 text-xs">
            {t("scheduler.refresh")}
          </button>
        </div>
      </div>

      {activeOps.length === 0 && (
        <p className="text-sm text-muted">{t("scheduler.no_active_operations")}</p>
      )}

      {activeOps.length > 0 && (
        <div className="space-y-2">
          {activeOps.map((op, index) => {
            const entrance = operationEntrance(op.id, index);
            return (
            <div key={op.id} className={`${entrance.className} flex items-center gap-3 rounded-md border border-border bg-subtle px-3 py-2 dark:border-border dark:bg-subtle`} style={entrance.style}>
              <StatusBadge status={op.status === "running" ? "downloading" : op.status === "enqueued" ? "pending" : op.status === "complete" ? "complete" : "failed"} />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">
                  {op.operation_type === "admin-rebuild" ? t("scheduler.rebuild_library") :
                   op.operation_type === "admin-clear" ? t("datamgmt.clear_all") :
                   op.operation_type === "admin-disk-import" ? t("datamgmt.disk_import") :
                   op.operation_type === "subscription-sync-batch" ? t("scheduler.subscription_sync_batch") :
                   op.operation_type}
                </div>
                {op.progress_data && (
                  <div className="text-xs text-muted">{op.progress_data.label || op.progress_stage}</div>
                )}
              </div>
              {op.error_log && <div className="text-xs text-danger dark:text-danger truncate max-w-[200px]">{op.error_log}</div>}
              <Link href={`/admin/jobs?tab=admin&task=${op.id}`} className="text-xs text-accent hover:underline dark:text-accent">
                {op.id.slice(0, 8)}
              </Link>
            </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default function SchedulerPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const qc = useQueryClient();
  const { has } = usePermissions();
  const canManageTasks = has("tasks");
  const canViewAuth = has("system");
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const search = sp.get("q") || "";
  const rawPage = Number.parseInt(sp.get("page") || "1", 10);
  const page = Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1;
  const searchOffset = (page - 1) * SEARCH_PAGE_SIZE;

  useEffect(() => {
    if (!canViewAuth || window.location.hash !== "#auth-status") return;
    let secondFrame = 0;
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        document.getElementById("auth-status")?.scrollIntoView({ block: "start" });
      });
    });
    return () => {
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame) window.cancelAnimationFrame(secondFrame);
    };
  }, [canViewAuth]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectAll, setSelectAll] = useState(false);

  const updateParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(sp.toString());
    Object.entries(updates).forEach(([key, value]) => {
      if (!value) next.delete(key); else next.set(key, value);
    });
    router.replace(next.toString() ? `${pathname}?${next.toString()}` : pathname, { scroll: false });
  };

  const queue = useQuery({ queryKey: ["queue-stats"], queryFn: api.queueStats, enabled: canManageTasks, refetchInterval: canManageTasks ? 15000 : false });
  const decisions = useQuery({ queryKey: queryKeys.schedulerDecisions, queryFn: api.schedulerDecisions, enabled: canManageTasks, refetchInterval: canManageTasks ? 15000 : false });
  const searchedDecisions = useQuery({
    queryKey: ["search", "scheduler", search, searchOffset, SEARCH_PAGE_SIZE],
    queryFn: () => api.search(search, searchOffset, SEARCH_PAGE_SIZE, "scheduler"),
    enabled: canManageTasks,
    refetchInterval: canManageTasks ? 15000 : false,
  });
  const filterComposer = useSearchComposer({
    value: search,
    scope: "scheduler",
    onChange: (query) => updateParams({ q: query || null, page: null }),
  });

  const syncNow = useMutation({
    mutationFn: () => api.triggerSyncNow("force_eligible"),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["queue-stats"] });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      toast.success({
        message: t("scheduler.sync_batch_result", { enqueued: data.enqueued_count, skipped: data.skipped_count }),
        action: { label: t("jobs.open_task"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${data.task_id}`) },
      });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const runDueScan = useMutation({
    mutationFn: () => api.triggerSyncNow("due_scan"),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["queue-stats"] });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      toast.info({
        message: t("scheduler.scan_result", { enqueued: data.enqueued_count, skipped: data.skipped_count }),
        action: { label: t("jobs.open_task"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${data.task_id}`) },
      });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  const clearFailed = useMutation({
    mutationFn: () => api.clearFailedJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["queue-stats"] }),
  });
  const syncRepo = useMutation({
    mutationFn: (id: string) => api.syncRepository(id),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["queue-stats"] });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
      if (data.status === "enqueued") {
        toast.success(data.message || t("repo_detail.sync_queued"));
      } else {
        const reason = typeof data.reason === "object" ? data.reason?.message : data.reason;
        toast.warning(reason || data.message || t("subscriptions.sync_no_jobs"));
      }
    },
    onError: (e: Error) => toast.error(e.message),
  });

  // Batch enable/disable sources
  const batchToggleSources = useMutation({
    mutationFn: async ({ ids, enabled }: { ids: string[]; enabled: boolean }) => {
      let ok = 0;
      let fail = 0;
      for (const sourceId of ids) {
        const item = items.find((it) => it.source_id === sourceId);
        if (!item) continue;
        try {
          await api.updateSubscriptionSource(item.subscription_id, sourceId, { is_enabled: enabled });
          ok++;
        } catch {
          fail++;
        }
      }
      return { ok, fail };
    },
    onSuccess: (result) => {
      toast.info(t("scheduler.batch_toggle_result", { ok: result.ok, fail: result.fail }));
      setSelected(new Set());
      setSelectAll(false);
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    },
  });

  const items = decisions.data?.items || [];
  const schedulerGroup = searchedDecisions.data?.groups.scheduler;
  const filteredItems = schedulerGroup?.items || [];
  const visibleIdsKey = filteredItems.map((item) => item.source_id).join("|");
  const selectedModes = new Set(
    searchedDecisions.data?.parsed.tokens
      .filter((token) => token.kind === "qualifier" && token.key === "is" && !token.negated)
      .map((token) => token.value) || [],
  );
  const decisionEntrance = useStaggeredEntrance(filteredItems.map((item) => item.source_id));
  const queueRows = queue.data?.queues ? Object.entries(queue.data.queues) : [];
  const queueEntrance = useStaggeredEntrance(queueRows.map(([name]) => name));
  const summary = useMemo(() => ({
    due: items.filter((item) => item.due).length,
    blocked: items.filter((item) => ["auth_unhealthy", "url_invalid", "unknown_provider"].includes(item.reason)).length,
    manual: items.filter((item) => item.reason === "manual_mode").length,
    waiting: items.filter((item) => !item.due && item.reason === "interval_not_due").length,
  }), [items]);
  const filterOptions = [
    ["", t("scheduler.filter_all")],
    ["due", t("scheduler.filter_due")],
    ["blocked", t("scheduler.filter_blocked")],
    ["waiting", t("scheduler.filter_waiting")],
    ["manual", t("scheduler.filter_manual")],
    ["disabled", t("scheduler.filter_disabled")],
  ];
  const lastUpdated = Math.max(queue.dataUpdatedAt || 0, decisions.dataUpdatedAt || 0, searchedDecisions.dataUpdatedAt || 0);
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["queue-stats"] });
    qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    qc.invalidateQueries({ queryKey: ["search", "scheduler"] });
  };

  useEffect(() => {
    const visibleIds = new Set(visibleIdsKey ? visibleIdsKey.split("|") : []);
    setSelected((current) => {
      const next = new Set(Array.from(current).filter((id) => visibleIds.has(id)));
      return next.size === current.size ? current : next;
    });
    setSelectAll(false);
  }, [visibleIdsKey]);

  const handleSelectAll = () => {
    if (selectAll) { setSelected(new Set()); setSelectAll(false); return; }
    setSelected(new Set(filteredItems.map((it) => it.source_id)));
    setSelectAll(true);
  };
  const toggleSelect = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) { next.delete(id); setSelectAll(false); } else next.add(id);
    setSelected(next);
  };
  const handleBatchToggle = (enabled: boolean) => {
    if (selected.size === 0) return;
    batchToggleSources.mutate({ ids: Array.from(selected), enabled });
  };

  return (
    <PermissionGuard anyOf={["tasks", "system"]}>
    <PageShell>
      <PageHeader title={t("scheduler.title")} description={t("scheduler.explain_desc")} />
      {canManageTasks && (
      <>
      <div data-page-primary-content>
        <AdaptiveToolbar
          className="mb-4"
          leading={
            <>
              <button onClick={refreshAll} className="btn-ghost px-5 py-2.5">
                {t("scheduler.refresh")}
              </button>
              <button onClick={() => runDueScan.mutate()} disabled={runDueScan.isPending} className="btn-ghost px-5 py-2.5">
                {runDueScan.isPending ? t("scheduler.scanning") : t("scheduler.run_due_scan")}
              </button>
              <button onClick={() => syncNow.mutate()} disabled={syncNow.isPending} className="btn-primary px-5 py-2.5">
                {syncNow.isPending ? t("scheduler.syncing") : t("scheduler.sync_eligible_now")}
              </button>
              {queue.data && queue.data.failed_jobs > 0 && (
                <button onClick={() => clearFailed.mutate()} disabled={clearFailed.isPending} className="btn-danger px-5 py-2.5">
                  {clearFailed.isPending ? "..." : t("scheduler.clear_all")}
                </button>
              )}
            </>
          }
        />
      </div>

      <FilterBar meta={t("scheduler.last_updated", { time: lastUpdated ? fmt.time(new Date(lastUpdated).toISOString()) : "—" })}>
        <SmartSearchInput
          value={search}
          onChange={(query) => updateParams({ q: query || null, page: null })}
          scope="scheduler"
          className="w-full sm:min-w-[320px] sm:max-w-xl"
          placeholder={t("scheduler.search_placeholder")}
        />
        <div className="segmented-control flex-wrap">
          {filterOptions.map(([key, label]) => (
            <button
              key={key}
              onClick={() => filterComposer.mutate({
                key: "is",
                value: key || null,
                operation: "replace-group",
                replace_values: ["due", "blocked", "waiting", "manual", "disabled"],
              })}
              className={`segment ${key ? selectedModes.has(key) : selectedModes.size === 0 ? "segment-active" : ""}`}
            >
              {label}
            </button>
          ))}
        </div>
      </FilterBar>

      {queue.data && (
        <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard label={t("scheduler.queued")} value={queue.data.default_queue} sub={t("scheduler.default_queue_short")} />
          <StatCard label={t("scheduler.scheduled")} value={queue.data.scheduled_queue} sub={queue.data.next_sync_scan_at ? fmt.dateTime(queue.data.next_sync_scan_at) : t("scheduler.no_scan")} />
          <StatCard label={t("dashboard.failed")} value={queue.data.failed_jobs} tone={queue.data.failed_jobs > 0 ? "danger" : "neutral"} />
          <StatCard label={t("dashboard.auto_sync")} value={queue.data.scheduler_enabled === false ? t("common.off") : t("common.on")} tone={queue.data.scheduler_enabled === false ? "danger" : "neutral"} sub={`${scheduleModeLabel(t, queue.data.scheduler_mode)} · ${queue.data.scheduler_timezone || "UTC"}`} />
        </div>
      )}

      {queue.data && (
        <section className="card mb-6 p-4">
          <h2 className="mb-3 text-base font-semibold">{t("scheduler.config_snapshot")}</h2>
          <div className="grid gap-3 text-sm md:grid-cols-4">
            <div><div className="text-xs text-muted">{t("scheduler.mode")}</div><div className="mt-1 font-medium">{scheduleModeLabel(t, queue.data.scheduler_mode)}</div></div>
            <div><div className="text-xs text-muted">{t("scheduler.timezone")}</div><div className="mt-1 font-medium">{queue.data.scheduler_timezone || "UTC"}</div></div>
            <div><div className="text-xs text-muted">{t("scheduler.fixed_times")}</div><div className="mt-1 font-mono text-xs">{queue.data.scheduled_times || "—"}</div></div>
            <div><div className="text-xs text-muted">{t("scheduler.scan_interval")}</div><div className="mt-1 font-medium">{t("scheduler.scan_interval_value", { minutes: queue.data.scheduler_scan_interval_minutes || 60 })}</div></div>
          </div>
        </section>
      )}

      {queueRows.length > 0 && (
        <section className="card mb-6 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <h2 className="text-base font-semibold">{t("scheduler.queue_breakdown")}</h2>
              <p className="mt-1 text-xs text-muted">{t("scheduler.queue_breakdown_desc")}</p>
            </div>
          </div>
          <div className="hidden grid-cols-[1.1fr_repeat(4,minmax(0,1fr))] gap-3 bg-subtle px-3 py-2 text-xs font-semibold uppercase text-muted sm:grid">
            <span>{t("scheduler.queue_name")}</span>
            <span>{t("scheduler.queue_queued")}</span>
            <span>{t("scheduler.queue_scheduled_count")}</span>
            <span>{t("scheduler.queue_started")}</span>
            <span>{t("scheduler.queue_failed")}</span>
          </div>
          {queueRows.map(([name, stats], index) => {
            const entrance = queueEntrance(name, index);
            return (
              <div key={name} className={entrance.className} style={entrance.style}>
                <QueueRow name={name} stats={stats} />
              </div>
            );
          })}
          {queue.data?.queues && (
            <div className="flex flex-wrap gap-2 border-t border-border px-4 py-3 text-xs dark:border-border">
              {(["downloads", "imports"] as const).map((name) => {
                const stats = queue.data?.queues?.[name];
                if (!stats || (!stats.failed && !stats.scheduled && !stats.started)) return null;
                return (
                  <Link
                    key={name}
                    href={`/admin/jobs?tab=${name === "imports" ? "imports" : "downloads"}${stats.failed ? "&q=status%3Afailed" : ""}`}
                    className="rounded-full border border-warning/30 bg-warning-subtle px-2 py-1 text-warning hover:underline dark:bg-warning-subtle dark:text-warning"
                  >
                    {t("scheduler.queue_attention", { queue: name, failed: stats.failed, scheduled: stats.scheduled, started: stats.started })}
                  </Link>
                );
              })}
            </div>
          )}
        </section>
      )}

      </>
      )}

      <AuthStatusSection enabled={canViewAuth} />

      {canManageTasks && (
      <>
      <AdminOperationsSection />

      <section>
        <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="text-base font-semibold">{t("scheduler.decision_snapshot")}</h2>
            <p className="mt-1 text-sm text-muted">{t("scheduler.decision_desc")}</p>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="badge">{t("scheduler.legend_due", { count: summary.due })}</span>
            <span className="badge">{t("scheduler.legend_waiting", { count: summary.waiting })}</span>
            <span className="badge">{t("scheduler.legend_manual", { count: summary.manual })}</span>
            <span className="badge">{t("scheduler.legend_blocked", { count: summary.blocked })}</span>
          </div>
        </div>

        {/* Batch action bar */}
        <SelectionBar
          count={selected.size}
          label={t("common.selected_count", { count: selected.size })}
          clearLabel={t("common.deselect_all")}
          onClear={() => { setSelected(new Set()); setSelectAll(false); }}
        >
            {has("subscriptions") && (
              <>
                <button
                  onClick={() => handleBatchToggle(false)}
                  disabled={batchToggleSources.isPending}
                  className="rounded px-3 py-1 text-xs font-medium bg-danger text-white hover:bg-danger disabled:opacity-50"
                >
                  {batchToggleSources.isPending ? "..." : t("scheduler.batch_disable_sync")}
                </button>
                <button
                  onClick={() => handleBatchToggle(true)}
                  disabled={batchToggleSources.isPending}
                  className="rounded px-3 py-1 text-xs font-medium bg-success text-white hover:bg-[#116329] disabled:opacity-50"
                >
                  {batchToggleSources.isPending ? "..." : t("scheduler.batch_enable_sync")}
                </button>
              </>
            )}
        </SelectionBar>

        {searchedDecisions.isLoading && <div className="space-y-2">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-20 animate-pulse rounded-md bg-subtle dark:bg-subtle" />)}</div>}
        {searchedDecisions.error && <ErrorState message={(searchedDecisions.error as Error).message} onRetry={() => searchedDecisions.refetch()} />}
        {searchedDecisions.data && filteredItems.length === 0 && <EmptyState title={t("scheduler.no_sources")} description={t("scheduler.no_sources_desc")} />}
        {filteredItems.length > 0 && (
          <div className="table-shell overflow-hidden">
            <div className="hidden grid-cols-[auto_1.2fr_1fr_1fr_1fr_1fr] gap-3 border-b border-border bg-subtle px-4 py-2 text-xs font-semibold uppercase text-muted lg:grid">
              <span className="flex items-center">
                <input type="checkbox" checked={selectAll} onChange={handleSelectAll} className="w-4 h-4 rounded border-border" title={t("common.select_all")} />
              </span>
              <span>{t("scheduler.col_creator_source")}</span>
              <span>{t("scheduler.col_decision")}</span>
              <span>{t("scheduler.col_mode")}</span>
              <span>{t("scheduler.col_next_window")}</span>
              <span>{t("scheduler.col_last_state")}</span>
            </div>
            <div className="divide-y divide-border dark:divide-border">
              {filteredItems.map((item, index) => {
                const entrance = decisionEntrance(item.source_id, index);
                return (
                <div key={item.source_id} className={`${entrance.className} grid grid-cols-1 gap-3 px-4 py-3 text-sm hover:bg-subtle dark:hover:bg-subtle lg:grid-cols-[auto_1.2fr_1fr_1fr_1fr_1fr]`} style={entrance.style}>
                  <div className="flex items-center">
                    <input type="checkbox" checked={selected.has(item.source_id)} onChange={() => toggleSelect(item.source_id)} className="w-4 h-4 rounded border-border" onClick={(e) => e.stopPropagation()} />
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <SourceBadge source={item.source} />
                      <Link href={`/admin/creators/${item.creator_id}`} className="truncate font-medium text-accent hover:underline dark:text-accent">{item.creator_name}</Link>
                    </div>
                    <div className="mt-1 truncate font-mono text-xs text-muted" title={item.source_url || ""}>{item.source_url || t("scheduler.no_url")}</div>
                  </div>
                  <div>
                    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${decisionTone(item)}`}>{schedulerDecisionLabel(t, item.reason, item.due)}</span>
                    <div className="mt-1 text-xs text-muted">
                      {item.auth_healthy ? t("scheduler.auth_ok") : t("scheduler.auth_issue")} · {item.url_valid ? t("scheduler.url_valid") : t("scheduler.url_invalid")}
                    </div>
                  </div>
                  <div className="text-xs text-muted">
                    <div className="font-medium text-fg">{scheduleModeLabel(t, item.effective_mode)}</div>
                    <div>{item.effective_mode === "fixed_time" ? (item.scheduled_times || "—") : t("scheduler.interval_value", { hours: item.sync_interval_hours })}</div>
                  </div>
                  <div className="text-xs text-muted">
                    <div>{fmt.dateTime(item.next_due_at)}</div>
                    {item.window_start && <div className="mt-1">{t("scheduler.window", { start: fmt.dateTime(item.window_start), end: fmt.dateTime(item.window_end) })}</div>}
                  </div>
                  <div className="text-xs text-muted">
                    <div>{t("scheduler.synced_at", { time: fmt.dateTime(item.last_synced_at) })}</div>
                    <div className="mt-1">{t("scheduler.attempted_at", { time: fmt.dateTime(item.last_attempted_at) })}</div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      {has("library") && (
                        <button onClick={() => syncRepo.mutate(item.source_id)} disabled={syncRepo.isPending} className="btn-ghost text-xs">{t("scheduler.sync_this_repo")}</button>
                      )}
                      <RowActionMenu
                        label={t("common.more_actions")}
                        items={[
                          { label: t("scheduler.open_repository"), href: adminRoutes.repository(item.source_id) },
                          { label: t("scheduler.open_jobs"), href: `/admin/jobs?tab=downloads&q=${encodeURIComponent(`kind:download repo:${item.source_id}`)}` },
                          { label: t("scheduler.manage"), href: `/admin/subscriptions/${item.subscription_id}` },
                        ]}
                      />
                    </div>
                  </div>
                </div>
                );
              })}
            </div>
          </div>
        )}
        {searchedDecisions.data ? (
          <Pagination
            page={page}
            pageSize={SEARCH_PAGE_SIZE}
            total={schedulerGroup?.total || 0}
            onPageChange={(nextPage) => updateParams({ page: nextPage === 1 ? null : String(nextPage) })}
          />
        ) : null}
      </section>
      </>
      )}
    </PageShell>
    </PermissionGuard>
  );
}
