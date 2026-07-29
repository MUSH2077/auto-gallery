"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { api, queryKeys } from "@/lib/api";
import { EmptyState, ErrorState, PageHeader, PageShell, PermissionGuard } from "@/components";
import { useT } from "@/lib/i18n";
import { POLL_ACTIVE_MS } from "@/lib/polling";
import Link from "next/link";
import { useI18nFormat } from "@/lib/i18n-format";

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "text-muted",
  INFO: "text-success",
  WARNING: "text-warning",
  ERROR: "text-danger",
  CRITICAL: "text-danger font-bold",
};

export default function SystemLogsPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const [levelFilter, setLevelFilter] = useState("");
  const [nameFilter, setNameFilter] = useState("");
  const [limit, setLimit] = useState(200);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const logs = useQuery({
    queryKey: queryKeys.system.logs(limit, levelFilter || undefined, nameFilter || undefined),
    queryFn: () => api.systemLogs(limit, levelFilter || undefined, nameFilter || undefined),
    refetchInterval: autoRefresh ? POLL_ACTIVE_MS : false,
    placeholderData: (previous) => previous,
  });

  return (
    <PermissionGuard module="system">
      <PageShell>
      <Link href="/admin/settings" className="btn-ghost mb-4 inline-flex min-h-11 items-center gap-2">
        <ArrowLeft aria-hidden="true" className="h-4 w-4" />
        {t("logs.back")}
      </Link>
      <PageHeader title={t("logs.title")} description={t("logs.desc")}>
        <button
          type="button"
          onClick={() => logs.refetch()}
          disabled={logs.isFetching}
          className="btn-ghost inline-flex min-h-11 items-center gap-2"
        >
          <RefreshCw aria-hidden="true" className={`h-4 w-4 ${logs.isFetching ? "animate-spin" : ""}`} />
          {logs.isFetching ? t("common.refreshing") : t("common.refresh")}
        </button>
      </PageHeader>

      <div className="mb-4 flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface p-3">
        <label className="grid gap-1 text-xs font-medium text-muted">
          {t("logs.level")}
          <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}
          className="input min-h-11 min-w-36 text-sm">
          <option value="">{t("logs.all_levels")}</option>
          {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </label>
        <label className="grid gap-1 text-xs font-medium text-muted">
          {t("logs.source")}
          <input value={nameFilter} onChange={(e) => setNameFilter(e.target.value)}
            placeholder={t("logs.filter_name")}
            className="input min-h-11 w-56 text-sm" />
        </label>
        <label className="grid gap-1 text-xs font-medium text-muted">
          {t("logs.limit")}
          <select value={limit} onChange={(event) => setLimit(Number(event.target.value))} className="input min-h-11 min-w-28 text-sm">
            {[100, 200, 500].map((value) => <option key={value} value={value}>{fmt.number(value)}</option>)}
          </select>
        </label>
        <label className="flex min-h-11 cursor-pointer items-center gap-2 px-2 text-sm text-muted">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="h-5 w-5 rounded" />
          {t("logs.auto_refresh")}
        </label>
        <div className="flex-1" />
        {logs.data && <span className="min-h-6 text-xs text-muted">{t("logs.entries", { count: fmt.number(logs.data.total) })}</span>}
      </div>

      <p aria-live="polite" className="sr-only">
        {logs.isFetching ? t("logs.refreshing") : logs.data ? t("logs.updated") : ""}
      </p>
      {logs.isLoading && <div aria-label={t("common.loading")} className="animate-pulse space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-8 bg-subtle rounded" />)}</div>}
      {logs.error && <ErrorState message={(logs.error as Error).message} onRetry={() => logs.refetch()} />}

      {logs.data && (
        logs.data.entries.length === 0
        ? <EmptyState title={t("logs.no_entries")} description={t("logs.no_entries_desc")} />
        : <div className="max-h-[70vh] overflow-auto rounded-md border border-border bg-subtle p-4 font-mono text-xs leading-relaxed" role="log" aria-label={t("logs.title")}>
          {logs.data.entries.map((e, i) => (
            <div key={`${e.ts}-${i}`} className="grid min-w-[680px] grid-cols-[8rem_4rem_9rem_minmax(0,1fr)] gap-3 rounded px-1 py-1 hover:bg-surface">
              <time className="shrink-0 text-muted" dateTime={e.ts}>{fmt.time(e.ts)}</time>
              <span className={`shrink-0 w-14 ${LEVEL_COLORS[e.level] || "text-muted"}`}>{e.level}</span>
              <span className="truncate text-muted" title={e.name}>{e.name}</span>
              <span className="whitespace-pre-wrap break-words text-fg">{e.msg}</span>
            </div>
          ))}
        </div>
      )}
      </PageShell>
    </PermissionGuard>
  );
}
