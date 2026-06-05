"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SchedulerDecisionItem } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function fmtDate(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function reasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    due_now: "Due now",
    fixed_time_window_due: "Due now",
    interval_due: "Due now",
    never_synced_interval: "Due now",
    interval_not_due: "Waiting interval",
    outside_fixed_time_window: "Before fixed-time window",
    already_attempted_in_window: "Already attempted in window",
    already_synced_in_window: "Already synced in window",
    manual_mode: "Manual",
    auth_unhealthy: "Auth unhealthy",
    scheduler_disabled: "Scheduler disabled",
    source_disabled: "Source disabled",
    subscription_inactive: "Subscription inactive",
    subscription_sync_disabled: "Subscription sync disabled",
    url_invalid: "Invalid URL",
    provider_not_downloadable: "Not downloadable",
    unknown_provider: "Unknown provider",
  };
  return labels[reason] || reason.replaceAll("_", " ");
}

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
      <PageHeader title={t("scheduler.title")} description="Explainable queue and source-level scheduler decisions.">
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
          <SummaryTile label="Queued" value={queue.data.default_queue} sub="Default queue" />
          <SummaryTile label="Scheduled" value={queue.data.scheduled_queue} sub={queue.data.next_sync_scan_at ? fmtDate(queue.data.next_sync_scan_at) : "No scan"} />
          <SummaryTile label="Failed" value={queue.data.failed_jobs} danger={queue.data.failed_jobs > 0} />
          <SummaryTile label="Auto sync" value={queue.data.scheduler_enabled === false ? "Off" : "On"} danger={queue.data.scheduler_enabled === false} sub={`${queue.data.scheduler_mode || "interval"} · ${queue.data.scheduler_timezone || "UTC"}`} />
        </div>
      )}

      {queue.data && (
        <section className="card mb-6 p-4">
          <h2 className="mb-3 text-base font-semibold">Scheduler config snapshot</h2>
          <div className="grid gap-3 text-sm md:grid-cols-4">
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">Mode</div><div className="mt-1 font-medium">{queue.data.scheduler_mode || "interval"}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">Timezone</div><div className="mt-1 font-medium">{queue.data.scheduler_timezone || "UTC"}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">Fixed times</div><div className="mt-1 font-mono text-xs">{queue.data.scheduled_times || "—"}</div></div>
            <div><div className="text-xs text-[#57606a] dark:text-[#8b949e]">Scan interval</div><div className="mt-1 font-medium">{queue.data.scheduler_scan_interval_minutes || 60}m</div></div>
          </div>
        </section>
      )}

      <section>
        <div className="mb-3 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="text-base font-semibold">Current Decision Snapshot</h2>
            <p className="mt-1 text-sm text-[#57606a] dark:text-[#8b949e]">Read-only source/repo decisions. This page never enqueues jobs by itself.</p>
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="badge">{summary.due} due</span>
            <span className="badge">{summary.waiting} waiting</span>
            <span className="badge">{summary.manual} manual</span>
            <span className="badge">{summary.blocked} blocked</span>
          </div>
        </div>

        {decisions.isLoading && <div className="space-y-2">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-20 animate-pulse rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />)}</div>}
        {decisions.error && <ErrorState message={(decisions.error as Error).message} onRetry={() => decisions.refetch()} />}
        {decisions.data && items.length === 0 && <EmptyState title="No sources" description="Create subscriptions and source URLs to see scheduler decisions." />}
        {items.length > 0 && (
          <div className="table-shell overflow-hidden">
            <div className="grid grid-cols-[1.2fr_1fr_1fr_1fr_1fr] gap-3 border-b border-[#d8dee4] bg-[#f6f8fa] px-4 py-2 text-xs font-semibold uppercase text-[#57606a] dark:border-[#30363d] dark:bg-[#21262d] dark:text-[#8b949e]">
              <span>Creator / Source</span>
              <span>Decision</span>
              <span>Mode</span>
              <span>Next / Window</span>
              <span>Last state</span>
            </div>
            <div className="divide-y divide-[#d8dee4] dark:divide-[#30363d]">
              {items.map((item) => (
                <div key={item.source_id} className="grid grid-cols-1 gap-3 px-4 py-3 text-sm hover:bg-[#f6f8fa] dark:hover:bg-[#21262d] lg:grid-cols-[1.2fr_1fr_1fr_1fr_1fr]">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <SourceBadge source={item.source} />
                      <Link href={`/admin/creators/${item.creator_id}`} className="truncate font-medium text-[#0969da] hover:underline dark:text-[#58a6ff]">{item.creator_name}</Link>
                    </div>
                    <div className="mt-1 truncate font-mono text-xs text-[#57606a] dark:text-[#8b949e]" title={item.source_url || ""}>{item.source_url || "No URL"}</div>
                  </div>
                  <div>
                    <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${decisionTone(item)}`}>{reasonLabel(item.due ? "due_now" : item.reason)}</span>
                    <div className="mt-1 text-xs text-[#57606a] dark:text-[#8b949e]">
                      {item.auth_healthy ? "Auth ok" : "Auth issue"} · {item.url_valid ? "URL valid" : "URL invalid"}
                    </div>
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div className="font-medium text-[#24292f] dark:text-[#e6edf3]">{item.effective_mode}</div>
                    <div>{item.effective_mode === "fixed_time" ? (item.scheduled_times || "—") : `${item.sync_interval_hours}h interval`}</div>
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div>{fmtDate(item.next_due_at)}</div>
                    {item.window_start && <div className="mt-1">Window {fmtDate(item.window_start)} - {fmtDate(item.window_end)}</div>}
                  </div>
                  <div className="text-xs text-[#57606a] dark:text-[#8b949e]">
                    <div>Synced {fmtDate(item.last_synced_at)}</div>
                    <div className="mt-1">Attempted {fmtDate(item.last_attempted_at)}</div>
                    <Link href={`/admin/subscriptions/${item.subscription_id}`} className="mt-1 inline-block text-[#0969da] hover:underline dark:text-[#58a6ff]">Manage</Link>
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
