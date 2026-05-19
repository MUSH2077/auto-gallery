"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "text-gray-400",
  INFO: "text-green-600 dark:text-green-400",
  WARNING: "text-yellow-600 dark:text-yellow-400",
  ERROR: "text-red-600 dark:text-red-400",
  CRITICAL: "text-red-700 dark:text-red-500 font-bold",
};

export default function SystemLogsPage() {
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
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="System Logs" description="Real-time application log viewer from the in-memory ring buffer (last 2000 entries)." />

      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}
          className="border rounded px-3 py-2 text-sm dark:bg-slate-700 dark:text-white dark:border-slate-600">
          <option value="">All Levels</option>
          {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <input value={nameFilter} onChange={(e) => setNameFilter(e.target.value)}
          placeholder="Filter by logger name..."
          className="border rounded px-3 py-2 text-sm w-48 dark:bg-slate-700 dark:text-white dark:border-slate-600" />
        <label className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400 cursor-pointer">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="rounded" />
          Auto-refresh (5s)
        </label>
        <div className="flex-1" />
        {logs.data && <span className="text-xs text-gray-400">{logs.data.total} entries</span>}
      </div>

      {/* Log entries */}
      {logs.isLoading && <div className="animate-pulse space-y-1">{Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-6 bg-gray-100 dark:bg-slate-700 rounded" />)}</div>}
      {logs.error && <ErrorState message={(logs.error as Error).message} />}

      {logs.data && (
        <div className="bg-slate-950 text-green-400 rounded-lg shadow p-4 font-mono text-xs leading-relaxed overflow-auto max-h-[70vh]">
          {logs.data.entries.length === 0 && <span className="text-gray-500">No log entries matching filters.</span>}
          {logs.data.entries.map((e, i) => (
            <div key={i} className="flex gap-3 hover:bg-slate-800 px-1 -mx-1 rounded">
              <span className="text-slate-500 shrink-0">{e.ts.slice(11, 19)}</span>
              <span className={`shrink-0 w-14 ${LEVEL_COLORS[e.level] || "text-gray-400"}`}>{e.level}</span>
              <span className="text-slate-400 shrink-0 w-28 truncate">{e.name.split(".").slice(-1)[0]}</span>
              <span className="text-gray-300 truncate">{e.msg}</span>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
