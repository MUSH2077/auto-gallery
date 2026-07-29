"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { ErrorState, PageHeader, PageShell, PermissionGuard, StatusBadge } from "@/components";
import SourceRegistryPanel from "@/components/SourceRegistryPanel";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { usePermissions } from "@/lib/usePermissions";

type SystemTab = "services" | "sources";

const SYSTEM_TABS: readonly SystemTab[] = ["services", "sources"];

export default function SystemPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { has, isLoading: permissionsLoading } = usePermissions();
  const canViewServices = has("system");
  const canViewSources = has("subscriptions");
  const requestedTab = searchParams.get("tab");
  const validRequestedTab = SYSTEM_TABS.includes(requestedTab as SystemTab)
    ? requestedTab as SystemTab
    : null;
  const fallbackTab: SystemTab = canViewServices ? "services" : "sources";
  const activeTab: SystemTab = validRequestedTab === "services" && canViewServices
    ? "services"
    : validRequestedTab === "sources" && canViewSources
      ? "sources"
      : fallbackTab;
  const paramsKey = searchParams.toString();

  useEffect(() => {
    if (permissionsLoading || !requestedTab || requestedTab === activeTab) return;
    const next = new URLSearchParams(paramsKey);
    next.set("tab", activeTab);
    router.replace(`/admin/system?${next.toString()}`, { scroll: false });
  }, [activeTab, paramsKey, permissionsLoading, requestedTab, router]);

  const health = useQuery({
    queryKey: queryKeys.health,
    queryFn: api.health,
    enabled: canViewServices && activeTab === "services",
    refetchInterval: canViewServices && activeTab === "services" ? 15000 : false,
  });
  const sources = useQuery({
    queryKey: queryKeys.sources,
    queryFn: api.sources,
    enabled: canViewSources && activeTab === "sources",
  });

  const refreshing = activeTab === "services" ? health.isFetching : sources.isFetching;
  const refreshCurrent = () => {
    if (activeTab === "services") {
      void health.refetch();
    } else {
      void sources.refetch();
    }
  };

  const serviceEntries = health.data
    ? Object.entries({ backend: "up", ...health.data.services })
    : [];

  return (
    <PermissionGuard anyOf={["system", "subscriptions"]}>
      <PageShell>
        <PageHeader
          title={t("system_health.title")}
          description={t("system_health.desc")}
          primaryAction={
            <button
              type="button"
              onClick={refreshCurrent}
              disabled={refreshing}
              className="btn-primary disabled:opacity-100"
            >
              {refreshing ? t("system_health.refreshing") : t("system_health.refresh")}
            </button>
          }
        />

        <div data-page-primary-content>
          <nav
            className="segmented-control mb-6 w-fit max-w-full flex-wrap"
            role="tablist"
            aria-label={t("system_health.sections")}
          >
            {canViewServices && (
              <Link
                id="system-tab-services"
                href="/admin/system?tab=services"
                scroll={false}
                role="tab"
                aria-selected={activeTab === "services"}
                aria-controls="system-panel-services"
                className={`segment min-h-11 ${activeTab === "services" ? "segment-active" : ""}`}
              >
                {t("system_health.services_tab")}
              </Link>
            )}
            {canViewSources && (
              <Link
                id="system-tab-sources"
                href="/admin/system?tab=sources"
                scroll={false}
                role="tab"
                aria-selected={activeTab === "sources"}
                aria-controls="system-panel-sources"
                className={`segment min-h-11 ${activeTab === "sources" ? "segment-active" : ""}`}
              >
                {t("system_health.sources_tab")}
              </Link>
            )}
          </nav>

          {canViewServices && (
            <section
              id="system-panel-services"
              role="tabpanel"
              aria-labelledby="system-tab-services"
              hidden={activeTab !== "services"}
            >
              {health.error && (
                <div className="mb-4">
                  <ErrorState message={(health.error as Error).message} onRetry={() => void health.refetch()} />
                </div>
              )}

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                {health.isLoading
                  ? Array.from({ length: 4 }).map((_, index) => (
                      <div key={index} className="card animate-pulse p-4">
                        <div className="mb-2 h-4 w-1/2 rounded bg-subtle" />
                        <div className="h-3 w-3/4 rounded bg-subtle" />
                      </div>
                    ))
                  : serviceEntries.map(([name, status]) => (
                      <article key={name} className="card p-4">
                        <div className="mb-2 flex items-center justify-between gap-3">
                          <h2 className="truncate font-medium capitalize">{name}</h2>
                          <StatusBadge status={status} />
                        </div>
                        <p className="text-xs text-muted">
                          {status === "up"
                            ? t("system_health.connected")
                            : status === "down"
                              ? t("system_health.unavailable")
                              : t("system_health.unknown")}
                        </p>
                      </article>
                    ))}
              </div>

              {health.data && (
                <p className="mt-4 text-xs text-muted">
                  {t("system_health.version")} {health.data.version}
                  {" · "}
                  {t("system_health.last_update")}{" "}
                  {health.dataUpdatedAt
                    ? fmt.time(new Date(health.dataUpdatedAt).toISOString())
                    : "—"}
                </p>
              )}
            </section>
          )}

          {canViewSources && (
            <section
              id="system-panel-sources"
              role="tabpanel"
              aria-labelledby="system-tab-sources"
              hidden={activeTab !== "sources"}
            >
              <SourceRegistryPanel
                sources={sources.data?.sources}
                loading={sources.isLoading}
                error={sources.error as Error | null}
                onRetry={() => void sources.refetch()}
              />
            </section>
          )}
        </div>
      </PageShell>
    </PermissionGuard>
  );
}
