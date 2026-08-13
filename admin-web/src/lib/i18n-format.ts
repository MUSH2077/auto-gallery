"use client";

import { useMemo } from "react";
import { TFunction, useI18n } from "@/lib/i18n";

const STATUS_KEYS = new Set([
  "auto_recovering", "cancelled", "complete", "completed", "down", "downloaded",
  "downloading", "enqueued", "failed", "importing", "paused", "pending", "retrying",
  "running", "stale", "unknown", "up",
]);
const SCHEDULER_REASON_KEYS = new Set([
  "already_attempted_in_window", "already_synced_in_window", "auth_unhealthy",
  "fixed_time_window_due", "interval_due", "interval_not_due", "manual_mode",
  "fixed_time_backlog_due", "interval_backlog_due",
  "never_synced_interval", "no_decision", "outside_fixed_time_window",
  "provider_not_downloadable", "scheduler_disabled", "source_disabled",
  "subscription_inactive", "subscription_sync_disabled", "unknown_provider", "url_invalid",
]);
const SCHEDULE_MODE_KEYS = new Set(["fixed_time", "interval", "manual"]);
const USER_MODULE_KEYS = new Set(["library", "curation", "upload", "subscriptions", "tasks", "system"]);

export function useI18nFormat() {
  const { lang, t } = useI18n();
  const locale = lang === "zh" ? "zh-CN" : "en-US";

  return useMemo(() => {
    const dateTime = new Intl.DateTimeFormat(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    const date = new Intl.DateTimeFormat(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    const time = new Intl.DateTimeFormat(locale, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const number = new Intl.NumberFormat(locale);
    const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });

    const parse = (value?: string | null) => {
      if (!value) return null;
      const d = new Date(value);
      return Number.isNaN(d.getTime()) ? null : d;
    };

    return {
      lang,
      locale,
      number: (value: number, options?: Intl.NumberFormatOptions) => (
        options ? new Intl.NumberFormat(locale, options).format(value) : number.format(value)
      ),
      date: (value?: string | null) => {
        const d = parse(value);
        return d ? date.format(d) : "—";
      },
      dateTime: (value?: string | null) => {
        const d = parse(value);
        return d ? dateTime.format(d) : "—";
      },
      time: (value?: string | null) => {
        const d = parse(value);
        return d ? time.format(d) : "—";
      },
      relative: (value?: string | null, emptyKey = "common.never") => {
        const d = parse(value);
        if (!d) return t(emptyKey);
        const diffMs = d.getTime() - Date.now();
        const abs = Math.abs(diffMs);
        if (abs < 60_000) return t("common.just_now");
        const minutes = Math.round(diffMs / 60_000);
        if (Math.abs(minutes) < 60) return relative.format(minutes, "minute");
        const hours = Math.round(diffMs / 3_600_000);
        if (Math.abs(hours) < 24) return relative.format(hours, "hour");
        const days = Math.round(diffMs / 86_400_000);
        if (Math.abs(days) < 30) return relative.format(days, "day");
        return date.format(d);
      },
    };
  }, [lang, locale, t]);
}

export function statusLabel(t: TFunction, status?: string | null): string {
  const normalized = (status || "unknown").toLowerCase();
  return STATUS_KEYS.has(normalized) ? t(`status.${normalized}`) : t("common.unknown");
}

export function schedulerDecisionLabel(t: TFunction, reason?: string | null, due?: boolean): string {
  if (due) return t("scheduler.reason.due_now");
  const normalized = reason || "no_decision";
  return SCHEDULER_REASON_KEYS.has(normalized)
    ? t(`scheduler.reason.${normalized}`)
    : t("scheduler.reason.no_decision");
}

export function scheduleModeLabel(t: TFunction, mode?: string | null): string {
  const normalized = (mode || "interval").toLowerCase();
  return SCHEDULE_MODE_KEYS.has(normalized)
    ? t(`scheduler.mode.${normalized}`)
    : t("scheduler.mode.interval");
}

export function userModuleLabel(t: TFunction, module: string): string {
  return USER_MODULE_KEYS.has(module)
    ? t(`users.module_${module}`)
    : t("common.unknown");
}
