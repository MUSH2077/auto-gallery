"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, SubscriptionDefaults } from "@/lib/api";
import { CalendarScheduleEditor, defaultCalendarRule, PageHeader, PageShell, ErrorState, PermissionGuard } from "@/components";
import { useT } from "@/lib/i18n";

function SubscriptionDefaultsContent() {
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
      <PageShell>
        <ErrorState message={settings.error?.message || t("subdefaults.failed")} onRetry={() => settings.refetch()} />
      </PageShell>
    );
  }

  if (!settings.data) {
    return (
      <PageShell>
        <div className="animate-pulse space-y-4">
          <div className="h-8 rounded-md bg-subtle dark:bg-subtle w-1/3" />
          <div className="h-48 rounded-md bg-subtle dark:bg-subtle" />
        </div>
      </PageShell>
    );
  }

  if (!local && settings.data.subscription_defaults) {
    setLocal({ ...settings.data.subscription_defaults });
  }

  return (
    <PageShell>
      <PageHeader title={t("subdefaults.title")} description={t("subdefaults.desc")} />

      {!current ? null : (
        <>
          <div className="card p-6 space-y-5 text-sm">
            <h4 className="font-medium text-fg border-b border-border pb-2">{t("subdefaults.sync_timing")}</h4>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("subdefaults.scheduler_enabled")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.scheduler_enabled.desc")}</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={current.scheduler_enabled ?? true}
                aria-label={t("subdefaults.scheduler_enabled")}
                onClick={() => setLocal({ ...current, scheduler_enabled: !(current.scheduler_enabled ?? true) })}
                className="relative inline-flex h-11 w-12 items-center justify-center rounded-md"
              >
                <span className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${(current.scheduler_enabled ?? true) ? "bg-success" : "bg-subtle"}`}>
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${(current.scheduler_enabled ?? true) ? "translate-x-6" : "translate-x-1"}`} />
                </span>
              </button>
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("subdefaults.schedule_mode")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.schedule_mode.desc")}</p>
              </div>
              <select
                aria-label={t("subdefaults.schedule_mode")}
                value={current.schedule_mode || "interval"}
                onChange={(e) => {
                  const mode = e.target.value as SubscriptionDefaults["schedule_mode"];
                  setLocal({ ...current, schedule_mode: mode, schedule_rule: mode === "calendar" ? (current.schedule_rule || defaultCalendarRule()) : null });
                }}
                className="select px-2 py-1"
              >
                <option value="interval">{t("subdefaults.interval")}</option>
                <option value="calendar">{t("subdefaults.calendar")}</option>
              </select>
            </div>

            {current.schedule_mode === "interval" ? (
              <div className="flex items-center justify-between py-3 border-b border-border">
                <div>
                  <span className="font-medium">{t("subdefaults.sync_interval")}</span>
                  <p className="text-xs text-muted mt-1">{t("subdefaults.sync_interval.desc")}</p>
                </div>
                <input type="number" min={1} max={168}
                  aria-label={t("subdefaults.sync_interval")}
                  value={current.default_sync_interval_hours}
                  onChange={(e) => setLocal({ ...current, default_sync_interval_hours: parseInt(e.target.value) || 6 })}
                  className="input w-20 px-2 py-1 text-center font-mono"
                />
              </div>
            ) : (
              <div className="py-3 border-b border-border"><CalendarScheduleEditor value={current.schedule_rule} onChange={(rule) => setLocal({ ...current, schedule_rule: rule })} /></div>
            )}

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("subdefaults.timezone")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.timezone.desc")}</p>
              </div>
              <select value={current.timezone || "UTC"} onChange={(e) => setLocal({ ...current, timezone: e.target.value })}
                aria-label={t("subdefaults.timezone")}
                className="select px-2 py-1">
                {["UTC", "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Kolkata",
                  "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
                  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                  "America/Sao_Paulo", "Australia/Sydney", "Pacific/Auckland"].map(tz => (
                  <option key={tz} value={tz}>{tz}</option>
                ))}
              </select>
            </div>

            <h4 className="font-medium text-fg border-b border-border pb-2 pt-2">{t("subdefaults.scheduler")}</h4>
            <div className="flex items-center justify-between py-3 border-b border-border">
              <div><span className="font-medium">{t("subdefaults.scan_interval")}</span>
                <p className="text-xs text-muted mt-1">{t("subdefaults.scan_interval.desc")}</p>
              </div>
              <input type="number" min={5} max={1440}
                aria-label={t("subdefaults.scan_interval")}
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
          {save.isSuccess && <p className="mt-2 text-sm text-success">{t("subdefaults.saved")}</p>}
          {save.error && <p className="mt-2 text-sm text-danger">{(save.error as Error).message}</p>}
        </>
      )}
    </PageShell>
  );
}

export default function SubscriptionDefaultsPage() {
  return (
    <PermissionGuard module="system">
      <SubscriptionDefaultsContent />
    </PermissionGuard>
  );
}
