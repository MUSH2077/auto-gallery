"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, PageHeader, ErrorState, CardSkeleton } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

function fmtBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

export default function Dashboard() {
  const t = useT();
  const router = useRouter();
  const health = useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 15000 });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storageStats, refetchInterval: 60000 });
  const failedDownloads = useQuery({ queryKey: [...queryKeys.downloadJobs.all, "failed"], queryFn: () => api.listDownloadJobs({ status: "failed", offset: 0, limit: 5 }) });
  const failedImports = useQuery({ queryKey: [...queryKeys.importJobs.all, "failed"], queryFn: () => api.listImportJobs("failed", 0, 5) });
  const recentDownloads = useQuery({ queryKey: [...queryKeys.downloadJobs.all, "recent"], queryFn: () => api.listDownloadJobs({ offset: 0, limit: 5 }) });
  const recentImports = useQuery({ queryKey: [...queryKeys.importJobs.all, "recent"], queryFn: () => api.listImportJobs(undefined, 0, 5) });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("dashboard.title")} description={t("dashboard.desc")} />

      {/* Services & Storage Row */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        {/* Services */}
        <section>
          <h2 className="text-lg font-semibold mb-3">{t("dashboard.services")}</h2>
          {health.isError ? (
            <ErrorState message={health.error?.message || t("dashboard.failed_health")} onRetry={() => health.refetch()} />
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
          <h2 className="text-lg font-semibold mb-3">{t("dashboard.storage")}</h2>
          {storage.isError ? (
            <ErrorState message={storage.error?.message || t("dashboard.failed_storage")} onRetry={() => storage.refetch()} />
          ) : storage.isLoading ? (
            <div className="grid grid-cols-2 gap-3">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} />)}</div>
          ) : storage.data ? (
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.downloads.size_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("dashboard.downloads_files").replace("{count}", String(storage.data.downloads.file_count))}</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.library.size_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("dashboard.library_files").replace("{count}", String(storage.data.library.file_count))}</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{fmtBytes(storage.data.disk.free_bytes)}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("dashboard.disk_free")}</div>
              </div>
              <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="text-2xl font-bold">{storage.data.disk.total_bytes > 0 ? Math.round((storage.data.disk.used_bytes / storage.data.disk.total_bytes) * 100) : "?"}%</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("dashboard.disk_used")}</div>
              </div>
            </div>
          ) : null}
        </section>
      </div>

      {/* Recent Activity */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("dashboard.recent_activity")}</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <h3 className="text-sm font-medium mb-2 dark:text-white">{t("dashboard.download_jobs")}</h3>
            {recentDownloads.isLoading ? <div className="animate-pulse h-16 bg-gray-100 dark:bg-slate-700 rounded" /> :
             !recentDownloads.data?.length ? <p className="text-xs text-gray-400 dark:text-gray-500">{t("dashboard.no_jobs")}</p> :
             <div className="space-y-1">
              {recentDownloads.data.slice(0, 5).map((j) => (
                <div key={j.id} className="flex items-center justify-between text-xs">
                  <span className="font-mono text-gray-400 dark:text-gray-500">{j.id.slice(0, 8)}</span>
                  <span className="text-gray-600 dark:text-gray-300 truncate mx-2 flex-1">{j.source_url?.slice(0, 60)}</span>
                  <span>{j.status === "complete" || j.status === "downloaded" ? "✅" : j.status === "failed" ? "❌" : "⏳"}</span>
                </div>
              ))}
             </div>}
          </div>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
            <h3 className="text-sm font-medium mb-2 dark:text-white">{t("dashboard.import_jobs")}</h3>
            {recentImports.isLoading ? <div className="animate-pulse h-16 bg-gray-100 dark:bg-slate-700 rounded" /> :
             !recentImports.data?.items?.length ? <p className="text-xs text-gray-400 dark:text-gray-500">{t("dashboard.no_jobs")}</p> :
             <div className="space-y-1">
              {recentImports.data.items.slice(0, 5).map((j) => (
                <div key={j.id} className="flex items-center justify-between text-xs">
                  <span className="font-mono text-gray-400 dark:text-gray-500">{j.id.slice(0, 8)}</span>
                  <span className="text-gray-600 dark:text-gray-300 truncate mx-2 flex-1">{j.download_job_id ? j.download_job_id.slice(0, 8) : "—"}</span>
                  <span>{j.status === "complete" ? "✅" : j.status === "failed" ? "❌" : "⏳"}</span>
                </div>
              ))}
             </div>}
          </div>
        </div>
      </section>

      {/* Recent Failures */}
      {(failedDownloads.data && failedDownloads.data.length > 0) || (failedImports.data && (failedImports.data.items?.length ?? 0) > 0) ? (
        <section className="mb-8">
          <h2 className="text-lg font-semibold mb-3 text-red-700 dark:text-red-400">{t("dashboard.recent_failures")}</h2>
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
            {failedImports.data?.items?.slice(0, 3).map((j) => (
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
          <h2 className="text-lg font-semibold">{t("dashboard.providers").replace("{count}", String(sources.data?.sources?.length || 0))}</h2>
          <button onClick={() => router.push("/admin/sources")} className="text-sm text-blue-600 hover:underline">{t("dashboard.view_all")}</button>
        </div>
        {sources.isError ? (
          <ErrorState message={sources.error?.message || t("dashboard.failed_sources")} onRetry={() => sources.refetch()} />
        ) : sources.isLoading ? (
          <div className="grid grid-cols-3 gap-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} />)}</div>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {sources.data?.sources?.slice(0, 6).map((s) => (
              <div key={s.source_name} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
                <div className="font-medium">{s.display_name}</div>
                <div className="text-xs text-gray-400 dark:text-gray-500 mt-1">{s.source_name}</div>
                <div className="flex gap-2 mt-2 flex-wrap">
                  {s.capabilities.can_download && <span className="px-2 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 rounded text-xs">{t("dashboard.download")}</span>}
                  {s.capabilities.supports_gallerydl && <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs">{t("dashboard.gallerydl")}</span>}
                  {s.capabilities.supports_tags && <span className="px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs">{t("dashboard.tags")}</span>}
                  {s.capabilities.is_reference_only && <span className="px-2 py-0.5 bg-yellow-100 text-yellow-700 rounded text-xs">{t("dashboard.reference_only")}</span>}
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
            { label: t("dashboard.quick_creators"), to: "/admin/creators" },
            { label: t("dashboard.quick_subscriptions"), to: "/admin/subscriptions" },
            { label: t("dashboard.quick_scheduler"), to: "/admin/scheduler" },
            { label: t("dashboard.quick_download_jobs"), to: "/admin/downloads" },
            { label: t("dashboard.quick_works"), to: "/admin/works" },
            { label: t("dashboard.quick_tags"), to: "/admin/tags" },
            { label: t("dashboard.quick_danbooru"), to: "/admin/reference/danbooru" },
            { label: t("dashboard.quick_settings"), to: "/admin/settings" },
          ].map((item) => (
            <button key={item.to} onClick={() => router.push(item.to)}
              className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-left hover:shadow-md transition-shadow">
              <span className="text-sm font-medium">{item.label}</span>
              <span className="text-xs text-blue-600 block mt-1">{t("dashboard.view_arrow")}</span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
