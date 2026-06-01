"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState } from "@/components";
import { useRouter } from "next/navigation";

function fmtNextSync(sub: Subscription): string {
  const last = new Date(sub.last_synced_at!);
  const next = new Date(last.getTime() + sub.sync_interval_hours * 3600 * 1000);
  const diff = next.getTime() - Date.now();
  const hours = Math.floor(diff / 3600000);
  const mins = Math.floor((diff % 3600000) / 60000);
  return hours > 0 ? "in ~" + hours + "h " + mins + "m" : "in ~" + mins + "m";
}

export default function SchedulerPage() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();

  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const queue = useQuery({ queryKey: ["queue-stats"], queryFn: api.queueStats, refetchInterval: 15000 });
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });

  const syncNow = useMutation({ mutationFn: () => api.triggerSyncNow(), onSuccess: () => { qc.invalidateQueries({ queryKey: ["queue-stats"] }); } });
  const clearFailed = useMutation({ mutationFn: () => api.clearFailedJobs(), onSuccess: () => { qc.invalidateQueries({ queryKey: ["queue-stats"] }); } });

  const getStrategyLabel = (s: Subscription) => {
    const mode = s.schedule_mode || "inherit";
    const sysMode = settings.data?.subscription_defaults?.schedule_mode || "interval";
    if (mode === "manual") return t("subscription_detail.strategy_manual");
    if (mode === "interval") return t("subscription_detail.strategy_interval") + " · " + s.sync_interval_hours + "h";
    if (mode === "fixed_time") return t("subscription_detail.strategy_fixed_time") + " · " + (s.scheduled_times || settings.data?.subscription_defaults?.scheduled_times || "—");
    const displayMode = sysMode === "fixed_time" ? t("subscription_detail.strategy_fixed_time") : t("subscription_detail.strategy_interval");
    const detail = sysMode === "fixed_time" ? (settings.data?.subscription_defaults?.scheduled_times || "") : (s.sync_interval_hours + "h");
    return t("scheduler.strategy_inherit_label").replace("{mode}", displayMode) + (detail ? " · " + detail : "");
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("scheduler.title")} description={t("scheduler.desc")}>
        <button onClick={() => { syncNow.mutate(); }} disabled={syncNow.isPending}
          className="px-5 py-2.5 bg-slate-900 dark:bg-slate-700 text-white rounded-lg text-sm font-medium hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 transition-colors">
          {syncNow.isPending ? t("scheduler.syncing") : t("scheduler.sync_now")}
        </button>
        {queue.data && queue.data.failed_jobs > 0 && (
          <button onClick={() => { clearFailed.mutate(); }} disabled={clearFailed.isPending}
            className="px-5 py-2.5 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 disabled:opacity-50 transition-colors">
            {clearFailed.isPending ? "..." : t("scheduler.clear_all")}
          </button>
        )}
      </PageHeader>

      {/* Queue stats */}
      {queue.data && (
        <div className="grid grid-cols-3 gap-3 mb-8">
          {[{ label: "Pending", value: queue.data.default_queue, color: "text-yellow-600" },
            { label: "Scheduled", value: queue.data.scheduled_queue, color: "text-purple-600" },
            { label: "Failed", value: queue.data.failed_jobs, color: "text-red-600" },
            ].map((s) => (
            <div key={s.label} className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-4 text-center">
              <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{s.label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Subscription list */}
      <section>
        <h3 className="text-base font-semibold mb-3 dark:text-white">{t("scheduler.sync_schedule")}</h3>
        {subs.isLoading ? <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-12 bg-gray-200 dark:bg-slate-700 rounded" />)}</div> :
         subs.data && !subs.data.length ? <EmptyState title={t("scheduler.no_subscriptions")} description={t("scheduler.no_subscriptions_desc")} /> :
         subs.data && subs.data.length > 0 ? (
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-hidden">
            <div className="divide-y dark:divide-slate-700">
              {subs.data.map((s: Subscription) => {
                const creator = creators.data?.find((c) => c.id === s.creator_id);
                return (
                  <div key={s.id} className="flex items-center gap-4 px-4 py-3 text-sm hover:bg-gray-50 dark:hover:bg-slate-700/50">
                    <span className="text-xs text-gray-400 font-mono w-12">{s.id.slice(0, 6)}</span>
                    <span className="flex-1 font-medium truncate dark:text-white">{creator?.display_name || creator?.name || s.id.slice(0, 8)}</span>
                    <span className="text-xs text-gray-500 w-32 text-center">{getStrategyLabel(s)}</span>
                    {s.last_synced_at ? (
                      <span className="text-xs text-gray-400 w-20 text-right">{fmtNextSync(s)}</span>
                    ) : (
                      <span className="text-xs text-yellow-600 w-20 text-right">{t("scheduler.never")}</span>
                    )}
                    <button onClick={() => router.push(`/admin/subscriptions/${s.id}`)}
                      className="text-xs text-blue-600 hover:underline">{t("scheduler.manage")}</button>
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}
      </section>
    </main>
  );
}
