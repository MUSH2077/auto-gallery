"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState } from "@/components";
import { useRouter } from "next/navigation";
import Link from "next/link";

function fmtNextSync(sub: Subscription): string {
  const last = new Date(sub.last_synced_at!);
  const next = new Date(last.getTime() + sub.sync_interval_hours * 3600 * 1000);
  const diff = next.getTime() - Date.now();
  const hours = Math.floor(diff / 3600000);
  const mins = Math.floor((diff % 3600000) / 60000);
  return hours > 0 ? `in ~${hours}h ${mins}m` : `in ~${mins}m`;
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

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("scheduler.title")} description={t("scheduler.desc")}>
        <button onClick={() => { syncNow.mutate(); }} disabled={syncNow.isPending}
          className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50">
          {syncNow.isPending ? t("scheduler.syncing") : t("scheduler.sync_now")}
        </button>
      </PageHeader>

      {/* Queue Stats */}
      <div className="grid grid-cols-3 gap-4 mb-8">
        {!queue.data ? (
          Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-12 bg-gray-100 dark:bg-slate-700 rounded" /></div>
          ))
        ) : (
          <>
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <div className="text-2xl font-bold">{Math.max(0, queue.data.default_queue)}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("scheduler.default_queue").replace("{count}", String(Math.max(0, queue.data.default_queue)))}</div>
            </div>
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <div className="text-2xl font-bold">{Math.max(0, queue.data.scheduled_queue)}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("scheduler.scheduled_queue")}</div>
            </div>
            <div className={`bg-white dark:bg-slate-800 rounded-lg shadow p-4 ${queue.data.failed_jobs > 0 ? "border-2 border-red-300" : ""}`}>
              <div className={`text-2xl font-bold ${queue.data.failed_jobs > 0 ? "text-red-600" : ""}`}>{Math.max(0, queue.data.failed_jobs)}</div>
              <div className="flex items-center justify-between mt-1">
                <span className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.failed_jobs")}</span>
                {queue.data.failed_jobs > 0 && (
                  <button onClick={() => { clearFailed.mutate(); }} disabled={clearFailed.isPending}
                    className="text-xs text-red-500 hover:text-red-700 underline">
                    {clearFailed.isPending ? "..." : t("scheduler.clear_all")}
                  </button>
                )}
              </div>
            </div>
          </>
        )}
      </div>

      {/* Scheduler Configuration */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold dark:text-white">{t("scheduler.config")}</h2>
          <Link href="/admin/settings/subscription-defaults" className="text-sm text-blue-600 hover:underline">{t("scheduler.edit_settings")}</Link>
        </div>
        {settings.isLoading ? (
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-16 bg-gray-100 dark:bg-slate-700 rounded" /></div>
        ) : settings.data ? (
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-sm">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.mode")}</div>
                <div className="font-medium dark:text-white mt-1">
                  {settings.data.subscription_defaults.schedule_mode === "fixed_time" ? t("scheduler.fixed_time") : t("scheduler.interval")}
                </div>
              </div>
              {settings.data.subscription_defaults.schedule_mode === "fixed_time" ? (
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.scheduled_times")}</div>
                  <div className="font-medium dark:text-white font-mono mt-1">{settings.data.subscription_defaults.scheduled_times || t("scheduler.not_set")}</div>
                </div>
              ) : (
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.default_interval")}</div>
                  <div className="font-medium dark:text-white mt-1">{settings.data.subscription_defaults.default_sync_interval_hours}h</div>
                </div>
              )}
              <div>
                <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.scan_frequency")}</div>
                <div className="font-medium dark:text-white mt-1">{t("scheduler.every").replace("{minutes}", String(settings.data.subscription_defaults.scheduler_scan_interval_minutes))}</div>
              </div>
              <div>
              </div>
            </div>
          </div>
        ) : null}
      </section>

      {/* Subscription Sync Schedule */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold mb-3">{t("scheduler.sync_schedule")}</h2>

        {subs.isLoading && (
          <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>
        )}
        {subs.error && <ErrorState message={(subs.error as Error).message} onRetry={() => subs.refetch()} />}
        {subs.data && !subs.data.length && (
          <EmptyState title={t("scheduler.no_subscriptions")} description={t("scheduler.no_subscriptions_desc")} />
        )}

        {subs.data && subs.data.length > 0 && (
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50">
                  <th className="text-left px-4 py-3">{t("scheduler.col_subscription")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_creator")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_auto_sync")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_interval")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_last_sync")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_next_sync")}</th>
                  <th className="text-left px-4 py-3">{t("scheduler.col_actions")}</th>
                </tr>
              </thead>
              <tbody>
                {subs.data.map((s: Subscription) => (
                  <tr key={s.id} className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
                    <td className="px-4 py-3 font-medium">{s.name || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">{getCreatorName(s.creator_id)}</td>
                    <td className="px-4 py-3">
                      {s.sync_enabled ? (
                        <span className="text-green-600 text-xs">{t("scheduler.enabled")}</span>
                      ) : (
                        <span className="text-gray-400 dark:text-gray-500 text-xs">{t("scheduler.manual_only")}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs font-mono">{s.sync_interval_hours}h</td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                      {s.last_synced_at ? new Date(s.last_synced_at).toLocaleString() : t("scheduler.never")}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                      {!s.last_synced_at ? t("scheduler.pending_first") : new Date(new Date(s.last_synced_at).getTime() + s.sync_interval_hours * 3600 * 1000) < new Date() ? t("scheduler.due_now") : fmtNextSync(s)}
                    </td>
                    <td className="px-4 py-3">
                      <button onClick={() => router.push(`/admin/subscriptions/${s.id}`)}
                        className="text-xs text-blue-600 hover:underline">{t("scheduler.manage")}</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
        <strong>{t("scheduler.how_title")}</strong> {t("scheduler.how_text")}
      </div>
    </main>
  );
}
