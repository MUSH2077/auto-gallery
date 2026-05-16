"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ErrorState, ConfirmDialog } from "@/components";
import Link from "next/link";

export default function SettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const reindex = useMutation({
    mutationFn: api.reindexSearch,
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); setConfirmReindex(false); },
    onError: () => { setConfirmReindex(false); },
  });
  const [confirmReindex, setConfirmReindex] = useState(false);

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Settings" description="System configuration" />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        <Link href="/admin/settings/gallerydl"
          className="bg-white rounded-lg shadow p-6 hover:shadow-md transition-shadow block">
          <h2 className="text-lg font-semibold mb-2">gallery-dl Config</h2>
          <p className="text-sm text-gray-500">Pixiv extractor settings, auth tokens, file organization, rate limiting.</p>
        </Link>

        <Link href="/admin/settings/dedup"
          className="bg-white rounded-lg shadow p-6 hover:shadow-md transition-shadow block">
          <h2 className="text-lg font-semibold mb-2">Deduplication</h2>
          <p className="text-sm text-gray-500">Source-level, cross-source, and perceptual hash dedup controls.</p>
        </Link>
      </div>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Search Index</h2>
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium">Meilisearch Re-indexing</p>
              <p className="text-xs text-gray-500 mt-1">Admin-triggered full re-indexing of all works, creators, and tags.</p>
            </div>
            <button onClick={() => setConfirmReindex(true)} disabled={reindex.isPending}
              className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50 shrink-0">
              {reindex.isPending ? "Reindexing..." : "Reindex Now"}
            </button>
          </div>
        </div>
      </section>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Deduplication Status</h2>
        {settings.isError ? (
          <ErrorState message={settings.error?.message || "Failed"} onRetry={() => settings.refetch()} />
        ) : !settings.data ? (
          <div className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-20 bg-gray-100 rounded" /></div>
        ) : (
          <div className="bg-white rounded-lg shadow p-4">
            <div className="grid grid-cols-2 gap-3 text-sm">
              {Object.entries(settings.data.dedup || {}).map(([key, value]) => (
                <div key={key} className="flex justify-between py-1 border-b last:border-0">
                  <span className="text-gray-500">{key}</span>
                  <span className="font-mono text-xs">{String(value)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">System Information</h2>
        <div className="bg-white rounded-lg shadow p-4 text-sm space-y-2">
          <div className="flex justify-between"><span className="text-gray-500">Backend API</span><span className="font-mono text-xs">/api/v1 (proxied)</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Admin Web</span><span className="text-xs">Next.js 14 · TypeScript · Tailwind · TanStack Query</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Auth mode</span><span className="text-xs text-gray-400">Admin API key</span></div>
        </div>
      </section>

      {confirmReindex && <ConfirmDialog open title="Reindex Search" message="Full Meilisearch re-indexing may take a while."
        onConfirm={() => reindex.mutate()} onCancel={() => setConfirmReindex(false)}
        isPending={reindex.isPending} error={(reindex.error as Error)?.message} />}
    </main>
  );
}
