"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

export default function AuthStatusPage() {
  const t = useT();
  const auth = useQuery({ queryKey: queryKeys.admin.authStatus, queryFn: api.getAuthStatus, refetchInterval: 30000 });

  return (
    <main className="max-w-5xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">&larr; {t("auth.back")}</Link>
      </div>
      <PageHeader title={t("auth.title")} description={t("auth.desc")} />

      {auth.isLoading && (
        <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle animate-pulse dark:bg-subtle" />)}</div>
      )}
      {auth.error && <ErrorState message={(auth.error as Error).message} onRetry={() => auth.refetch()} />}

      {auth.data && (
        <>
          {/* Summary bar */}
          <div className="grid grid-cols-4 gap-3 mb-6">
            <div className="card p-4 text-center">
              <div className="text-2xl font-bold">{auth.data.summary.total}</div>
              <div className="text-xs text-muted">{t("auth.total")}</div>
            </div>
            <div className="card p-4 text-center">
              <div className="text-2xl font-bold text-success">{auth.data.summary.healthy}</div>
              <div className="text-xs text-muted">{t("auth.healthy")}</div>
            </div>
            <div className={`card p-4 text-center ${auth.data.summary.unhealthy > 0 ? "border-danger dark:border-danger" : ""}`}>
              <div className={`text-2xl font-bold ${auth.data.summary.unhealthy > 0 ? "text-danger" : ""}`}>{auth.data.summary.unhealthy}</div>
              <div className="text-xs text-muted">{t("auth.unhealthy")}</div>
            </div>
            <div className="card p-4 text-center">
              <div className="text-2xl font-bold text-muted">{auth.data.summary.unknown}</div>
              <div className="text-xs text-muted">{t("auth.unknown")}</div>
            </div>
          </div>

          {auth.data.sources.length === 0 && (
            <EmptyState title={t("auth.no_sources")} description={t("auth.no_sources_desc")} />
          )}

          {auth.data.sources.map((s) => (
            <div key={s.id} className="card p-4 mb-2 text-sm">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <SourceBadge source={s.source} />
                  <div>
                    <div className="font-medium">{s.creator.display_name || s.creator.name}</div>
                    <div className="mt-0.5 font-mono text-xs text-muted">{s.source_url}</div>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  {!s.is_enabled && <span className="badge">{t("auth.disabled")}</span>}
                  {s.auth_healthy === true && <span className="inline-flex items-center gap-1 text-xs text-success bg-success-subtle px-2 py-0.5 rounded"><span className="w-2 h-2 bg-success-subtle0 rounded-full" /> {t("auth.healthy")}</span>}
                  {s.auth_healthy === false && <span className="inline-flex items-center gap-1 text-xs text-danger bg-danger-subtle px-2 py-0.5 rounded"><span className="w-2 h-2 bg-danger-subtle0 rounded-full" /> {t("auth.unhealthy")}</span>}
                  {s.auth_healthy === null && <span className="badge inline-flex items-center gap-1"><span className="w-2 h-2 bg-muted rounded-full" /> {t("auth.unknown")}</span>}
                  {s.last_successful_auth && <span className="text-xs text-muted">{t("auth.last_success")} {new Date(s.last_successful_auth).toLocaleString()}</span>}
                </div>
              </div>
            </div>
          ))}

          <div className="mt-6 rounded-md border border-accent-subtle bg-accent-subtle p-4 text-sm text-accent dark:border-accent/30 dark:bg-accent/15 dark:text-accent">
            {t("auth.how")}
          </div>
        </>
      )}
    </main>
  );
}
