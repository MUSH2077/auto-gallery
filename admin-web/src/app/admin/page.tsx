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
    { label: t("dashboard.download_jobs"), value: String(failedDJ.data?.length ?? 0), sub: "failed", delay: 50 },
    { label: t("dashboard.import_jobs"), value: String(failedIJ.data?.total ?? 0), sub: "processed", delay: 100 },
    ...(storage.data ? [
      { label: t("dashboard.disk_free"), value: fmtBytes(storage.data.disk?.free_bytes || 0), sub: "available", delay: 150 },
      { label: "Usage", value: `${storage.data.disk?.total_bytes > 0 ? Math.round((storage.data.disk?.used_bytes || 0) / (storage.data.disk?.total_bytes || 1) * 100) : "?"}%`,
        sub: `${fmtBytes(storage.data.downloads?.size_bytes || 0)} downloads`, delay: 200 },
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
    <main className="max-w-6xl mx-auto p-6 md:p-10 page-transition">
      <header className="mb-10">
        <h1 className="text-4xl md:text-5xl font-bold tracking-tight text-stone-900 dark:text-stone-100"
          style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
          {t("dashboard.title")}
        </h1>
        <p className="mt-2 text-stone-500 dark:text-stone-400 text-sm max-w-lg leading-relaxed">{t("dashboard.desc")}</p>
      </header>

      {/* Key Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
        {stats.map((s, i) => (
          <div key={i} className="card-elevated p-5 page-transition" style={{ animationDelay: `${s.delay}ms` }}>
            <div className="text-2xl md:text-3xl font-bold text-amber-700 dark:text-amber-400 tracking-tight">{s.value}</div>
            <div className="text-xs text-stone-500 dark:text-stone-400 mt-1 font-medium uppercase tracking-wider">{s.label}</div>
          </div>
        ))}
      </div>

      {/* Services + Storage */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-10">
        <section>
          <h2 className="text-xl font-bold mb-4 text-stone-900 dark:text-stone-100" style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>{t("dashboard.services")}</h2>
          {health.data ? (
            <div className="grid grid-cols-3 gap-3">
              {Object.entries(health.data.services).map(([name, status], i) => (
                <div key={name} className="card-interactive p-4 flex flex-col items-center text-center gap-2 page-transition" style={{ animationDelay: `${250 + i * 50}ms` }}>
                  <StatusBadge status={status as string} />
                  <span className="text-xs font-medium capitalize text-stone-600 dark:text-stone-300">{name}</span>
                </div>
              ))}
            </div>
          ) : health.isLoading ? (
            <div className="grid grid-cols-3 gap-3">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="card p-8 skeleton rounded-xl" />)}</div>
          ) : <ErrorState message={health.error?.message || "Failed"} onRetry={() => health.refetch()} />}
        </section>

        <section>
          <h2 className="text-xl font-bold mb-4 text-stone-900 dark:text-stone-100" style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>{t("dashboard.storage")}</h2>
          {storage.data ? (
            <div className="grid grid-cols-2 gap-3">
              {[
                { l: t("dashboard.downloads_files"), v: fmtBytes(storage.data.downloads?.size_bytes || 0), n: storage.data.downloads?.file_count },
                { l: t("dashboard.library_files"), v: fmtBytes(storage.data.library?.size_bytes || 0), n: storage.data.library?.file_count },
                { l: t("dashboard.disk_free"), v: fmtBytes(storage.data.disk?.free_bytes || 0) },
                { l: t("dashboard.disk_used"), v: `${storage.data.disk?.total_bytes > 0 ? Math.round((storage.data.disk?.used_bytes || 0) / (storage.data.disk?.total_bytes || 1) * 100) : "?"}%` },
              ].map((x, i) => (
                <div key={i} className="card p-4 page-transition" style={{ animationDelay: `${300 + i * 50}ms` }}>
                  <div className="text-xl font-bold text-stone-800 dark:text-stone-200">{x.v}</div>
                  <div className="text-xs text-stone-500 dark:text-stone-400 mt-1">{x.l}</div>
                  {x.n !== undefined && <div className="text-[10px] text-stone-400 mt-0.5">{x.n} files</div>}
                </div>
              ))}
            </div>
          ) : storage.isLoading ? (
            <div className="grid grid-cols-2 gap-3">{Array.from({ length: 4 }).map((_, i) => <div key={i} className="card p-8 skeleton rounded-xl" />)}</div>
          ) : <ErrorState message={storage.error?.message || "Failed"} onRetry={() => storage.refetch()} />}
        </section>
      </div>

      {/* Quick Links */}
      <section>
        <h2 className="text-xl font-bold mb-4 text-stone-900 dark:text-stone-100" style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>{t("dashboard.recent_activity")}</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {links.map((item, i) => (
            <button key={item.to} onClick={() => router.push(item.to)}
              className="card-interactive p-5 text-left group page-transition" style={{ animationDelay: `${400 + i * 50}ms` }}>
              <span className="text-sm font-medium text-stone-700 dark:text-stone-200 group-hover:text-amber-700 dark:group-hover:text-amber-400 transition-colors">{item.label}</span>
              <span className="text-xs text-amber-600 dark:text-amber-500 block mt-2 opacity-0 group-hover:opacity-100 transition-opacity">{t("dashboard.view_arrow")} →</span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
