"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionDefaults } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

function ScheduledTimePicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const times = value ? value.split(",").map((t) => t.trim()).filter(Boolean) : [];

  const setTime = (idx: number, newVal: string) => {
    const updated = [...times];
    updated[idx] = newVal;
    onChange(updated.join(", "));
  };

  const addTime = () => {
    onChange([...times, "03:00:00"].join(", "));
  };

  const removeTime = (idx: number) => {
    const updated = times.filter((_, i) => i !== idx);
    onChange(updated.join(", "));
  };

  return (
    <div className="space-y-2">
      {times.length === 0 && (
        <button onClick={addTime} className="btn-ghost px-3 py-1.5 text-xs">
          + 添加时间
        </button>
      )}
      {times.map((t, i) => (
        <div key={i} className="flex items-center gap-2">
          <input
            type="time"
            step="1"
            value={t.length <= 5 ? t + ":00" : t}
            onChange={(e) => setTime(i, e.target.value)}
            className="input px-2 py-1 font-mono w-36"
          />
          <span className="text-xs text-gray-400 font-mono">{t}</span>
          <button onClick={() => removeTime(i)} className="text-red-500 hover:text-red-700 text-lg leading-none" title="Remove">×</button>
          {i === times.length - 1 && (
            <button onClick={addTime} className="text-blue-600 hover:text-blue-800 text-sm" title="Add">+</button>
          )}
        </div>
      ))}
    </div>
  );
}

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
          <div className="h-8 rounded-md bg-[#eaeef2] dark:bg-[#21262d] w-1/3" />
          <div className="h-48 rounded-md bg-[#eaeef2] dark:bg-[#21262d]" />
        </div>
      </main>
    );
  }

  if (!local && settings.data.subscription_defaults) {
    setLocal({ ...settings.data.subscription_defaults });
  }

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("subdefaults.back")}</Link>
      </div>
      <PageHeader title={t("subdefaults.title")} description={t("subdefaults.desc")} />

      {!current ? null : (
        <>
          <div className="card p-6 space-y-5 text-sm">
            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2">{t("subdefaults.sync_timing")}</h4>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("subdefaults.scheduler_enabled")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.scheduler_enabled.desc")}</p>
              </div>
              <button
                type="button"
                onClick={() => setLocal({ ...current, scheduler_enabled: !(current.scheduler_enabled ?? true) })}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${(current.scheduler_enabled ?? true) ? "bg-green-600" : "bg-gray-300 dark:bg-gray-600"}`}
              >
                <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${(current.scheduler_enabled ?? true) ? "translate-x-6" : "translate-x-1"}`} />
              </button>
            </div>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("subdefaults.schedule_mode")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.schedule_mode.desc")}</p>
              </div>
              <select
                value={current.schedule_mode || "interval"}
                onChange={(e) => setLocal({ ...current, schedule_mode: e.target.value as "interval" | "fixed_time" })}
                className="select px-2 py-1"
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
                <input type="number" min={1} max={168}
                  value={current.default_sync_interval_hours}
                  onChange={(e) => setLocal({ ...current, default_sync_interval_hours: parseInt(e.target.value) || 6 })}
                  className="input w-20 px-2 py-1 text-center font-mono"
                />
              </div>
            ) : (
              <div className="py-3 border-b dark:border-slate-700">
                <div className="mb-2">
                  <span className="font-medium">{t("subdefaults.scheduled_times")}</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.scheduled_times.desc")}</p>
                </div>
                <ScheduledTimePicker
                  value={current.scheduled_times || ""}
                  onChange={(v) => setLocal({ ...current, scheduled_times: v })}
                />
              </div>
            )}

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">{t("subdefaults.timezone")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.timezone.desc")}</p>
              </div>
              <select value={current.timezone || "UTC"} onChange={(e) => setLocal({ ...current, timezone: e.target.value })}
                className="select px-2 py-1">
                {["UTC", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Kolkata",
                  "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
                  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                  "America/Sao_Paulo", "Australia/Sydney", "Pacific/Auckland"].map(tz => (
                  <option key={tz} value={tz}>{tz}</option>
                ))}
              </select>
            </div>

            <h4 className="font-medium text-gray-700 dark:text-gray-300 border-b dark:border-slate-700 pb-2 pt-2">{t("subdefaults.scheduler")}</h4>
            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div><span className="font-medium">{t("subdefaults.scan_interval")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("subdefaults.scan_interval.desc")}</p>
              </div>
              <input type="number" min={5} max={1440}
                value={current.scheduler_scan_interval_minutes}
                onChange={(e) => setLocal({ ...current, scheduler_scan_interval_minutes: parseInt(e.target.value) || 60 })}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>
          </div>

          <div className="mt-4 flex justify-end">
            <button onClick={() => save.mutate(current)} disabled={save.isPending}
              className="btn-primary px-6">
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
