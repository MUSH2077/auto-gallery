"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, PageHeader, ErrorState, CardSkeleton } from "@/components";
import { useRouter } from "next/navigation";

export default function Dashboard() {
  const router = useRouter();
  const health = useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 15000 });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="System Dashboard" description="Service health and provider status" />

      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Services</h2>
        {health.isError ? (
          <ErrorState message={health.error?.message || "Failed to load health status"} onRetry={() => health.refetch()} />
        ) : health.isLoading ? (
          <div className="grid grid-cols-3 gap-3">
            {Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} />)}
          </div>
        ) : health.data ? (
          <div className="grid grid-cols-3 gap-3">
            {Object.entries(health.data.services).map(([name, status]) => (
              <div key={name} className="bg-white rounded-lg shadow p-4 flex items-center gap-3">
                <StatusBadge status={status as string} />
                <span className="font-medium capitalize">{name}</span>
              </div>
            ))}
          </div>
        ) : null}
        {health.data && <p className="text-xs text-gray-400 mt-2">Version: {health.data.version} · Status: <StatusBadge status={health.data.status} /></p>}
      </section>

      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Providers ({sources.data?.sources?.length || 0})</h2>
          <button onClick={() => router.push("/admin/sources")} className="text-sm text-blue-600 hover:underline">View all</button>
        </div>
        {sources.isError ? (
          <ErrorState message={sources.error?.message || "Failed to load sources"} onRetry={() => sources.refetch()} />
        ) : sources.isLoading ? (
          <div className="grid grid-cols-3 gap-3">
            {Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} />)}
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {sources.data?.sources?.slice(0, 6).map((s) => (
              <div key={s.source_name} className="bg-white rounded-lg shadow p-4">
                <div className="font-medium">{s.display_name}</div>
                <div className="text-xs text-gray-400 mt-1">{s.source_name}</div>
                <div className="flex gap-2 mt-2 flex-wrap">
                  {s.capabilities.can_download && <span className="px-2 py-0.5 bg-green-100 text-green-700 rounded text-xs">download</span>}
                  {s.capabilities.supports_gallerydl && <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs">gallery-dl</span>}
                  {s.capabilities.supports_tags && <span className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs">tags</span>}
                  {s.capabilities.is_reference_only && <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-xs">reference only</span>}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: "Creators", to: "/admin/creators" },
            { label: "Subscriptions", to: "/admin/subscriptions" },
            { label: "Download Jobs", to: "/admin/downloads" },
            { label: "Works", to: "/admin/works" },
          ].map((item) => (
            <button key={item.to} onClick={() => router.push(item.to)}
              className="bg-white rounded-lg shadow p-4 text-left hover:shadow-md transition-shadow">
              <span className="text-sm font-medium">{item.label}</span>
              <span className="text-xs text-blue-600 block mt-1">View &rarr;</span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
