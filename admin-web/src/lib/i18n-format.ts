"use client";

import { useMemo } from "react";
import { TFunction, useI18n } from "@/lib/i18n";

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
      number: (value: number) => number.format(value),
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
  const key = `status.${(status || "unknown").toLowerCase()}`;
  return t(key, status || t("common.unknown"));
}

export function schedulerDecisionLabel(t: TFunction, reason?: string | null, due?: boolean): string {
  const key = due ? "scheduler.reason.due_now" : `scheduler.reason.${reason || "no_decision"}`;
  return t(key, reason ? reason.replaceAll("_", " ") : t("scheduler.reason.no_decision"));
}

export function scheduleModeLabel(t: TFunction, mode?: string | null): string {
  const normalized = (mode || "interval").toLowerCase();
  return t(`scheduler.mode.${normalized}`, mode || "interval");
}
