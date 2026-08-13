"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { ErrorState, PageHeader, PageShell, PermissionGuard, StatusBadge } from "@/components";
import SourceRegistryPanel from "@/components/SourceRegistryPanel";
import { api, queryKeys } from "@/lib/api";
import { useT, type TFunction } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { usePermissions } from "@/lib/usePermissions";

type SystemTab = "services" | "sources";

const SYSTEM_TABS: readonly SystemTab[] = ["services", "sources"];

function formatCapacity(bytes?: number | null) {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  return `${(bytes / (1024 ** 3)).toFixed(2)} GiB`;
}

function formatPercent(ratio?: number | null) {
  if (ratio == null || !Number.isFinite(ratio)) return "—";
  return `${Math.round(ratio * 100)}%`;
}

function formatPsi(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(2)}%`;
}

function formatRate(bytes?: number | null) {
  if (bytes == null || !Number.isFinite(bytes)) return "—";
  return `${(bytes / (1024 ** 2)).toFixed(1)} MiB/s`;
}

function formatAge(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function translateReason(t: TFunction, reason: string) {
  const [code, detail] = reason.split(":", 2);
  const key = `system_health.resource.reason.${code}`;
  const translated = t(key);
  if (translated === key) return reason;
  return detail ? `${translated} (${detail})` : translated;
}

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
  const pressure = health.data?.resource_pressure;
  const controllerMode = pressure?.controller_mode
    ?? (pressure?.status === "paused" ? "critical" : pressure?.status === "warning" ? "constrained" : "normal");
  const hasSplitReasons = pressure?.hard_reasons !== undefined || pressure?.soft_reasons !== undefined;
  const hardReasons = (pressure?.hard_reasons
    ?? (!hasSplitReasons && controllerMode === "critical" ? pressure?.reasons : [])
    ?? []).map((reason) => translateReason(t, reason));
  const softReasons = (pressure?.soft_reasons
    ?? (!hasSplitReasons && controllerMode !== "critical" ? pressure?.reasons : [])
    ?? []).map((reason) => translateReason(t, reason));
  const displayedControllerMode = hardReasons.length > 0
    ? "critical"
    : controllerMode === "normal" && softReasons.length > 0
      ? "constrained"
      : controllerMode;
  const pressureTone = displayedControllerMode === "critical"
    ? "border-danger/30 bg-danger-subtle text-danger"
    : displayedControllerMode === "constrained"
      ? "border-warning/30 bg-warning-subtle text-warning"
      : "border-success/30 bg-success-subtle text-success";
  const budget = pressure?.budget;
  const effectiveBudget = budget?.effective_throughput_scale ?? budget?.throughput_scale;
  const computedBudget = budget?.computed_throughput_scale ?? budget?.throughput_scale;
  const rolloutBudgetCap = budget?.rollout_max_scale;
  const reservation = budget?.reservation;
  const profileBudgets = Object.values(budget?.profiles ?? {});
  const allowedProfiles = profileBudgets.filter((profile) => profile.allowed).length;
  const outboxEntries = Object.entries(health.data?.business?.outboxes ?? {});
  const outboxBacklog = outboxEntries.reduce(
    (total, [, box]) => total + (box.waiting ?? 0) + (box.processing ?? 0),
    0,
  );

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
              {pressure ? (
                <section
                  className={`mb-4 rounded-lg border p-4 ${pressureTone}`}
                  aria-live="polite"
                  aria-label={t("system_health.resource.title")}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="flex items-center gap-2">
                        <h2 className="font-semibold">{t("system_health.resource.title")}</h2>
                        <StatusBadge
                          status={displayedControllerMode}
                          label={t(`system_health.resource.mode.${displayedControllerMode}`)}
                        />
                        {budget?.governance_mode ? (
                          <span className="rounded-full border border-current/25 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide">
                            {t(`system_health.resource.governance.${budget.governance_mode}`)}
                          </span>
                        ) : null}
                      </div>
                      {hardReasons.length > 0 ? (
                        <p className="mt-1 text-xs opacity-90">
                          <span className="font-semibold">{t("system_health.resource.hard_reasons")}:</span>{" "}
                          {hardReasons.join(" · ")}
                        </p>
                      ) : null}
                      {softReasons.length > 0 ? (
                        <p className="mt-1 text-xs opacity-90">
                          <span className="font-semibold">{t("system_health.resource.soft_reasons")}:</span>{" "}
                          {softReasons.join(" · ")}
                        </p>
                      ) : null}
                      {hardReasons.length === 0 && softReasons.length === 0 ? (
                        <p className="mt-1 text-xs opacity-90">{t("system_health.resource.no_reasons")}</p>
                      ) : null}
                    </div>
                    {pressure.sampled_at ? (
                      <span className="text-xs opacity-80">{fmt.time(pressure.sampled_at)}</span>
                    ) : null}
                  </div>

                  <div className="mt-4 grid grid-cols-2 gap-3 text-sm md:grid-cols-4 xl:grid-cols-7">
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.memory")}</p>
                      <p className="font-mono font-medium">{formatCapacity(pressure.memory?.available_bytes)}</p>
                      <p className="text-xs opacity-75">{formatPercent(pressure.memory?.available_ratio)}</p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.swap")}</p>
                      <p className="font-mono font-medium">{formatCapacity(pressure.swap?.free_bytes)}</p>
                      <p className="text-xs opacity-75">{formatPercent(pressure.swap?.free_ratio)}</p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.memory_psi")}</p>
                      <p className="font-mono font-medium">{formatPsi(pressure.psi?.memory_full_avg10)}</p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.io_psi")}</p>
                      <p className="font-mono font-medium">{formatPsi(pressure.psi?.io_full_avg10)}</p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.redis")}</p>
                      <p className="font-mono font-medium">{formatPercent(pressure.redis?.usage_ratio)}</p>
                      <p className="text-xs opacity-75">
                        {pressure.redis?.writable === false
                          ? t("system_health.resource.read_only")
                          : pressure.redis?.writable === true
                            ? t("system_health.resource.writable")
                            : "—"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.concurrency")}</p>
                      <p className="font-mono font-medium">
                        {pressure.download_concurrency
                          ? `${pressure.download_concurrency.effective} / ${pressure.download_concurrency.cap}`
                          : "—"}
                      </p>
                      <p className="text-xs opacity-75">{t("system_health.resource.effective_cap")}</p>
                    </div>
                    <div>
                      <p className="text-xs opacity-75">{t("system_health.resource.budget")}</p>
                      <p className="font-mono font-medium">{formatPercent(effectiveBudget)}</p>
                      <p className="text-xs opacity-75">
                        {budget?.generation != null
                          ? t("system_health.resource.budget_generation", { generation: budget.generation })
                          : "—"}
                        {profileBudgets.length > 0
                          ? ` · ${allowedProfiles}/${profileBudgets.length} ${t("system_health.resource.profiles")}`
                          : ""}
                      </p>
                      {computedBudget != null && effectiveBudget != null && computedBudget !== effectiveBudget ? (
                        <p className="text-[10px] opacity-70">
                          {t("system_health.resource.computed_budget", { value: formatPercent(computedBudget) })}
                        </p>
                      ) : null}
                      {rolloutBudgetCap != null && rolloutBudgetCap < 1 ? (
                        <p className="text-[10px] opacity-70">
                          {t("system_health.resource.rollout_cap", { value: formatPercent(rolloutBudgetCap) })}
                        </p>
                      ) : null}
                      {reservation?.active_count != null ? (
                        <p className="text-[10px] opacity-70">
                          {t("system_health.resource.lease_summary", {
                            count: reservation.active_count,
                            memory: formatCapacity(reservation.reserved_bytes),
                          })}
                        </p>
                      ) : null}
                      {(budget?.read_bytes_per_second != null || budget?.write_bytes_per_second != null) ? (
                        <p className="text-[10px] opacity-70">
                          {t("system_health.resource.read_short")} {formatRate(budget?.read_bytes_per_second)} · {t("system_health.resource.write_short")} {formatRate(budget?.write_bytes_per_second)}
                        </p>
                      ) : null}
                    </div>
                  </div>
                </section>
              ) : null}

              {health.error && (
                <div className="mb-4">
                  <ErrorState message={(health.error as Error).message} onRetry={() => void health.refetch()} />
                </div>
              )}

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
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

              {outboxEntries.length > 0 ? (
                <section className="card mt-4 p-4" aria-label={t("system_health.outboxes.title")}>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <h2 className="font-semibold">{t("system_health.outboxes.title")}</h2>
                    <span className="text-xs text-muted">
                      {t("system_health.outboxes.backlog", { count: outboxBacklog })}
                    </span>
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-5">
                    {outboxEntries.map(([name, box]) => {
                      const status = (box.failed ?? 0) > 0
                        ? "failed"
                        : (box.processing ?? 0) > 0
                          ? "running"
                          : (box.waiting ?? 0) > 0
                            ? "pending"
                            : "complete";
                      const nameKey = `system_health.outboxes.name.${name}`;
                      const translatedName = t(nameKey);
                      return (
                        <article key={name} className="rounded-md border border-border bg-surface p-3">
                          <div className="flex items-center justify-between gap-2">
                            <h3 className="truncate text-sm font-medium">
                              {translatedName === nameKey ? name : translatedName}
                            </h3>
                            <StatusBadge status={status} className="px-2 py-0 text-[10px]" />
                          </div>
                          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                            <dt className="text-muted">{t("system_health.outboxes.waiting")}</dt>
                            <dd className="text-right font-mono">{box.waiting ?? 0}</dd>
                            <dt className="text-muted">{t("system_health.outboxes.processing")}</dt>
                            <dd className="text-right font-mono">{box.processing ?? 0}</dd>
                            <dt className="text-muted">{t("system_health.outboxes.oldest")}</dt>
                            <dd className="text-right font-mono">{formatAge(box.oldest_age_seconds)}</dd>
                            <dt className="text-muted">{t("system_health.outboxes.failures")}</dt>
                            <dd className={(box.failed ?? 0) > 0 ? "text-right font-mono text-danger" : "text-right font-mono"}>{box.failed ?? 0}</dd>
                          </dl>
                        </article>
                      );
                    })}
                  </div>
                </section>
              ) : null}

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
