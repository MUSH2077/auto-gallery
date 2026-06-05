"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SchedulerDecisionItem } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { scheduleModeLabel, schedulerDecisionLabel, useI18nFormat } from "@/lib/i18n-format";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function decisionTone(item: SchedulerDecisionItem): string {
  if (item.due) return "border-[#0969da]/30 bg-[#ddf4ff] text-[#0969da] dark:border-[#58a6ff]/30 dark:bg-[#1f6feb26] dark:text-[#58a6ff]";
  if (["auth_unhealthy", "url_invalid", "unknown_provider", "scheduler_disabled"].includes(item.reason)) {
    return "border-[#cf222e]/30 bg-[#ffebe9] text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]";
  }
  if (["already_attempted_in_window", "source_disabled", "subscription_sync_disabled"].includes(item.reason)) {
    return "border-[#bf8700]/30 bg-[#fff8c5] text-[#9a6700] dark:bg-[#bb800926] dark:text-[#d29922]";
  }
  return "border-[#d8dee4] bg-[#f6f8fa] text-[#57606a] dark:border-[#30363d] dark:bg-[#21262d] dark:text-[#8b949e]";
}

function SummaryTile({ label, value, sub, danger }: { label: string; value: string | number; sub?: string; danger?: boolean }) {
  return (
    <div className="card p-4">
      <div className={`tabular text-2xl font-semibold ${danger ? "text-[#cf222e] dark:text-[#f85149]" : "text-[#24292f] dark:text-[#e6edf3]"}`}>{value}</div>
      <div className="mt-1 text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{label}</div>
      {sub && <div className="mt-1 text-xs text-[#8c959f] dark:text-[#6e7681]">{sub}</div>}
    </div>
  );
}

