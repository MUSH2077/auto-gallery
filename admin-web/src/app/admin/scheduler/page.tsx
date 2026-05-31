"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, Subscription, SubscriptionDefaults, DownloadDefaults } from "@/lib/api";
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

function ToggleSwitch({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="relative inline-flex items-center cursor-pointer shrink-0">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="sr-only peer" />
      <div className="w-9 h-5 bg-gray-200 rounded-full peer peer-checked:bg-slate-700 dark:peer-checked:bg-blue-500 peer-focus:ring-2 peer-focus:ring-blue-300 dark:peer-focus:ring-blue-800 after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full dark:border-gray-600" />
    </label>
  );
}

const TIMEZONES = ["UTC", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Kolkata",
  "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "America/Sao_Paulo", "Australia/Sydney", "Pacific/Auckland"];

export default function SchedulerPage() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();

  const subs = useQuery({ queryKey: queryKeys.subscriptions.all, queryFn: () => api.listSubscriptions() });
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });
  const queue = useQuery({ queryKey: ["queue-stats"], queryFn: api.queueStats, refetchInterval: 15000 });
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });

  const [subLocal, setSubLocal] = useState<SubscriptionDefaults | null>(null);
  const [dlLocal, setDlLocal] = useState<DownloadDefaults | null>(null);
  const [saved, setSaved] = useState(false);

  const syncNow = useMutation({ mutationFn: () => api.triggerSyncNow(), onSuccess: () => { qc.invalidateQueries({ queryKey: ["queue-stats"] }); } });
  const clearFailed = useMutation({ mutationFn: () => api.clearFailedJobs(), onSuccess: () => { qc.invalidateQueries({ queryKey: ["queue-stats"] }); } });

  const saveSettings = useMutation({
    mutationFn: (data: { subscription_defaults: SubscriptionDefaults; download_defaults: DownloadDefaults }) =>
      api.updateAdminSettings(data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });

  // Init local state from settings
  if (!subLocal && settings.data?.subscription_defaults) setSubLocal({ ...settings.data.subscription_defaults });
  if (!dlLocal && settings.data?.download_defaults) setDlLocal({ ...settings.data.download_defaults });

  const sub = subLocal || settings.data?.subscription_defaults;
  const dl = dlLocal || settings.data?.download_defaults;

  const setSub = (key: keyof SubscriptionDefaults, val: any) => { if (sub) setSubLocal({ ...sub, [key]: val }); };
  const setDl = (key: keyof DownloadDefaults, val: any) => { if (dl) setDlLocal({ ...dl, [key]: val }); };

  const getCreatorName = (creatorId: string) => {
    const c = creators.data?.find((c: any) => c.id === creatorId);
    return c ? (c.display_name || c.name) : creatorId.slice(0, 8);
  };

  const fmtStrategy = (s: Subscription) => {
    const mode = s.schedule_mode || "inherit";
    const sysMode = settings.data?.subscription_defaults?.schedule_mode || "interval";
    if (mode === "manual") return t("subscription_detail.strategy_manual");
    if (mode === "interval") return t("subscription_detail.strategy_interval") + " · " + s.sync_interval_hours + "h";
    if (mode === "fixed_time") return t("subscription_detail.strategy_fixed_time") + " · " + (s.scheduled_times || settings.data?.subscription_defaults?.scheduled_times || "—");
    // inherit
    const displayMode = sysMode === "fixed_time" ? t("subscription_detail.strategy_fixed_time") : t("subscription_detail.strategy_interval");
    const detail = sysMode === "fixed_time"
      ? (settings.data?.subscription_defaults?.scheduled_times || "")
      : (s.sync_interval_hours + "h");
    return t("scheduler.strategy_inherit_label").replace("{mode}", displayMode) + (detail ? " · " + detail : "");
  };

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("scheduler.title")} description={t("scheduler.desc")} />

      {/* Stats Row */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        {!queue.data ? (
          Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-5 animate-pulse">
              <div className="h-10 bg-gray-100 dark:bg-slate-700 rounded" />
            </div>
          ))
        ) : (
          <>
            <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-5">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center text-blue-600 dark:text-blue-400 text-lg font-bold">
                  {Math.max(0, queue.data.default_queue)}
                </div>
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.default_queue").replace("{count}", "")}</div>
                  <div className="text-sm font-medium dark:text-white">{t("scheduler.pending")}</div>
                </div>
              </div>
            </div>
            <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-5">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center text-purple-600 dark:text-purple-400 text-lg font-bold">
                  {Math.max(0, queue.data.scheduled_queue)}
                </div>
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.scheduled_queue")}</div>
                  <div className="text-sm font-medium dark:text-white">{t("scheduler.scheduled_desc")}</div>
                </div>
              </div>
            </div>
            <div className={"bg-white dark:bg-slate-800 rounded-xl shadow-sm p-5 " + (queue.data.failed_jobs > 0 ? "ring-2 ring-red-300 dark:ring-red-800" : "")}>
              <div className="flex items-center gap-3">
                <div className={"w-10 h-10 rounded-lg flex items-center justify-center text-lg font-bold " + (queue.data.failed_jobs > 0 ? "bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400" : "bg-gray-100 dark:bg-slate-700 text-gray-400")}>
                  {Math.max(0, queue.data.failed_jobs)}
                </div>
                <div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("scheduler.failed_jobs")}</div>
                  <div className="text-sm font-medium dark:text-white">{queue.data.failed_jobs > 0 ? t("scheduler.needs_attention") : t("scheduler.all_clear")}</div>
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Quick Actions */}
      <div className="flex items-center gap-3 mb-6">
        <button onClick={() => { syncNow.mutate(); }} disabled={syncNow.isPending}
          className="px-5 py-2.5 bg-slate-900 dark:bg-slate-700 text-white rounded-lg text-sm font-medium hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 transition-colors inline-flex items-center gap-2">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 4v6h6M23 20v-6h-6"/><path d="M20.49 9A9 9 0 005.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 013.51 15"/></svg>
          {syncNow.isPending ? t("scheduler.syncing") : t("scheduler.sync_now")}
        </button>
        {queue.data && queue.data.failed_jobs > 0 && (
          <button onClick={() => { clearFailed.mutate(); }} disabled={clearFailed.isPending}
            className="px-5 py-2.5 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 disabled:opacity-50 transition-colors inline-flex items-center gap-2">
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6h14"/></svg>
            {clearFailed.isPending ? "..." : t("scheduler.clear_all")}
          </button>
        )}
      </div>

      {/* Settings Cards (2-col) */}
      {sub && dl && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          {/* Sync Schedule Settings */}
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-6">
            <h3 className="font-semibold text-sm mb-4 flex items-center gap-2 dark:text-white">
              <svg className="w-4 h-4 text-blue-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              {t("scheduler.sync_config")}
            </h3>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("subdefaults.schedule_mode")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("subdefaults.schedule_mode.desc")}</div>
                </div>
                <select value={sub.schedule_mode || "interval"}
                  onChange={(e) => setSub("schedule_mode", e.target.value)}
                  className="border rounded-lg px-3 py-1.5 text-sm dark:bg-slate-700 dark:text-white dark:border-slate-600">
                  <option value="interval">{t("subdefaults.interval")}</option>
                  <option value="fixed_time">{t("subdefaults.fixed_time")}</option>
                </select>
              </div>

              {sub.schedule_mode === "interval" ? (
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium dark:text-white">{t("subdefaults.sync_interval")}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400">{t("subdefaults.sync_interval.desc")}</div>
                  </div>
                  <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg px-1">
                    <input type="number" min={1} max={168} value={sub.default_sync_interval_hours}
                      onChange={(e) => setSub("default_sync_interval_hours", parseInt(e.target.value) || 6)}
                      className="w-14 border-0 bg-transparent px-2 py-1.5 text-sm font-mono text-center dark:text-white" />
                    <span className="text-xs text-gray-500 dark:text-gray-400 pr-2">hours</span>
                  </div>
                </div>
              ) : (
                <div>
                  <div className="text-sm font-medium mb-1.5 dark:text-white">{t("subdefaults.scheduled_times")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400 mb-2">{t("subdefaults.scheduled_times.desc")}</div>
                  <input type="text" value={sub.scheduled_times || ""}
                    onChange={(e) => setSub("scheduled_times", e.target.value)}
                    placeholder="03:00, 15:00"
                    className="w-full border rounded-lg px-3 py-1.5 text-sm font-mono dark:bg-slate-700 dark:text-white dark:border-slate-600" />
                  <p className="text-xs text-gray-400 mt-1">{t("subdefaults.scheduled_times.example")}</p>
                </div>
              )}

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("subdefaults.timezone")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("subdefaults.timezone.desc")}</div>
                </div>
                <select value={sub.timezone || "UTC"}
                  onChange={(e) => setSub("timezone", e.target.value)}
                  className="border rounded-lg px-3 py-1.5 text-sm dark:bg-slate-700 dark:text-white dark:border-slate-600">
                  {TIMEZONES.map(tz => <option key={tz} value={tz}>{tz}</option>)}
                </select>
              </div>

              <div className="flex items-center justify-between pt-1 border-t dark:border-slate-700">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("subdefaults.scan_interval")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("subdefaults.scan_interval.desc")}</div>
                </div>
                <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg px-1">
                  <input type="number" min={5} max={1440} value={sub.scheduler_scan_interval_minutes}
                    onChange={(e) => setSub("scheduler_scan_interval_minutes", parseInt(e.target.value) || 60)}
                    className="w-14 border-0 bg-transparent px-2 py-1.5 text-sm font-mono text-center dark:text-white" />
                  <span className="text-xs text-gray-500 dark:text-gray-400 pr-2">min</span>
                </div>
              </div>
            </div>
          </div>

          {/* Download Settings */}
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-6">
            <h3 className="font-semibold text-sm mb-4 flex items-center gap-2 dark:text-white">
              <svg className="w-4 h-4 text-green-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
              {t("scheduler.dl_config")}
            </h3>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("dldefaults.timeout")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("dldefaults.timeout.desc")}</div>
                </div>
                <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg px-1">
                  <input type="number" min={60} max={3600} step={60} value={dl.timeout_seconds}
                    onChange={(e) => setDl("timeout_seconds", parseInt(e.target.value) || 600)}
                    className="w-14 border-0 bg-transparent px-2 py-1.5 text-sm font-mono text-center dark:text-white" />
                  <span className="text-xs text-gray-500 dark:text-gray-400 pr-2">sec</span>
                </div>
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("dldefaults.retries")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("dldefaults.retries.desc")}</div>
                </div>
                <input type="number" min={0} max={10} value={dl.max_retries}
                  onChange={(e) => setDl("max_retries", parseInt(e.target.value) || 3)}
                  className="w-16 border rounded-lg px-2 py-1.5 text-sm font-mono text-center dark:bg-slate-700 dark:text-white dark:border-slate-600" />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("dldefaults.backoff")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("dldefaults.backoff.desc")}</div>
                </div>
                <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg px-1">
                  <input type="number" min={10} max={600} step={10} value={dl.retry_backoff_base_seconds}
                    onChange={(e) => setDl("retry_backoff_base_seconds", parseInt(e.target.value) || 60)}
                    className="w-14 border-0 bg-transparent px-2 py-1.5 text-sm font-mono text-center dark:text-white" />
                  <span className="text-xs text-gray-500 dark:text-gray-400 pr-2">sec</span>
                </div>
              </div>
              <div className="flex items-center justify-between pt-1 border-t dark:border-slate-700">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("dldefaults.max_posts")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("dldefaults.max_posts.desc")}</div>
                </div>
                <input type="number" min={10} max={10000} step={10} value={dl.max_posts}
                  onChange={(e) => setDl("max_posts", parseInt(e.target.value) || 200)}
                  className="w-20 border rounded-lg px-2 py-1.5 text-sm font-mono text-center dark:bg-slate-700 dark:text-white dark:border-slate-600" />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-medium dark:text-white">{t("dldefaults.skip_ai")}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{t("dldefaults.skip_ai.desc")}</div>
                </div>
                <ToggleSwitch checked={dl.skip_ai_generated} onChange={(v) => setDl("skip_ai_generated", v)} />
              </div>
              <div className="p-3 bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-900/50 rounded-lg text-xs text-blue-700 dark:text-blue-300">
                <strong>{t("dldefaults.schedule")}:</strong>{" "}
                {t("dldefaults.schedule.desc").replace("{base}", String(dl.retry_backoff_base_seconds)).replace("{double}", String(dl.retry_backoff_base_seconds * 2)).replace("{max}", String(dl.max_retries))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Save button */}
      {sub && dl && (
        <div className="flex justify-end mb-8">
          <button onClick={() => saveSettings.mutate({ subscription_defaults: sub, download_defaults: dl })}
            disabled={saveSettings.isPending}
            className="px-6 py-2.5 bg-slate-900 dark:bg-slate-700 text-white rounded-lg text-sm font-medium hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 transition-colors">
            {saveSettings.isPending ? t("common.saving") : t("subdefaults.save")}
          </button>
          {saved && <span className="ml-3 text-green-600 dark:text-green-400 text-sm self-center">{t("common.saved")}</span>}
          {saveSettings.error && <span className="ml-3 text-red-600 text-sm self-center">{(saveSettings.error as Error).message}</span>}
        </div>
      )}

      {/* Subscription Sync Table */}
      <section>
        <h3 className="font-semibold text-sm mb-3 flex items-center gap-2 dark:text-white">
          <svg className="w-4 h-4 text-purple-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
          {t("scheduler.sync_schedule")}
          <span className="text-xs text-gray-400 font-normal ml-1">({subs.data?.length || 0})</span>
        </h3>

        {subs.isLoading && (
          <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-14 bg-gray-100 dark:bg-slate-700 rounded-lg animate-pulse" />)}</div>
        )}
        {subs.error && <ErrorState message={(subs.error as Error).message} onRetry={() => subs.refetch()} />}
        {subs.data && !subs.data.length && (
          <EmptyState title={t("scheduler.no_subscriptions")} description={t("scheduler.no_subscriptions_desc")} />
        )}

        {subs.data && subs.data.length > 0 && (
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 dark:bg-slate-700/50 text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_subscription")}</th>
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_creator")}</th>
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_auto_sync")}</th>
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_strategy")}</th>
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_last_sync")}</th>
                  <th className="text-left px-4 py-3 font-medium">{t("scheduler.col_next_sync")}</th>
                  <th className="text-right px-4 py-3 font-medium">{t("scheduler.col_actions")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-slate-700">
                {subs.data.map((s: Subscription) => {
                  const isOverdue = s.last_synced_at && new Date(new Date(s.last_synced_at).getTime() + s.sync_interval_hours * 3600 * 1000) < new Date();
                  return (
                    <tr key={s.id} className="hover:bg-gray-50 dark:hover:bg-slate-700/50 transition-colors">
                      <td className="px-4 py-3 font-medium dark:text-white">{s.name || "—"}</td>
                      <td className="px-4 py-3 text-gray-500 dark:text-gray-400">{getCreatorName(s.creator_id)}</td>
                      <td className="px-4 py-3">
                        {s.sync_enabled ? (
                          <span className="inline-flex items-center gap-1 text-green-600 dark:text-green-400 text-xs font-medium">
                            <span className="w-1.5 h-1.5 bg-green-500 rounded-full" />
                            {t("scheduler.enabled")}
                          </span>
                        ) : (
                          <span className="text-gray-400 dark:text-gray-500 text-xs">{t("scheduler.manual_only")}</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-600 dark:text-gray-300 max-w-[180px] truncate" title={fmtStrategy(s)}>{fmtStrategy(s)}</td>
                      <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                        {s.last_synced_at ? new Date(s.last_synced_at).toLocaleString() : t("scheduler.never")}
                      </td>
                      <td className="px-4 py-3 text-xs">
                        {!s.last_synced_at ? (
                          <span className="text-blue-600 dark:text-blue-400 font-medium">{t("scheduler.pending_first")}</span>
                        ) : isOverdue ? (
                          <span className="text-red-600 dark:text-red-400 font-medium">{t("scheduler.due_now")}</span>
                        ) : (
                          <span className="text-gray-500 dark:text-gray-400">{fmtNextSync(s)}</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button onClick={() => router.push("/admin/subscriptions/" + s.id)}
                          className="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400 font-medium hover:underline">
                          {t("scheduler.manage")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-900/50 rounded-xl text-sm text-blue-800 dark:text-blue-300">
        <strong>{t("scheduler.how_title")}</strong> {t("scheduler.how_text")}
      </div>
    </main>
  );
}
