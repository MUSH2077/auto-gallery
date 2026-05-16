"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, PageHeader } from "@/components";

export default function SystemPage() {
  const [refreshing, setRefreshing] = useState(false);
  const health = useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 15000 });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="System Health" description="Real-time service status">
        <button
          onClick={() => { setRefreshing(true); health.refetch().then(() => setRefreshing(false)); sources.refetch(); }}
          disabled={refreshing}
          className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
        >
          {refreshing ? "Refreshing..." : "Refresh"}
        </button>
      </PageHeader>

      {health.error && <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 dark:text-red-400">{(health.error as Error).message}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3 mb-8">
        {health.data ? Object.entries(health.data.services).map(([name, status]) => (
          <div key={name} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-medium capitalize">{name}</span>
              <StatusBadge status={status as string} />
            </div>
            <div className="text-xs text-gray-400 dark:text-gray-500">
              {status === "up" ? "Connected and responding" : status === "down" ? "Service unavailable" : "Unknown state"}
            </div>
          </div>
        )) : (
          Array.from({ length: 3 }).map((_, i) => <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 animate-pulse"><div className="h-4 bg-gray-200 rounded w-1/2 mb-2" /><div className="h-3 bg-gray-200 rounded w-3/4" /></div>)
        )}
      </div>

      <section>
        <h2 className="text-lg font-semibold mb-3">Provider Registry</h2>
        <div className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-hidden">
          <table className="w-full text-sm">
            <thead><tr className="border-b dark:border-slate-700 bg-gray-50 dark:bg-slate-800/50"><th className="text-left px-4 py-3">Name</th><th className="text-left px-4 py-3">Source</th><th className="text-left px-4 py-3">Download</th><th className="text-left px-4 py-3">gallery-dl</th><th className="text-left px-4 py-3">Tags</th><th className="text-left px-4 py-3">Type</th></tr></thead>
            <tbody>
              {sources.data?.sources?.map((s) => (
                <tr key={s.source_name} className="border-b dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700 dark:bg-slate-800/50">
                  <td className="px-4 py-3 font-medium">{s.display_name}</td>
                  <td className="px-4 py-3 text-gray-500 dark:text-gray-400">{s.source_name}</td>
                  <td className="px-4 py-3">{s.capabilities.can_download ? <StatusBadge status="up" /> : <StatusBadge status="down" />}</td>
                  <td className="px-4 py-3">{s.capabilities.supports_gallerydl ? "✓" : "—"}</td>
                  <td className="px-4 py-3">{s.capabilities.supports_tags ? "✓" : "—"}</td>
                  <td className="px-4 py-3 text-xs">{s.capabilities.is_reference_only ? "Reference" : s.capabilities.can_import_local ? "Local import" : "API Source"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {health.data && <p className="text-xs text-gray-400 dark:text-gray-500 mt-4">App version: {health.data.version} · Last update: {new Date().toLocaleTimeString()}</p>}
    </main>
  );
}