export default function SchedulerPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const qc = useQueryClient();

  const queue = useQuery({ queryKey: ["queue-stats"], queryFn: api.queueStats, refetchInterval: 15000 });
  const decisions = useQuery({ queryKey: queryKeys.schedulerDecisions, queryFn: api.schedulerDecisions, refetchInterval: 15000 });

  const syncNow = useMutation({
    mutationFn: () => api.triggerSyncNow(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["queue-stats"] });
      qc.invalidateQueries({ queryKey: queryKeys.schedulerDecisions });
    },
  });
  const clearFailed = useMutation({
    mutationFn: () => api.clearFailedJobs(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["queue-stats"] }),
  });

  const items = decisions.data?.items || [];
  const summary = useMemo(() => ({
    due: items.filter((item) => item.due).length,
    blocked: items.filter((item) => ["auth_unhealthy", "url_invalid", "unknown_provider"].includes(item.reason)).length,
    manual: items.filter((item) => item.reason === "manual_mode").length,
    waiting: items.filter((item) => !item.due && item.reason === "interval_not_due").length,
  }), [items]);

  return (
    <main className="mx-auto max-w-7xl p-6">
      <PageHeader title={t("scheduler.title")} description={t("scheduler.explain_desc")}>
        <button onClick={() => syncNow.mutate()} disabled={syncNow.isPending} className="btn-primary px-5 py-2.5">
          {syncNow.isPending ? t("scheduler.syncing") : t("scheduler.sync_now")}
        </button>
        {queue.data && queue.data.failed_jobs > 0 && (
          <button onClick={() => clearFailed.mutate()} disabled={clearFailed.isPending} className="btn-danger px-5 py-2.5">
            {clearFailed.isPending ? "..." : t("scheduler.clear_all")}
          </button>
        )}
      </PageHeader>

      {queue.data && (
        <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
          <SummaryTile label={t("scheduler.queued")} value={queue.data.default_queue} sub={t("scheduler.default_queue_short")} />
          <SummaryTile label={t("scheduler.scheduled")} value={queue.data.scheduled_queue} sub={queue.data.next_sync_scan_at ? fmt.dateTime(queue.data.next_sync_scan_at) : t("scheduler.no_scan")} />
          <SummaryTile label={t("dashboard.failed")} value={queue.data.failed_jobs} danger={queue.data.failed_jobs > 0} />
          <SummaryTile label={t("dashboard.auto_sync")} value={queue.data.scheduler_enabled === false ? t("common.off") : t("common.on")} danger={queue.data.scheduler_enabled === false} sub={`${scheduleModeLabel(t, queue.data.scheduler_mode)} · ${queue.data.scheduler_timezone || "UTC"}`} />
        </div>
      )}

      {queue.data && (
        <section className="card mb-6 p-4">
          <h2 className="mb-3 text-base font-semibold">{t("scheduler.config_snapshot")}</h2>
          <div className="grid gap-3 text-sm md:grid-cols-4">
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">{t("scheduler.mode")}</div><div className="mt-1 font-medium">{scheduleModeLabel(t, queue.data.scheduler_mode)}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">{t("scheduler.timezone")}</div><div className="mt-1 font-medium">{queue.data.scheduler_timezone || "UTC"}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">{t("scheduler.fixed_times")}</div><div className="mt-1 font-mono text-xs">{queue.data.scheduled_times || "—"}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">{t("scheduler.scan_interval")}</div><div className="mt-1 font-medium">{t("scheduler.scan_interval_value", { minutes: queue.data.scheduler_scan_interval_minutes || 60 })}</div></div>
          </div>
        </section>
      )}

      <section>
        <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="text-base font-semibold">{t("scheduler.decision_snapshot")}</h2>
            <p className="mt-1 text-sm text-[#57606a] dark:text-[#8b949e]">{t("scheduler.decision_desc")}</p>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="badge">{t("scheduler.legend_due", { count: summary.due })}</span>
            <span className="badge">{t("scheduler.legend_waiting", { count: summary.waiting })}</span>
            <span className="badge">{t("scheduler.legend_manual", { count: summary.manual })}</span>
            <span className="badge">{t("scheduler.legend_blocked", { count: summary.blocked })}</span>
          </div>
        </div>

        {decisions.isLoading && <div className="space-y-2">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-20 animate-pulse rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />)}</div>}
        {decisions.error && <ErrorState message={(decisions.error as Error).message} onRetry={() => decisions.refetch()} />}
        {decisions.data && items.length === 0 && <EmptyState title={t("scheduler.no_sources")} description={t("scheduler.no_sources_desc")} />}
        {items.length > 0 && (
          <div className="table-shell overflow-hidden">
            <div className="grid grid-cols-[1.2fr_1fr_1fr_1fr_1fr] gap-3 border-b border-[#d8dee4] bg-[#f6f8fa] px-4 py-2 text-xs font-semibold uppercase text-[#57606a] dark:border-[#30363d] dark:bg-[#21262d] dark:text-[#8b949e]">
              <span>{t("scheduler.col_creator_source")}</span>
              <span>{t("scheduler.col_decision")}</span>
              <span>{t("scheduler.col_mode")}</span>
              <span>{t("scheduler.col_next_window")}</span>
              <span>{t("scheduler.col_last_state")}</span>
            </div>
            <div className="divide-y divide-[#d8dee4] dark:divide-[#30363d]">
              {items.map((item) => (
                <div key={item.source_id} className="grid grid-cols-1 gap-3 px-4 py-3 text-sm hover:bg-[#f6f8fa] dark:hover:bg-[#21262d] lg:grid-cols-[1.2fr_1fr_1fr_1fr_1fr]">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <SourceBadge source={item.source} />
                      <Link href={`/admin/creators/${item.creator_id}`} className="truncate font-medium text-[#0969da] hover:underline dark:text-[#58a6ff]">{item.creator_name}</Link>
                    </div>
                    <div className="mt-1 truncate font-mono text-xs text-[#57606a] dark:text-[#8b949e]" title={item.source_url || ""}>{item.source_url || t("scheduler.no_url")}</div>
                  </div>
                  <div>
                    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${decisionTone(item)}`}>{schedulerDecisionLabel(t, item.reason, item.due)}</span>
                    <div className="mt-1 text-xs text-[#57606a] dark:text-[#8b949e]">
                      {item.auth_healthy ? t("scheduler.auth_ok") : t("scheduler.auth_issue")} · {item.url_valid ? t("scheduler.url_valid") : t("scheduler.url_invalid")}
                    </div>
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div className="font-medium text-[#24292f] dark:text-[#e6edf3]">{scheduleModeLabel(t, item.effective_mode)}</div>
                    <div>{item.effective_mode === "fixed_time" ? (item.scheduled_times || "—") : t("scheduler.interval_value", { hours: item.sync_interval_hours })}</div>
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div>{fmt.dateTime(item.next_due_at)}</div>
                    {item.window_start && <div className="mt-1">{t("scheduler.window", { start: fmt.dateTime(item.window_start), end: fmt.dateTime(item.window_end) })}</div>}
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div>{t("scheduler.synced_at", { time: fmt.dateTime(item.last_synced_at) })}</div>
                    <div className="mt-1">{t("scheduler.attempted_at", { time: fmt.dateTime(item.last_attempted_at) })}</div>
                    <Link href={`/admin/subscriptions/${item.subscription_id}`} className="mt-1 inline-block text-[#0969da] hover:underline dark:text-[#58a6ff]">{t("scheduler.manage")}</Link>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
