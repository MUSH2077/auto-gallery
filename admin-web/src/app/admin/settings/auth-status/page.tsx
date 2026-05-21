"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

export default function AuthStatusPage() {
  const t = useT();
  const auth = useQuery({ queryKey: ["auth-status"], queryFn: api.getAuthStatus, refetchInterval: 30000 });

  return (
    <main className="max-w-5xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("auth.back")}</Link>
      </div>
      <PageHeader title={t("auth.title")} description={t("auth.desc")} />

      {auth.isLoading && (
        <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>
      )}
      {auth.error && <ErrorState message={(auth.error as Error).message} onRetry={() => auth.refetch()} />}

      {auth.data && (
        <>
          {/* Summary bar */}
          <div className="grid grid-cols-4 gap-3 mb-6">
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold">{auth.data.summary.total}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">{t("auth.total")}</div>
            </div>
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold text-green-600">{auth.data.summary.healthy}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">{t("auth.healthy")}</div>
            </div>
            <div className={`bg-white rounded-lg shadow p-4 text-center ${auth.data.summary.unhealthy > 0 ? "border-2 border-red-300" : ""}`}>
              <div className={`text-2xl font-bold ${auth.data.summary.unhealthy > 0 ? "text-red-600" : ""}`}>{auth.data.summary.unhealthy}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">{t("auth.unhealthy")}</div>
            </div>
            <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-center">
              <div className="text-2xl font-bold text-gray-400 dark:text-gray-500">{auth.data.summary.unknown}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">{t("auth.unknown")}</div>
            </div>
          </div>

          {auth.data.sources.length === 0 && (
            <EmptyState title={t("auth.no_sources")} description={t("auth.no_sources_desc")} />
          )}

          {auth.data.sources.map((s) => (
            <div key={s.id} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-2 text-sm">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <SourceBadge source={s.source} />
                  <div>
                    <div className="font-medium">{s.creator.display_name || s.creator.name}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400 font-mono mt-0.5">{s.source_url}</div>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {!s.is_enabled && <span className="text-xs bg-gray-100 dark:bg-slate-700 text-gray-500 dark:text-gray-400 px-2 py-0.5 rounded">{t("auth.disabled")}</span>}
                  {s.auth_healthy === true && <span className="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-400 bg-green-100 dark:bg-green-900/30 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-green-500 rounded-full" /> {t("auth.healthy")}</span>}
                  {s.auth_healthy === false && <span className="inline-flex items-center gap-1 text-xs text-red-700 dark:text-red-400 bg-red-100 dark:bg-red-900/30 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-red-500 rounded-full" /> {t("auth.unhealthy")}</span>}
                  {s.auth_healthy === null && <span className="inline-flex items-center gap-1 text-xs text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-slate-700 px-2 py-0.5 rounded"><span className="w-2 h-2 bg-gray-300 rounded-full" /> {t("auth.unknown")}</span>}
                  {s.last_successful_auth && <span className="text-xs text-gray-400 dark:text-gray-500">{t("auth.last_success")} {new Date(s.last_successful_auth).toLocaleString()}</span>}
                </div>
              </div>
            </div>
          ))}

          <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
            {t("auth.how")}
          </div>
        </>
      )}
    </main>
  );
}
