"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionDefaults } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function SubscriptionDefaultsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<SubscriptionDefaults | null>(null);

  const save = useMutation({
    mutationFn: (data: SubscriptionDefaults) => api.updateAdminSettings({ subscription_defaults: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const current = local || settings.data?.subscription_defaults;

  if (settings.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={settings.error?.message || "Failed"} onRetry={() => settings.refetch()} />
      </main>
    );
  }

  if (!settings.data) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-48 bg-gray-200 rounded" />
        </div>
      </main>
    );
  }

  if (!local && settings.data.subscription_defaults) {
    setLocal({ ...settings.data.subscription_defaults });
  }

  const setNum = (key: keyof SubscriptionDefaults, val: number) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  const setBool = (key: keyof SubscriptionDefaults, val: boolean) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Subscription Defaults" description="Default sync settings and scheduler behavior." />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5 text-sm">
            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">Sync Timing</h4>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">Schedule Mode</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Interval: sync every N hours. Fixed Time: sync at specific times of day.</p>
              </div>
              <select
                value={current.schedule_mode || "interval"}
                onChange={(e) => setLocal({ ...current, schedule_mode: e.target.value as "interval" | "fixed_time" })}
                className="border rounded px-2 py-1 text-sm dark:bg-slate-700 dark:text-white"
              >
                <option value="interval">Interval (N hours)</option>
                <option value="fixed_time">Fixed Time (HH:MM)</option>
              </select>
            </div>

            {current.schedule_mode === "interval" ? (
              <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
                <div>
                  <span className="font-medium">Default Sync Interval</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Sync each subscription when this many hours have passed since last sync.</p>
                </div>
                <input
                  type="number" min={1} max={168}
                  value={current.default_sync_interval_hours}
                  onChange={(e) => setNum("default_sync_interval_hours", parseInt(e.target.value) || 6)}
                  className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
                />
              </div>
            ) : (
              <div className="py-3 border-b dark:border-slate-700">
                <div className="mb-2">
                  <span className="font-medium">Scheduled Times</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Comma-separated HH:MM times (UTC). Sync runs at these moments each day.</p>
                </div>
                <input
                  type="text"
                  value={current.scheduled_times || ""}
                  onChange={(e) => setLocal({ ...current, scheduled_times: e.target.value })}
                  placeholder="03:00, 15:00"
                  className="w-48 border rounded px-2 py-1 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">Example: "00:00, 06:00, 12:00, 18:00" for every 6 hours at fixed moments.</p>
              </div>
            )}

            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-2">Scheduler Settings</h4>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">Scheduler Scan Interval</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">How often the scheduler wakes up to check for due subscriptions (minutes). Min 5.</p>
              </div>
              <input
                type="number" min={5} max={1440}
                value={current.scheduler_scan_interval_minutes}
                onChange={(e) => setNum("scheduler_scan_interval_minutes", parseInt(e.target.value) || 60)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>

            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-2">New Subscription Defaults</h4>

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">Default Sync Enabled</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">New subscriptions start with auto-sync enabled.</p>
              </div>
              <button
                onClick={() => setBool("default_sync_enabled", !current.default_sync_enabled)}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${
                  current.default_sync_enabled ? "bg-green-600" : "bg-gray-300"
                }`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  current.default_sync_enabled ? "translate-x-6" : "translate-x-1"
                }`} />
              </button>
            </div>
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
            >
              {save.isPending ? "Saving..." : "Save Settings"}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">Settings saved.</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}
