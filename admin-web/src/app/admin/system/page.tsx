"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { StatusBadge, PageHeader, PageShell, PermissionGuard } from "@/components";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";

export default function SystemPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const [refreshing, setRefreshing] = useState(false);
  const health = useQuery({ queryKey: queryKeys.health, queryFn: api.health, refetchInterval: 15000 });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  return (
    <PermissionGuard module="system">
    <PageShell>
      <PageHeader
        title={t("system_health.title")}
        description={t("system_health.desc")}
        primaryAction={
        <button
          onClick={() => { setRefreshing(true); health.refetch().then(() => setRefreshing(false)); sources.refetch(); }}
          disabled={refreshing}
          className="btn-primary"
        >
          {refreshing ? t("system_health.refreshing") : t("system_health.refresh")}
        </button>
        }
      />

      <div data-page-primary-content>
      {health.error && <div className="mb-4 rounded-md border border-danger/40 bg-danger-subtle p-4 text-sm text-danger dark:border-danger/40 dark:bg-danger/15 dark:text-danger">{(health.error as Error).message}</div>}

      <div className="mb-8 grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
        {health.data ? Object.entries(health.data.services).map(([name, status]) => (
          <div key={name} className="card p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="font-medium capitalize">{name}</span>
              <StatusBadge status={status as string} />
            </div>
            <div className="text-xs text-muted">
              {status === "up" ? t("system_health.connected") : status === "down" ? t("system_health.unavailable") : t("system_health.unknown")}
            </div>
          </div>
        )) : (
          Array.from({ length: 3 }).map((_, i) => <div key={i} className="card p-4 animate-pulse"><div className="mb-2 h-4 w-1/2 rounded bg-subtle dark:bg-subtle" /><div className="h-3 w-3/4 rounded bg-subtle dark:bg-subtle" /></div>)
        )}
      </div>

      <section>
        <h2 className="text-lg font-semibold mb-3">{t("system_health.providers")}</h2>
        <div className="space-y-3 md:hidden">
          {sources.data?.sources?.map((source) => (
            <article key={source.source_name} className="card p-4">
              <div className="mb-3 flex min-w-0 items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold">{source.display_name}</h3>
                  <p className="mt-0.5 truncate font-mono text-xs text-muted">{source.source_name}</p>
                </div>
                <StatusBadge status={source.capabilities.can_download ? "up" : "down"} />
              </div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
                <div>
                  <dt className="text-muted">{t("system_health.col_gallerydl")}</dt>
                  <dd className="mt-1 font-medium">{source.capabilities.supports_gallerydl ? "✓" : "—"}</dd>
                </div>
                <div>
                  <dt className="text-muted">{t("system_health.col_tags")}</dt>
                  <dd className="mt-1 font-medium">{source.capabilities.supports_tags ? "✓" : "—"}</dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-muted">{t("system_health.col_type")}</dt>
                  <dd className="mt-1 font-medium">{source.capabilities.is_reference_only ? t("system_health.type_reference") : source.capabilities.can_import_local ? t("system_health.type_local") : t("system_health.type_api")}</dd>
                </div>
              </dl>
            </article>
          ))}
        </div>
        <div className="table-shell hidden md:block">
          <table className="min-w-[48rem] w-full text-sm">
            <thead><tr className="table-head"><th className="text-left px-4 py-3">{t("system_health.col_name")}</th><th className="text-left px-4 py-3">{t("system_health.col_source")}</th><th className="text-left px-4 py-3">{t("system_health.col_download")}</th><th className="text-left px-4 py-3">{t("system_health.col_gallerydl")}</th><th className="text-left px-4 py-3">{t("system_health.col_tags")}</th><th className="text-left px-4 py-3">{t("system_health.col_type")}</th></tr></thead>
            <tbody>
              {sources.data?.sources?.map((s) => (
                <tr key={s.source_name} className="table-row">
                  <td className="px-4 py-3 font-medium">{s.display_name}</td>
                  <td className="px-4 py-3 text-muted">{s.source_name}</td>
                  <td className="px-4 py-3">{s.capabilities.can_download ? <StatusBadge status="up" /> : <StatusBadge status="down" />}</td>
                  <td className="px-4 py-3">{s.capabilities.supports_gallerydl ? "✓" : "—"}</td>
                  <td className="px-4 py-3">{s.capabilities.supports_tags ? "✓" : "—"}</td>
                  <td className="px-4 py-3 text-xs">{s.capabilities.is_reference_only ? t("system_health.type_reference") : s.capabilities.can_import_local ? t("system_health.type_local") : t("system_health.type_api")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {health.data && <p className="mt-4 text-xs text-muted">{t("system_health.version")} {health.data.version} · {t("system_health.last_update")} {fmt.time(new Date().toISOString())}</p>}
      </div>
    </PageShell>
    </PermissionGuard>
  );
}
