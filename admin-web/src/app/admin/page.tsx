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
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {health.data && <ServiceCard health={health.data} t={t} />}
        {storage.data && <StorageCard storage={storage.data} t={t} fmtBytes={fmtBytes} />}
        <StatCard title={t("dashboard.download_jobs")} count={failedDownloads.data?.length ?? 0} color="blue" />
        <StatCard title={t("dashboard.import_jobs")} count={failedImports.data?.total ?? 0} color="purple" />
      </div>
      <QuickLinks router={router} t={t} />
    </main>
  );
}

function ServiceCard({ health, t }: { health: any; t: any }) {
  const svc = health.services || {};
  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
      <h3 className="text-sm font-medium mb-2 dark:text-white">{t("dashboard.services")}</h3>
      <div className="space-y-1 text-xs">
        {Object.entries(svc).map(([k, v]) => (
          <div key={k} className="flex items-center justify-between">
            <span className="text-gray-500 dark:text-gray-400">{k}</span>
            <StatusBadge status={v === "up" ? "up" : "down"} />
          </div>
        ))}
      </div>
    </div>
  );
}

function StorageCard({ storage, t, fmtBytes }: { storage: any; t: any; fmtBytes: (n: number) => string }) {
  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4">
      <h3 className="text-sm font-medium mb-2 dark:text-white">{t("dashboard.storage")}</h3>
      <div className="space-y-1 text-xs">
        <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("dashboard.downloads_files")}</span><span className="font-mono">{fmtBytes(storage.downloads?.total_size || 0)}</span></div>
        <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("dashboard.library_files")}</span><span className="font-mono">{fmtBytes(storage.library?.total_size || 0)}</span></div>
        <div className="flex justify-between"><span className="text-gray-500 dark:text-gray-400">{t("dashboard.disk_free")}</span><span className="font-mono">{fmtBytes(storage.disk?.free || 0)}</span></div>
      </div>
    </div>
  );
}

function StatCard({ title, count, color }: { title: string; count: number; color: string }) {
  const borderColor = color === "blue" ? "border-blue-400" : "border-purple-400";
  return (
    <div className={`bg-white dark:bg-slate-800 rounded-lg shadow p-4 border-l-4 ${borderColor}`}>
      <h3 className="text-sm font-medium dark:text-white">{title}</h3>
      <p className="text-2xl font-bold mt-1">{count}</p>
    </div>
  );
}

function QuickLinks({ router, t }: { router: any; t: any }) {
  const links = [
    { label: t("dashboard.quick_creators"), to: "/admin/creators" },
    { label: t("dashboard.quick_subscriptions"), to: "/admin/subscriptions" },
    { label: t("dashboard.quick_scheduler"), to: "/admin/scheduler" },
    { label: t("dashboard.quick_download_jobs"), to: "/admin/jobs" },
    { label: t("dashboard.quick_works"), to: "/admin/works" },
    { label: t("dashboard.quick_tags"), to: "/admin/tags" },
    { label: t("dashboard.quick_danbooru"), to: "/admin/reference/danbooru" },
    { label: t("dashboard.quick_settings"), to: "/admin/settings" },
  ];
  return (
    <section>
      <h2 className="text-lg font-semibold mb-3 dark:text-white">{t("dashboard.recent_activity")}</h2>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {links.map((item) => (
          <button key={item.to} onClick={() => router.push(item.to)}
            className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-left hover:shadow-md transition-shadow">
            <span className="text-sm font-medium">{item.label}</span>
            <span className="text-xs text-blue-600 block mt-1">{t("dashboard.view_arrow")}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
