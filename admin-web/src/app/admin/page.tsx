"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, PageHeader, ErrorState, CardSkeleton } from "@/components";
import { useRouter } from "next/navigation";

function fmtBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

export default function Dashboard() {
  const router = useRouter();
  const health = useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 15000 });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storageStats, refetchInterval: 60000 });
  const failedDownloads = useQuery({ queryKey: [...queryKeys.downloadJobs.all, "failed"], queryFn: () => api.listDownloadJobs("failed", 0, 5) });
  const failedImports = useQuery({ queryKey: [...queryKeys.importJobs.all, "failed"], queryFn: () => api.listImportJobs("failed", 0, 5) });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="System Dashboard" description="Service health, storage, and recent activity" />

      {/* Services & Storage Row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        {/* Services */}
        <section>
          <h2 className="text-lg font-semibold mb-3">Services</h2>
          {health.isError ? (
            <ErrorState message={health.error?.message || "Failed to load health"} onRetry={() => health.refetch()} />
          ) : health.isLoading ? (
            <div className="grid grid-cols-3 gap-3">{Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} />)}</div>
          ) : health.data ? (
            <div className="grid grid-cols-3 gap-3">
              {Object.entries(health.data.services).map(([name, status]) => (
                <div key={name} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 flex items-center gap-3">
                  <StatusBadge status={status as string} />
                  <span className="font-medium capitalize">{name}</span>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        {/* Storage */}
        <section>
          <h2 className="text-lg font-semibold mb-3">Storage</h2>
          {storage.isError ? (
            <ErrorState message={storage.error?.message || "Failed to load storage"} onRetry={() => storage.refetch()} />
          ) : storage.isLoading ? (
            <div className="grid grid-cols-2 gap-3">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} />)}</div>
          ) : storage.data ? (
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.downloads.size_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Downloads · {storage.data.downloads.file_count} files</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.library.size_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Library · {storage.data.library.file_count} files</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.disk.free_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Disk free</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{storage.data.disk.total_bytes > 0 ? Math.round((storage.data.disk.used_bytes / storage.data.disk.total_bytes) * 100) : "?"}%</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">Disk used</div>
              </div>
            </div>
          ) : null}
        </section>
      </div>

      {/* Recent Failures */}
      {(failedDownloads.data && failedDownloads.data.length > 0) || (failedImports.data && failedImports.data.length > 0) ? (
        <section className="mb-8">
          <h2 className="text-lg font-semibold mb-3 text-red-700 dark:text-red-400">Recent Failures</h2>
          <div className="space-y-2">
            {failedDownloads.data?.slice(0, 3).map((j) => (
              <div key={j.id} className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm flex items-center justify-between">
                <div>
                  <span className="font-mono text-xs text-gray-400 dark:text-gray-500 mr-2">DL {j.id.slice(0, 8)}</span>
                  <StatusBadge status="failed" />
                  <span className="text-red-600 ml-2">{(j.error_log || "").slice(0, 120)}</span>
                </div>
                <button onClick={() => router.push("/admin/downloads")} className="text-xs text-blue-600 hover:underline shrink-0 ml-4">View</button>
              </div>
            ))}
            {failedImports.data?.slice(0, 3).map((j) => (
              <div key={j.id} className="bg-red-50 border border-red-200 rounded-lg p-3 text-sm flex items-center justify-between">
                <div>
                  <span className="font-mono text-xs text-gray-400 dark:text-gray-500 mr-2">IM {j.id.slice(0, 8)}</span>
                  <StatusBadge status="failed" />
                  <span className="text-red-600 ml-2">{(j.error_log || "").slice(0, 120)}</span>
                </div>
                <button onClick={() => router.push("/admin/import-jobs")} className="text-xs text-blue-600 hover:underline shrink-0 ml-4">View</button>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {/* Providers */}
      <section className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">Providers ({sources.data?.sources?.length || 0})</h2>
          <button onClick={() => router.push("/admin/sources")} className="text-sm text-blue-600 hover:underline">View all</button>
        </div>
        {sources.isError ? (
          <ErrorState message={sources.error?.message || "Failed to load sources"} onRetry={() => sources.refetch()} />
        ) : sources.isLoading ? (
          <div className="grid grid-cols-3 gap-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} />)}</div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {sources.data?.sources?.slice(0, 6).map((s) => (
              <div key={s.source_name} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="font-medium">{s.display_name}</div>
                <div className="text-xs text-gray-400 dark:text-gray-500 mt-1">{s.source_name}</div>
                <div className="flex gap-2 mt-2 flex-wrap">
                  {s.capabilities.can_download && <span className="px-2 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded text-xs">download</span>}
                  {s.capabilities.supports_gallerydl && <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs">gallery-dl</span>}
                  {s.capabilities.supports_tags && <span className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs">tags</span>}
                  {s.capabilities.is_reference_only && <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-xs">reference only</span>}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Quick Links */}
      <section>
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: "Creators", to: "/admin/creators" },
            { label: "Subscriptions", to: "/admin/subscriptions" },
            { label: "Scheduler", to: "/admin/scheduler" },
            { label: "Download Jobs", to: "/admin/downloads" },
            { label: "Works", to: "/admin/works" },
            { label: "Tags", to: "/admin/tags" },
            { label: "Danbooru Reference", to: "/admin/reference/danbooru" },
            { label: "Settings", to: "/admin/settings" },
          ].map((item) => (
            <button key={item.to} onClick={() => router.push(item.to)}
              className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-left hover:shadow-md transition-shadow">
              <span className="text-sm font-medium">{item.label}</span>
              <span className="text-xs text-blue-600 block mt-1">View &rarr;</span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
