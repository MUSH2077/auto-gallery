"use client";
import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionDefaults, DownloadDefaults } from "@/lib/api";
import { CalendarScheduleEditor, defaultCalendarRule, PageHeader, PageShell, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";

const TIMEZONES = ["UTC", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Kolkata",
  "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "America/Sao_Paulo", "Australia/Sydney", "Pacific/Auckland"];

export default function SchedulerDefaultsPage() {
  const toast = useToast();
  const t = useT();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [subLocal, setSubLocal] = useState<SubscriptionDefaults | null>(null);
  const [dlLocal, setDlLocal] = useState<DownloadDefaults | null>(null);

  useEffect(() => {
    if (settings.data?.subscription_defaults) {
      setSubLocal((current) => current ?? { ...settings.data!.subscription_defaults });
    }
    if (settings.data?.download_defaults) {
      setDlLocal((current) => current ?? { ...settings.data!.download_defaults });
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: (data: { subscription_defaults: SubscriptionDefaults; download_defaults: DownloadDefaults }) =>
      api.updateAdminSettings(data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); toast.success({ message: t("notification.saved") }); },
  });

  const sub = subLocal || settings.data?.subscription_defaults;
  const dl = dlLocal || settings.data?.download_defaults;

  if (settings.isError) return <PageShell><ErrorState message={settings.error?.message || t("common.error")} onRetry={() => settings.refetch()} /></PageShell>;
  if (!settings.data) return <PageShell><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded-md bg-subtle dark:bg-subtle" /><div className="h-48 rounded-md bg-subtle dark:bg-subtle" /></div></PageShell>;
  if (!sub || !dl) return null;

  const setSub = (k: keyof SubscriptionDefaults, v: any) => { if (subLocal) setSubLocal({ ...subLocal, [k]: v }); };
  const setDl = (k: keyof DownloadDefaults, v: any) => { if (dlLocal) setDlLocal({ ...dlLocal, [k]: v }); };

  return (
    <PageShell>
      <PageHeader title={t("scheduler.defaults_title")} description={t("scheduler.defaults_desc")} />

      <div className="space-y-6">
        {/* ── Sync Schedule ── */}
        <div className="card p-6 text-sm">
          <h3 className="font-medium mb-4 dark:text-white">{t("subdefaults.sync_timing")}</h3>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("subdefaults.scheduler_enabled")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.scheduler_enabled.desc")}</p></div>
              <button
                type="button"
                role="switch"
                aria-checked={sub.scheduler_enabled ?? true}
                aria-label={t("subdefaults.scheduler_enabled")}
                onClick={() => setSub("scheduler_enabled", !(sub.scheduler_enabled ?? true))}
                className="relative inline-flex h-11 w-12 shrink-0 items-center justify-center rounded-md"
              >
                <span className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${(sub.scheduler_enabled ?? true) ? "bg-success" : "bg-subtle"}`}>
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${(sub.scheduler_enabled ?? true) ? "translate-x-6" : "translate-x-1"}`} />
                </span>
              </button>
            </div>

            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("subdefaults.schedule_mode")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.schedule_mode.desc")}</p></div>
              <select aria-label={t("subdefaults.schedule_mode")} value={sub.schedule_mode || "interval"} onChange={(e) => {
                const mode = e.target.value as SubscriptionDefaults["schedule_mode"];
                if (subLocal) setSubLocal({ ...subLocal, schedule_mode: mode, schedule_rule: mode === "calendar" ? (subLocal.schedule_rule || defaultCalendarRule()) : null });
              }}
                className="select px-2 py-1">
                <option value="interval">{t("subdefaults.interval")}</option>
                <option value="calendar">{t("subdefaults.calendar")}</option>
              </select>
            </div>

            {sub.schedule_mode === "interval" ? (
              <div className="flex items-center justify-between">
                <div><span className="font-medium">{t("subdefaults.sync_interval")}</span>
                  <p className="text-xs text-muted mt-1">{t("subdefaults.sync_interval.desc")}</p></div>
                <input aria-label={t("subdefaults.sync_interval")} type="number" min={1} max={168} value={sub.default_sync_interval_hours}
                  onChange={(e) => setSub("default_sync_interval_hours", parseInt(e.target.value) || 6)}
                  className="input w-20 px-2 py-1 text-center font-mono" />
              </div>
            ) : (
              <CalendarScheduleEditor value={sub.schedule_rule} onChange={(rule) => setSub("schedule_rule", rule)} />
            )}

            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("subdefaults.timezone")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.timezone.desc")}</p></div>
              <select aria-label={t("subdefaults.timezone")} value={sub.timezone || "UTC"} onChange={(e) => setSub("timezone", e.target.value)}
                className="select px-2 py-1">
                {TIMEZONES.map(tz => <option key={tz} value={tz}>{tz}</option>)}
              </select>
            </div>

            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("subdefaults.scan_interval")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.scan_interval.desc")}</p></div>
              <input aria-label={t("subdefaults.scan_interval")} type="number" min={5} max={1440} value={sub.scheduler_scan_interval_minutes}
                onChange={(e) => setSub("scheduler_scan_interval_minutes", parseInt(e.target.value) || 60)}
                className="input w-20 px-2 py-1 text-center font-mono" />
            </div>
          </div>
        </div>

        {/* ── Download Defaults ── */}
        <div className="card p-6 text-sm">
          <h3 className="font-medium mb-4 dark:text-white">{t("settings.dl_defaults")}</h3>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("dldefaults.timeout")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.timeout.desc")}</p></div>
              <div className="flex items-center gap-1"><input aria-label={t("dldefaults.timeout")} type="number" min={60} max={3600} step={60} value={dl.timeout_seconds}
                onChange={(e) => setDl("timeout_seconds", parseInt(e.target.value) || 600)}
                className="input w-20 px-2 py-1 text-center font-mono" /><span className="text-xs text-muted">{t("common.seconds_short")}</span></div>
            </div>
            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("dldefaults.retries")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.retries.desc")}</p></div>
              <input aria-label={t("dldefaults.retries")} type="number" min={0} max={10} value={dl.max_retries}
                onChange={(e) => setDl("max_retries", parseInt(e.target.value) || 3)}
                className="input w-20 px-2 py-1 text-center font-mono" />
            </div>
            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("dldefaults.backoff")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.backoff.desc")}</p></div>
              <div className="flex items-center gap-1"><input aria-label={t("dldefaults.backoff")} type="number" min={10} max={600} step={10} value={dl.retry_backoff_base_seconds}
                onChange={(e) => setDl("retry_backoff_base_seconds", parseInt(e.target.value) || 60)}
                className="input w-20 px-2 py-1 text-center font-mono" /><span className="text-xs text-muted">{t("common.seconds_short")}</span></div>
            </div>
            <div className="flex items-center justify-between">
              <div><span className="font-medium">{t("dldefaults.max_posts")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.max_posts.desc")}</p></div>
              <input aria-label={t("dldefaults.max_posts")} type="number" min={10} max={10000} step={10} value={dl.max_posts}
                onChange={(e) => setDl("max_posts", parseInt(e.target.value) || 200)}
                className="input w-20 px-2 py-1 text-center font-mono" />
            </div>
          </div>
        </div>

        {/* Save */}
        <div className="flex justify-end">
          <button onClick={() => save.mutate({ subscription_defaults: sub, download_defaults: dl })} disabled={save.isPending}
            className="btn-primary px-6">
            {save.isPending ? t("common.saving") : t("subdefaults.save")}
          </button>
        </div>
        
        {save.error && <p className="text-danger text-sm">{(save.error as Error).message}</p>}
      </div>
    </PageShell>
  );
}
