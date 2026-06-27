"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "text-muted",
  INFO: "text-green-600 dark:text-green-400",
  WARNING: "text-yellow-600 dark:text-yellow-400",
  ERROR: "text-red-600 dark:text-red-400",
  CRITICAL: "text-red-700 dark:text-red-500 font-bold",
};

export default function SystemLogsPage() {
  const t = useT();
  const [levelFilter, setLevelFilter] = useState("");
  const [nameFilter, setNameFilter] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);

  const logs = useQuery({
    queryKey: ["system-logs", levelFilter, nameFilter],
    queryFn: () => {
      const params = [levelFilter ? `level=${levelFilter}` : "", nameFilter ? `name=${nameFilter}` : ""].filter(Boolean).join("&");
      return api.systemLogs(200, levelFilter || undefined, nameFilter || undefined);
    },
    refetchInterval: autoRefresh ? 5000 : undefined,
  });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("logs.back")}</Link>
      </div>
      <PageHeader title={t("logs.title")} description={t("logs.desc")} />

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}
          className="border rounded px-3 py-2 text-sm dark:bg-subtle dark:text-white dark:border-border">
          <option value="">{t("logs.all_levels")}</option>
          {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <input value={nameFilter} onChange={(e) => setNameFilter(e.target.value)}
          placeholder={t("logs.filter_name")}
          className="border rounded px-3 py-2 text-sm w-48 dark:bg-subtle dark:text-white dark:border-border" />
        <label className="flex items-center gap-2 text-sm text-muted cursor-pointer">
          <input type="checkbox" aria-label="Select item" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="rounded" />
          {t("logs.auto_refresh")}
        </label>
        <div className="flex-1" />
        {logs.data && <span className="text-xs text-muted">{logs.data.total} {t("logs.entries")}</span>}
      </div>

      {/* Log entries */}
      {logs.isLoading && <div className="animate-pulse space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-6 bg-subtle rounded" />)}</div>}
      {logs.error && <ErrorState message={(logs.error as Error).message} />}

      {logs.data && (
        <div className="bg-subtle text-green-400 rounded-lg shadow p-4 font-mono text-xs leading-relaxed overflow-auto max-h-[70vh]">
          {logs.data.entries.length === 0 && <span className="text-muted">{t("logs.no_entries")}</span>}
          {logs.data.entries.map((e, i) => (
            <div key={i} className="flex gap-3 hover:bg-subtle px-1 -mx-1 rounded">
              <span className="text-muted shrink-0">{e.ts.slice(11, 19)}</span>
              <span className={`shrink-0 w-14 ${LEVEL_COLORS[e.level] || "text-muted"}`}>{e.level}</span>
              <span className="text-muted shrink-0 w-28 truncate">{e.name.split(".").slice(-1)[0]}</span>
              <span className="text-muted truncate">{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
