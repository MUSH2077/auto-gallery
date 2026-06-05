"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, ErrorState } from "@/components";
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
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storageStats, refetchInterval: 60000 });
  const failedDJ = useQuery({ queryKey: [...queryKeys.downloadJobs.all, "failed"], queryFn: () => api.listDownloadJobs({ status: "failed", offset: 0, limit: 5 }) });
  const failedIJ = useQuery({ queryKey: [...queryKeys.importJobs.all, "failed"], queryFn: () => api.listImportJobs("failed", 0, 5) });

  const stats = [
    { label: t("dashboard.download_jobs"), value: String(failedDJ.data?.length ?? 0), sub: t("dashboard.failed"), delay: 50 },
    { label: t("dashboard.import_jobs"), value: String(failedIJ.data?.total ?? 0), sub: t("dashboard.processed"), delay: 100 },
    ...(storage.data ? [
      { label: t("dashboard.disk_free"), value: fmtBytes(storage.data.disk?.free_bytes || 0), sub: t("dashboard.available"), delay: 150 },
      { label: t("dashboard.usage"), value: `${storage.data.disk?.total_bytes > 0 ? Math.round((storage.data.disk?.used_bytes || 0) / (storage.data.disk?.total_bytes || 1) * 100) : "?"}%`,
        sub: t("dashboard.original_media_sub", { size: fmtBytes(storage.data.downloads?.size_bytes || 0) }), delay: 200 },
    ] : []),
  ];

  const links = [
    { to: "/admin/creators", label: t("dashboard.quick_creators") },
    { to: "/admin/subscriptions", label: t("dashboard.quick_subscriptions") },
    { to: "/admin/scheduler", label: t("dashboard.quick_scheduler") },
    { to: "/admin/jobs", label: t("dashboard.quick_download_jobs") },
    { to: "/admin/works", label: t("dashboard.quick_works") },
    { to: "/admin/tags", label: t("dashboard.quick_tags") },
    { to: "/admin/reference/danbooru", label: t("dashboard.quick_danbooru") },
    { to: "/admin/settings", label: t("dashboard.quick_settings") },
  ];

  return (
    <main className="mx-auto max-w-6xl p-6 page-transition">
      <header className="mb-6 border-b border-[#d8dee4] pb-4 dark:border-[#30363d]">
        <h1 className="text-2xl font-semibold tracking-normal text-[#24292f] dark:text-[#e6edf3]">
          {t("dashboard.title")}
        </h1>
        <p className="mt-1.5 max-w-2xl text-sm leading-6 text-[#57606a] dark:text-[#8b949e]">{t("dashboard.desc")}</p>
      </header>

      {/* Key Stats */}
      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        {stats.map((s, i) => (
          <div key={i} className="card p-4 page-transition" style={{ animationDelay: `${s.delay}ms` }}>
            <div className="tabular text-2xl font-semibold tracking-tight text-[#24292f] dark:text-[#e6edf3]">{s.value}</div>
            <div className="mt-1 text-xs font-medium uppercase text-[#57606a] dark:text-[#8b949e]">{s.label}</div>
            <div className="mt-1 text-xs text-[#8c959f] dark:text-[#6e7681]">{s.sub}</div>
          </div>
        ))}
      </div>

      {/* Services + Storage */}
      <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <h2 className="section-title mb-3">{t("dashboard.services")}</h2>
          {health.data ? (
            <div className="grid grid-cols-3 gap-3">
              {Object.entries(health.data.services).map(([name, status], i) => (
                <div key={name} className="card p-4 flex flex-col items-center text-center gap-2 page-transition" style={{ animationDelay: `${250 + i * 50}ms` }}>
                  <StatusBadge status={status as string} />
                  <span className="text-xs font-medium capitalize text-[#57606a] dark:text-[#8b949e]">{name}</span>
                </div>
              ))}
            </div>
          ) : health.isLoading ? (
            <div className="grid grid-cols-3 gap-3">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="card p-8 skeleton" />)}</div>
          ) : <ErrorState message={health.error?.message || "Failed"} onRetry={() => health.refetch()} />}
        </section>

        <section>
          <h2 className="section-title mb-3">{t("dashboard.storage")}</h2>
          {storage.data ? (
            <div className="grid grid-cols-2 gap-3">
              {[
                { l: t("dashboard.downloads_files", { count: storage.data.downloads?.file_count ?? 0 }), v: fmtBytes(storage.data.downloads?.size_bytes || 0), n: storage.data.downloads?.file_count },
                { l: t("dashboard.library_files", { count: storage.data.library?.file_count ?? 0 }), v: fmtBytes(storage.data.library?.size_bytes || 0), n: storage.data.library?.file_count },
                { l: t("dashboard.disk_free"), v: fmtBytes(storage.data.disk?.free_bytes || 0) },
                { l: t("dashboard.disk_used"), v: `${storage.data.disk?.total_bytes > 0 ? Math.round((storage.data.disk?.used_bytes || 0) / (storage.data.disk?.total_bytes || 1) * 100) : "?"}%` },
              ].map((x, i) => (
                <div key={i} className="card p-4 page-transition" style={{ animationDelay: `${300 + i * 50}ms` }}>
                  <div className="tabular text-xl font-semibold text-[#24292f] dark:text-[#e6edf3]">{x.v}</div>
                  <div className="mt-1 text-xs text-[#57606a] dark:text-[#8b949e]">{x.l}</div>
                  {x.n !== undefined && <div className="mt-0.5 text-[10px] text-[#8c959f] dark:text-[#6e7681]">{t("dashboard.file_count", { count: x.n })}</div>}
                </div>
              ))}
            </div>
          ) : storage.isLoading ? (
            <div className="grid grid-cols-2 gap-3">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="card p-8 skeleton" />)}</div>
          ) : <ErrorState message={storage.error?.message || "Failed"} onRetry={() => storage.refetch()} />}
        </section>
      </div>

      {/* Quick Links */}
      <section>
        <h2 className="section-title mb-3">{t("dashboard.recent_activity")}</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {links.map((item, i) => (
            <button key={item.to} onClick={() => router.push(item.to)}
              className="card-interactive group p-4 text-left page-transition" style={{ animationDelay: `${400 + i * 50}ms` }}>
              <span className="text-sm font-medium text-[#24292f] transition-colors group-hover:text-[#0969da] dark:text-[#e6edf3] dark:group-hover:text-[#58a6ff]">{item.label}</span>
              <span className="mt-2 block text-xs text-[#57606a] opacity-0 transition-opacity group-hover:opacity-100 dark:text-[#8b949e]">{t("dashboard.view_arrow")}</span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
