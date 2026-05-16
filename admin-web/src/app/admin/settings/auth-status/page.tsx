"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";
import Link from "next/link";

export default function AuthStatusPage() {
  const auth = useQuery({ queryKey: ["auth-status"], queryFn: api.getAuthStatus, refetchInterval: 30000 });

  return (
    <main className="max-w-5xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Auth & Cookie Status" description="Monitor authentication health for all subscription sources." />

      {auth.isLoading && (
        <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 rounded animate-pulse" />)}</div>
      )}
      {auth.error && <ErrorState message={(auth.error as Error).message} onRetry={() => auth.refetch()} />}

      {auth.data && (
        <>
          {/* Summary bar */}
          <div className="grid grid-cols-4 gap-3 mb-6">
            <div className="bg-white rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold">{auth.data.summary.total}</div>
              <div className="text-xs text-gray-500">Total Sources</div>
            </div>
            <div className="bg-white rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold text-green-600">{auth.data.summary.healthy}</div>
              <div className="text-xs text-gray-500">Healthy</div>
            </div>
            <div className={`bg-white rounded-lg shadow p-4 text-center ${auth.data.summary.unhealthy > 0 ? "border-2 border-red-300" : ""}`}>
              <div className={`text-2xl font-bold ${auth.data.summary.unhealthy > 0 ? "text-red-600" : ""}`}>{auth.data.summary.unhealthy}</div>
              <div className="text-xs text-gray-500">Unhealthy</div>
            </div>
            <div className="bg-white rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold text-gray-400">{auth.data.summary.unknown}</div>
              <div className="text-xs text-gray-500">Unknown</div>
            </div>
          </div>

          {auth.data.sources.length === 0 && (
            <EmptyState title="No subscription sources" description="Create a subscription with sources to monitor auth health." />
          )}

          {auth.data.sources.map((s) => (
            <div key={s.id} className="bg-white rounded-lg shadow p-4 mb-2 text-sm">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <SourceBadge source={s.source} />
                  <div>
                    <div className="font-medium">{s.creator.display_name || s.creator.name}</div>
                    <div className="text-xs text-gray-500 font-mono mt-0.5">{s.source_url}</div>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {!s.is_enabled && <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded">Disabled</span>}
                  {s.auth_healthy === true && <span className="inline-flex items-center gap-1 text-xs text-green-700 bg-green-100 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-green-500 rounded-full" /> Healthy</span>}
                  {s.auth_healthy === false && <span className="inline-flex items-center gap-1 text-xs text-red-700 bg-red-100 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-red-500 rounded-full" /> Unhealthy</span>}
                  {s.auth_healthy === null && <span className="inline-flex items-center gap-1 text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-gray-300 rounded-full" /> Unknown</span>}
                  {s.last_successful_auth && <span className="text-xs text-gray-400">Last success: {new Date(s.last_successful_auth).toLocaleString()}</span>}
                </div>
              </div>
            </div>
          ))}

          <div className="mt-6 p-4 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-800">
            <strong>How auth detection works:</strong> After each download, the worker scans gallery-dl stderr for known auth error patterns (HTTP 401/403,
            cookie expired, token invalid, login required). If detected, <code className="text-xs bg-blue-100 px-1 rounded">auth_healthy</code> is set to
            false and the scheduler will skip that source until it recovers. A successful download resets the flag.
          </div>
        </>
      )}
    </main>
  );
}
