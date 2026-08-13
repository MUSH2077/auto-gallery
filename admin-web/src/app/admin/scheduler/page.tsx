"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
} from "@/components";
import { useToast } from "@/components/Toast";
import { adminRoutes } from "@/lib/adminRoutes";
import { api, queryKeys, type SchedulerDecisionItem } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import {
  scheduleModeLabel,
  schedulerDecisionLabel,
  useI18nFormat,
} from "@/lib/i18n-format";

const PLAN_PAGE_SIZE = 25;

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
        <p className="font-medium text-danger">{schedulerDecisionLabel(t, item.reason, item.due)}</p>
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
      <span className="text-muted">{schedulerDecisionLabel(t, item.reason, item.due)}</span>
    </div>
  );
}

export default function SchedulerPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const toast = useToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { has } = usePermissions();
  const [plansOpen, setPlansOpen] = useState(false);

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

  const runDueScan = useMutation({
    mutationFn: () => api.triggerSyncNow("due_scan"),
    onSuccess: (data) => {
      toast.info(t("scheduler.scan_result", { enqueued: data.enqueued_count, skipped: data.skipped_count }));
      queryClient.invalidateQueries({ queryKey: queryKeys.system.queueStats });
      queryClient.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const syncEligible = useMutation({
    mutationFn: () => api.triggerSyncNow("force_eligible"),
    onSuccess: (data) => {
      toast.success(t("scheduler.sync_batch_result", { enqueued: data.enqueued_count, skipped: data.skipped_count }));
      queryClient.invalidateQueries({ queryKey: queryKeys.system.queueStats });
      queryClient.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
      queryClient.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const loop = queue.data?.scheduler_loop;
  const attentionItems = attention.data?.items || [];
  const blockedCount = attentionItems.filter((item) => !item.is_overdue).length;
  const overdueCount = attentionItems.filter((item) => item.is_overdue).length;
  const visibleAttention = !queue.data?.scheduler_enabled && attentionItems.length > 0
    ? [attentionItems[0]]
    : attentionItems;

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

  return (
    <PermissionGuard anyOf={["tasks", "system"]}>
      <PageShell>
        <PageHeader title={t("scheduler.title")} description={t("scheduler.compact_desc")} />

        <section data-page-primary-content className="mb-4 rounded-md border border-border bg-surface">
          <div className="flex flex-col gap-3 p-3 lg:flex-row lg:items-center lg:justify-between">
            <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs" aria-live="polite">
              <span className={`inline-flex min-h-7 items-center rounded-full border px-2.5 font-medium ${loopTone(loop?.status)}`}>
                {t(`scheduler.loop_${loop?.status || "unknown"}`)}
              </span>
              <span><span className="text-muted">{t("scheduler.last_scan")}</span> <strong>{fmt.dateTime(loop?.last_finished_at)}</strong></span>
              <span><span className="text-muted">{t("scheduler.next_scan")}</span> <strong>{fmt.dateTime(loop?.next_scan_at || queue.data?.next_sync_scan_at)}</strong></span>
              <span><span className="text-muted">{t("scheduler.overdue")}</span> <strong className={overdueCount ? "text-warning" : ""}>{overdueCount}</strong></span>
              <span><span className="text-muted">{t("scheduler.blocked")}</span> <strong className={blockedCount ? "text-danger" : ""}>{blockedCount}</strong></span>
            </div>
            {has("tasks") && (
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className="btn-primary min-h-11" onClick={() => runDueScan.mutate()} disabled={runDueScan.isPending}>
                  {runDueScan.isPending ? t("scheduler.scanning") : t("scheduler.run_due_scan")}
                </button>
                <RowActionMenu
                  label={t("common.more_actions")}
                  items={[
                    { label: t("scheduler.sync_eligible_now"), onSelect: () => syncEligible.mutate(), disabled: syncEligible.isPending },
                    { label: t("scheduler.defaults_title"), href: adminRoutes.settingsSection("scheduler-defaults") },
                  ]}
                />
              </div>
            )}
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
    </PermissionGuard>
  );
}
