"use client";
import { useT } from "@/lib/i18n";
import { useNotifications, type BatchJobState } from "@/components/NotificationCenter";
import { useRouter } from "next/navigation";
import { PageHeader, EmptyState } from "@/components";

function BatchJobCard({ job, t }: { job: BatchJobState; t: (k: string) => string }) {
  const router = useRouter();
  const { clearBatchJob } = useNotifications();

  const statusBadge = () => {
    switch (job.status) {
      case "running":
        return <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 animate-pulse">{t("notification.running")}</span>;
      case "completed":
        return <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400">{t("notification.completed")}</span>;
      case "error":
        return <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400">{t("notification.error")}</span>;
      default:
        return <span className="px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400">{job.status}</span>;
    }
  };

  const timeStr = new Date(job.startedAt).toLocaleString();

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <h3 className="text-sm font-semibold dark:text-white">
              {job.importType === "pixiv" ? t("notification.batch_import") : "URL Batch Import"}
            </h3>
            {statusBadge()}
          </div>
          <p className="text-xs text-slate-500 dark:text-slate-400">{timeStr}</p>
          {job.progress && (
            <div className="mt-3">
              <div className="flex justify-between text-xs text-slate-500 mb-1">
                <span>{job.progress.current}/{job.progress.total}</span>
                <span>{job.progress.imported} imported</span>
              </div>
              <div className="w-full bg-slate-200 dark:bg-slate-700 rounded-full h-2">
                <div className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${(job.progress.current / job.progress.total) * 100}%` }} />
              </div>
            </div>
          )}
          {job.result && (
            <div className="mt-3 grid grid-cols-4 gap-2">
              <div className="bg-green-50 dark:bg-green-900/20 rounded p-2 text-center">
                <div className="text-lg font-bold text-green-700 dark:text-green-400">{job.result.imported_count || job.result.imported?.length || 0}</div>
                <div className="text-[10px] text-green-600">{t("danbooru.batch_result_imported")}</div>
              </div>
              <div className="bg-yellow-50 dark:bg-yellow-900/20 rounded p-2 text-center">
                <div className="text-lg font-bold text-yellow-700 dark:text-yellow-400">{job.result.low_confidence_count || job.result.low_confidence?.length || 0}</div>
                <div className="text-[10px] text-yellow-600">{t("danbooru.batch_result_low_confidence")}</div>
              </div>
              <div className="bg-red-50 dark:bg-red-900/20 rounded p-2 text-center">
                <div className="text-lg font-bold text-red-700 dark:text-red-400">{job.result.not_found_count || job.result.not_found?.length || 0}</div>
                <div className="text-[10px] text-red-600">{t("danbooru.batch_result_not_found")}</div>
              </div>
              <div className="bg-gray-50 dark:bg-slate-800/50 rounded p-2 text-center">
                <div className="text-lg font-bold text-gray-700 dark:text-gray-400">{job.result.error_count || job.result.errors?.length || 0}</div>
                <div className="text-[10px] text-gray-500">{t("danbooru.batch_result_errors")}</div>
              </div>
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button onClick={() => router.push("/admin/reference/danbooru")}
            className="px-3 py-1 text-xs rounded bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors">
            {t("common.view")}
          </button>
          {job.status !== "running" && (
            <button onClick={() => clearBatchJob()}
              className="px-3 py-1 text-xs rounded text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
              {t("common.close")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export default function NotificationsPage() {
  const t = useT();
  const router = useRouter();
  const { items, batchJob, removeActivity, clearRecent } = useNotifications();

  const allEmpty = items.length === 0 && !batchJob;

  return (
    <main className="max-w-3xl mx-auto p-6 md:p-10 page-transition">
      <PageHeader
        title={t("notifications.title")}
        description={t("notifications.desc")}
      />
      {allEmpty ? (
        <EmptyState title={t("notification.empty")} />
      ) : (
        <div className="space-y-3">
          {batchJob && <BatchJobCard job={batchJob} t={t} />}
          {items.map((a) => (
            <div key={a.id} className="card p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <h3 className="text-sm font-semibold dark:text-white">{a.title}</h3>
                    {a.status === "running" && <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 animate-pulse">{t("notification.running")}</span>}
                    {a.status === "completed" && <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400">{t("notification.completed")}</span>}
                    {a.status === "error" && <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400">{t("notification.error")}</span>}
                  </div>
                  {a.message && <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{a.message}</p>}
                  <p className="text-[10px] text-slate-400 mt-1">{new Date(a.timestamp).toLocaleString()}</p>
                  {a.status === "running" && a.progress !== undefined && (
                    <div className="mt-2 w-full bg-slate-200 dark:bg-slate-700 rounded-full h-1.5">
                      <div className="bg-blue-500 h-1.5 rounded-full transition-all duration-500" style={{ width: `${a.progress}%` }} />
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  {a.link && (
                    <button onClick={() => router.push(a.link!)}
                      className="px-3 py-1 text-xs rounded bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors">
                      {t("common.view")}
                    </button>
                  )}
                  <button onClick={() => removeActivity(a.id)}
                    className="px-3 py-1 text-xs rounded text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors">
                    {t("common.close")}
                  </button>
                </div>
              </div>
            </div>
          ))}
          {items.length > 0 && (
            <div className="flex justify-end pt-2">
              <button onClick={clearRecent}
                className="px-4 py-1.5 text-xs rounded bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600 transition-colors text-slate-600 dark:text-slate-400">
                {t("notification.clear_all")}
              </button>
            </div>
          )}
        </div>
      )}
    </main>
  );
}
