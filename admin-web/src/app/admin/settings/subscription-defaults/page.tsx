"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionDefaults } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

export default function SubscriptionDefaultsPage() {
  const t = useT();
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
        <ErrorState message={settings.error?.message || t("subdefaults.failed")} onRetry={() => settings.refetch()} />
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
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("subdefaults.back")}</Link>
      </div>
      <PageHeader title={t("subdefaults.title")} description={t("subdefaults.desc")} />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5 text-sm">
            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("subdefaults.sync_timing")}</h4>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("subdefaults.schedule_mode")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.schedule_mode.desc")}</p>
              </div>
              <select
                value={current.schedule_mode || "interval"}
                onChange={(e) => setLocal({ ...current, schedule_mode: e.target.value as "interval" | "fixed_time" })}
                className="border rounded px-2 py-1 text-sm dark:bg-slate-700 dark:text-white"
              >
                <option value="interval">{t("subdefaults.interval")}</option>
                <option value="fixed_time">{t("subdefaults.fixed_time")}</option>
              </select>
            </div>

            {current.schedule_mode === "interval" ? (
              <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
                <div>
                  <span className="font-medium">{t("subdefaults.sync_interval")}</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.sync_interval.desc")}</p>
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
                  <span className="font-medium">{t("subdefaults.scheduled_times")}</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.scheduled_times.desc")}</p>
                </div>
                <input
                  type="text"
                  value={current.scheduled_times || ""}
                  onChange={(e) => setLocal({ ...current, scheduled_times: e.target.value })}
                  placeholder="03:00, 15:00"
                  className="w-48 border rounded px-2 py-1 text-sm font-mono dark:bg-slate-700 dark:text-white"
                />
                <p className="text-xs text-gray-400 mt-1">{t("subdefaults.scheduled_times.example")}</p>
              </div>
            )}

            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-2">{t("subdefaults.scheduler")}</h4>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("subdefaults.scan_interval")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.scan_interval.desc")}</p>
              </div>
              <input
                type="number" min={5} max={1440}
                value={current.scheduler_scan_interval_minutes}
                onChange={(e) => setNum("scheduler_scan_interval_minutes", parseInt(e.target.value) || 60)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>

            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-2">{t("subdefaults.new_sub")}</h4>

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">{t("subdefaults.sync_enabled")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.sync_enabled.desc")}</p>
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
              {save.isPending ? t("common.saving") : t("subdefaults.save")}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">{t("subdefaults.saved")}</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}
