"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, Subscription } from "@/lib/api";
import { PageHeader, StatusBadge, EmptyState, ErrorState } from "@/components";
import { useRouter } from "next/navigation";

function fmtNextSync(sub: Subscription): string {
  if (!sub.last_synced_at) return "Pending first sync";
  const last = new Date(sub.last_synced_at);
  const next = new Date(last.getTime() + sub.sync_interval_hours * 3600 * 1000);
  if (next < new Date()) return "Due now";
  const diff = next.getTime() - Date.now();
  const hours = Math.floor(diff / 3600000);
  const mins = Math.floor((diff % 3600000) / 60000);
  return hours > 0 ? `in ~${hours}h ${mins}m` : `in ~${mins}m`;
}

export default function SchedulerPage() {
  const router = useRouter();
  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const queue = useQuery({ queryKey: ["queue-stats"], queryFn: api.queueStats, refetchInterval: 15000 });

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Scheduler" description="Subscription sync schedule and queue status" />

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
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Default Queue (jobs)</div>
            </div>
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
              <div className="text-2xl font-bold">{Math.max(0, queue.data.scheduled_queue)}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Scheduled Queue</div>
            </div>
            <div className={`bg-white rounded-lg shadow p-4 ${queue.data.failed_jobs > 0 ? "border-2 border-red-300" : ""}`}>
              <div className={`text-2xl font-bold ${queue.data.failed_jobs > 0 ? "text-red-600" : ""}`}>{Math.max(0, queue.data.failed_jobs)}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Failed Jobs</div>
            </div>
          </>
        )}
      </div>

      {/* Subscription Sync Status */}
      <section>
        <h2 className="text-lg font-semibold mb-3">Subscription Sync Schedule</h2>

        {subs.isLoading && (
          <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>
        )}
        {subs.error && <ErrorState message={(subs.error as Error).message} onRetry={() => subs.refetch()} />}
        {subs.data && !subs.data.length && (
          <EmptyState title="No subscriptions" description="Create a subscription to see sync scheduling." />
        )}

        {subs.data && subs.data.length > 0 && (
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50">
                  <th className="text-left px-4 py-3">Subscription</th>
                  <th className="text-left px-4 py-3">Creator</th>
                  <th className="text-left px-4 py-3">Auto Sync</th>
                  <th className="text-left px-4 py-3">Interval</th>
                  <th className="text-left px-4 py-3">Last Sync</th>
                  <th className="text-left px-4 py-3">Next Sync</th>
                  <th className="text-left px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {subs.data.map((s: Subscription) => (
                  <tr key={s.id} className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
                    <td className="px-4 py-3 font-medium">{s.name || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">{getCreatorName(s.creator_id)}</td>
                    <td className="px-4 py-3">
                      {s.sync_enabled ? (
                        <span className="text-green-600 text-xs">Enabled</span>
                      ) : (
                        <span className="text-gray-400 dark:text-gray-500 text-xs">Manual only</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs font-mono">{s.sync_interval_hours}h</td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                      {s.last_synced_at ? new Date(s.last_synced_at).toLocaleString() : "Never"}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">{fmtNextSync(s)}</td>
                    <td className="px-4 py-3">
                      <button onClick={() => router.push(`/admin/subscriptions/${s.id}`)}
                        className="text-xs text-blue-600 hover:underline">Manage</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
        <strong>How scheduling works:</strong> The scheduler scans active, sync-enabled subscriptions every hour.
        For each subscription source, it checks whether the configured sync interval has elapsed since the last download.
        If auth is unhealthy, the source is skipped. Failed jobs appear in the queue stats above.
      </div>
    </main>
  );
}
