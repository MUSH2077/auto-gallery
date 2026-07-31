"use client";
import { useEffect, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ConfirmDialog, Modal, PageShell, StatusBadge, PermissionGuard, UrlTabs } from "@/components";
import { BackupManagementContent } from "@/app/admin/settings/backup/page";
import ChartFrame from "@/components/charts/ChartFrame";
import StorageColonnade, { type StorageColonnadeGroup } from "@/components/charts/StorageColonnade";
import TickDonut from "@/components/charts/TickDonut";
import type { ChartDatum } from "@/components/charts/types";
import { useNotifications } from "@/components/NotificationCenter";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { useRouter, useSearchParams } from "next/navigation";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";

type Severity = "error" | "warning" | "info";
type DataManagementTab = "overview" | "maintenance" | "backups" | "danger";
const DATA_MANAGEMENT_TABS: readonly DataManagementTab[] = ["overview", "maintenance", "backups", "danger"];

function formatSize(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${(mb * 1024).toFixed(0)} KB`;
}

function severityBadge(s: string, t: (k: string) => string) {
  const sev: Severity = (["error","warning","info"].includes(s) ? s : "info") as Severity;
  const map: Record<Severity, string> = {
    error: "bg-danger-subtle text-danger border-danger/30",
    warning: "bg-warning-subtle text-warning border-warning/30",
    info: "bg-accent-subtle text-accent border-accent/30",
  };
  const label: Record<Severity, string> = {
    error: t("datamgmt.severity_error"),
    warning: t("datamgmt.severity_warning"),
    info: t("datamgmt.severity_info"),
  };
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full border ${map[sev]}`}>
      {label[sev]}
    </span>
  );
}

export default function DataManagementPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const activeTab: DataManagementTab = DATA_MANAGEMENT_TABS.includes(requestedTab as DataManagementTab)
    ? requestedTab as DataManagementTab
    : "overview";
  const paramsKey = searchParams.toString();
  const qc = useQueryClient();
  const notify = useNotifications();
  const toast = useToast();
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [resetLedger, setResetLedger] = useState(false);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [integrityItems, setIntegrityItems] = useState<{ type: string; description: string; count: number; items: any[] } | null>(null);
  const [dangerConfirm, setDangerConfirm] = useState("");

  useEffect(() => {
    if (!requestedTab || requestedTab === activeTab) return;
    const next = new URLSearchParams(paramsKey);
    next.set("tab", activeTab);
    router.replace(`${adminRoutes.dataManagement}?${next.toString()}`, { scroll: false });
  }, [activeTab, paramsKey, requestedTab, router]);

  // ── Data queries ──
  const systemInfo = useQuery({
    queryKey: ["system-info"],
    queryFn: () => api.getSystemInfo(),
    enabled: activeTab === "overview",
    refetchInterval: 60000,
    placeholderData: (previousData) => previousData,
  });
  const storageBreakdown = useQuery({
    queryKey: ["storage-breakdown"],
    queryFn: () => api.getStorageBreakdown(),
    enabled: activeTab === "overview",
    placeholderData: (previousData) => previousData,
  });
  const creatorCount = useQuery({ queryKey: queryKeys.creators.count, queryFn: () => api.countCreators(), enabled: activeTab === "overview" });
  const subCount = useQuery({ queryKey: queryKeys.subscriptions.count, queryFn: () => api.countSubscriptions(), enabled: activeTab === "overview" });
  const integrity = useQuery({ queryKey: ["integrity-check"], queryFn: () => api.getIntegrityCheck(), enabled: false });

  // ── Mutations ──
  const cleanupJSON = useMutation({
    mutationFn: () => api.cleanupMetadataJSONs(),
    onSuccess: (d: any) => setResult({ ok: true, msg: t("datamgmt.cleanup_json_done").replace("{count}", String(d.removed)) }),
    onError: (e) => setResult({ ok: false, msg: (e as Error).message }),
  });

  const importFromDisk = useMutation({
    mutationFn: () => api.importFromDisk(resetLedger ? { reset_ledger: true } : {}),
    onMutate: () => setResult(null),
    onSuccess: (d: any) => {
      const title = t("datamgmt.disk_import");
      toast.success({
        title,
        message: d.message,
        action: { label: t("jobs.task_detail"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${d.job_id}`) },
      });
      notify.startOperationJob(d.job_id, "admin-disk-import", title);
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (e) => toast.error({ title: t("datamgmt.disk_import"), message: (e as Error).message }),
  });

  const reenrichCreators = useMutation({
    mutationFn: () => api.reenrichCreators(),
    onMutate: () => setResult(null),
    onSuccess: (d: any) => {
      const title = t("datamgmt.reenrich");
      toast.success({
        title,
        message: d.message,
        action: { label: t("jobs.task_detail"), onClick: () => router.push(`/admin/jobs?tab=admin&task=${d.job_id}`) },
      });
      notify.startOperationJob(d.job_id, "admin-creator-reenrich", title);
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (e) => toast.error({ title: t("datamgmt.reenrich"), message: (e as Error).message }),
  });

  const rebuildLibrary = useMutation({
    mutationFn: () => api.rebuildLibrary(),
    onSuccess: (d: any) => {
      const title = t("datamgmt.library_rebuild");
      setResult({ ok: true, msg: d.message });
      notify.startOperationJob(d.job_id, "admin-rebuild", title);
      qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    },
    onError: (e) => setResult({ ok: false, msg: (e as Error).message }),
  });

  const rebuildSearch = useMutation({
    mutationFn: () => api.reindexSearch(),
    onSuccess: () => setResult({ ok: true, msg: t("settings.reindex_started") }),
    onError: (e) => setResult({ ok: false, msg: (e as Error).message }),
  });

  const runIntegrity = () => integrity.refetch();

  const dangerActions = [
    { key: "works", title: t("datamgmt.danger_clear_works"), desc: t("datamgmt.danger_clear_works_desc"), color: "red" },
    { key: "creators", title: t("datamgmt.danger_clear_creators"), desc: t("datamgmt.danger_clear_creators_desc"), color: "red" },
    { key: "subs", title: t("datamgmt.danger_clear_subs"), desc: t("datamgmt.danger_clear_subs_desc"), color: "red" },
    { key: "tags", title: t("datamgmt.danger_clear_tags"), desc: t("datamgmt.danger_clear_tags_desc"), color: "orange" },
    { key: "jobs", title: t("datamgmt.danger_clear_jobs"), desc: t("datamgmt.danger_clear_jobs_desc"), color: "orange" },
    { key: "all", title: t("datamgmt.danger_clear_all"), desc: t("datamgmt.danger_clear_all_desc"), color: "red" },
    { key: "settings", title: t("datamgmt.danger_reset_settings"), desc: t("datamgmt.danger_reset_settings_desc"), color: "blue" },
  ];

  const clearEntityForAction = (key: string) => key === "subs" ? "downloads" : key;

  const clearOperationMutation = useMutation({
    mutationFn: ({ entity }: { key: string; entity: string; title: string }) => api.startClearOperation(entity),
    onMutate: (vars) => {
      setActiveAction(vars.key);
    },
    onSuccess: (data, vars) => {
      setResult({ ok: true, msg: t("datamgmt.action_queued", { action: vars.title }) });
      notify.startOperationJob(data.job_id, "admin-clear", vars.title, { entity: vars.entity });
      if (vars.entity === "creators" || vars.entity === "all") {
        qc.setQueryData(queryKeys.creators.count, { count: 0 });
        qc.setQueryData(queryKeys.subscriptions.count, { count: 0 });
        qc.setQueriesData({ queryKey: ["creators", "list"] }, { items: [], total: 0 });
        qc.setQueriesData({ queryKey: ["subscriptions", "list"] }, []);
      }
      setConfirmAction(null);
    },
    onError: (e) => {
      setResult({ ok: false, msg: (e as Error).message });
      setConfirmAction(null);
    },
    onSettled: () => {
      setActiveAction(null);
    },
  });

  const dangerUnlocked = dangerConfirm === t("datamgmt.danger_confirm_text");
  const clearOperation = notify.operationJob?.kind === "admin-clear" ? notify.operationJob : null;

  // Computed
  const info = systemInfo.data;
  const breakdown = storageBreakdown.data;
  const issues = integrity.data?.issues || [];
  const dbStats = breakdown?.db_stats || integrity.data?.db_stats;
  const totalSourceSize = useMemo(() => (
    breakdown?.sources
      ? Object.values(breakdown.sources).reduce((sum, storage) => sum + storage.size_mb, 0)
      : 0
  ), [breakdown?.sources]);
  const sourceEntries = useMemo(() => (
    breakdown?.sources
      ? Object.entries(breakdown.sources).sort(([, left], [, right]) => right.size_mb - left.size_mb)
      : []
  ), [breakdown?.sources]);
  const creatorEntries = breakdown?.creator_tree || [];
  const unlinkedRepositories = breakdown?.unlinked_repositories || [];
  const sourceStorageData = useMemo<ChartDatum[]>(() => (
    sourceEntries.map(([source, storage]) => ({
      id: source,
      label: source,
      value: storage.size_mb,
      colorRole: `source:${source}`,
      description: t("charts.storage_source_row", {
        source,
        size: formatSize(storage.size_mb),
        count: storage.work_count,
      }),
    }))
  ), [sourceEntries, t]);
  const sourceStorageLeader = sourceStorageData[0];
  const storageGroups = useMemo<StorageColonnadeGroup[]>(() => (
    creatorEntries.map((creator) => ({
      id: creator.creator_id,
      label: creator.display_name,
      value: creator.size_mb,
      href: `/admin/creators/${creator.creator_id}`,
      workCount: creator.work_count,
      children: creator.repositories.map((repository) => ({
        id: `${creator.creator_id}:${repository.source}:${repository.directory_name}`,
        label: repository.directory_name,
        value: repository.size_mb,
        href: repository.repository_id
          ? adminRoutes.repository(repository.repository_id)
          : undefined,
        source: repository.source,
        sourceLabel: repository.source_display_name,
        workCount: repository.work_count,
      })),
    }))
  ), [creatorEntries]);
  const creatorStorageLeader = storageGroups[0];
  const integrityItemKeys = (integrityItems?.items || []).map(
    (item, index) => item.id || item.path || item.file_name || item.name || `item:${index}`,
  );
  const issueEntrance = useStaggeredEntrance(issues.map((issue) => issue.type));
  const integrityItemEntrance = useStaggeredEntrance(integrityItemKeys);

  return (
    <PermissionGuard module="system">
    <PageShell>
      <PageHeader title={t("datamgmt.title")} description={t("datamgmt.desc")} />
      <div data-page-primary-content>
        <UrlTabs
          activeId={activeTab}
          ariaLabel={t("datamgmt.sections")}
          tabs={[
            { id: "overview", label: t("datamgmt.overview_tab"), href: `${adminRoutes.dataManagement}?tab=overview` },
            { id: "maintenance", label: t("datamgmt.maintenance_tab"), href: `${adminRoutes.dataManagement}?tab=maintenance` },
            { id: "backups", label: t("backup.title"), href: `${adminRoutes.dataManagement}?tab=backups` },
            { id: "danger", label: t("datamgmt.danger_title"), href: `${adminRoutes.dataManagement}?tab=danger` },
          ]}
        />

      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm flex items-center justify-between ${result.ok ? "bg-success-subtle border border-success/30 text-success" : "bg-danger-subtle border border-danger/30 text-danger"}`}>
          <span>{result.msg}</span>
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">{t("datamgmt.dismiss")}</button>
        </div>
      )}

      {clearOperation && (
        <div className={`mb-4 rounded-md border p-3 text-sm ${
          clearOperation.status === "error"
            ? "border-danger/30 bg-danger-subtle text-danger"
            : clearOperation.status === "completed"
              ? "border-success/30 bg-success-subtle text-success"
              : "border-accent/30 bg-accent-subtle text-accent"
        }`}>
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">{clearOperation.title}</span>
            <StatusBadge status={clearOperation.status} className="uppercase" />
          </div>
          <div className="mt-1 text-xs opacity-80">
            {clearOperation.error || clearOperation.result?.message || clearOperation.progress?.label || t("datamgmt.queued_background")}
          </div>
        </div>
      )}

      {activeTab === "overview" && (<>
      <section
        aria-labelledby="data-ledger-title"
        className="mb-6 overflow-hidden rounded-md border border-border bg-border"
      >
        <div className="bg-surface px-4 py-3">
          <h2 id="data-ledger-title" className="text-sm font-semibold text-fg">{t("charts.metric_ledger_title")}</h2>
          <p className="mt-1 text-xs text-muted">{t("charts.metric_ledger_desc")}</p>
        </div>
        <dl className="grid grid-cols-2 gap-px md:grid-cols-4 2xl:grid-cols-7">
          {[
            { label: t("datamgmt.stats_works"), value: dbStats?.works ?? "-" },
            { label: t("datamgmt.stats_assets"), value: dbStats?.assets ?? "-" },
            { label: t("datamgmt.stats_creators"), value: creatorCount.data?.count ?? "-" },
            { label: t("datamgmt.stats_subs"), value: subCount.data?.count ?? "-" },
            { label: t("datamgmt.stats_tags"), value: dbStats?.tags ?? "-" },
            { label: t("datamgmt.stats_downloads"), value: info ? formatSize(info.downloads_size_mb) : "-" },
            { label: t("datamgmt.stats_library"), value: info ? formatSize(info.library_size_mb) : "-" },
          ].map((metric, index) => (
            <div
              key={metric.label}
              className={`min-h-24 min-w-0 overflow-hidden bg-surface px-4 py-3 ${index === 6 ? "col-span-2 2xl:col-span-1" : ""}`}
            >
              <dt className="flex min-w-0 items-start gap-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted [overflow-wrap:anywhere]">
                <span className="shrink-0 font-mono text-accent">{String(index + 1).padStart(2, "0")}</span>
                {metric.label}
              </dt>
              <dd className="mt-3 font-mono text-xl font-semibold tabular-nums text-fg [overflow-wrap:anywhere]">{metric.value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <div className="mb-6 grid grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.35fr)]">
        <ChartFrame
          title={t("datamgmt.storage_title")}
          insight={sourceStorageLeader
            ? t("charts.storage_insight", {
              source: sourceStorageLeader.label,
              size: formatSize(sourceStorageLeader.value),
              share: fmt.number(
                totalSourceSize > 0 ? (sourceStorageLeader.value / totalSourceSize) * 100 : 0,
                { maximumFractionDigits: 1 },
              ),
            })
            : t("datamgmt.storage_no_data")}
          description={t("charts.storage_encoding")}
          testId="storage-source-chart"
        >
          {sourceStorageData.length ? (
            <TickDonut
              data={sourceStorageData}
              otherLabel={t("charts.other_sources")}
              formatValue={formatSize}
            />
          ) : (
            <p className="py-8 text-center text-sm text-muted">{t("datamgmt.storage_no_data")}</p>
          )}
        </ChartFrame>

        <ChartFrame
          title={t("datamgmt.storage_creators_title")}
          insight={creatorStorageLeader
            ? t("charts.storage_tree_insight", {
              creator: creatorStorageLeader.label,
              size: formatSize(creatorStorageLeader.value),
            })
            : t("datamgmt.storage_no_data")}
          description={t("charts.storage_tree_encoding")}
          testId="creator-storage-chart"
        >
          {storageGroups.length ? (
            <StorageColonnade
              groups={storageGroups}
              formatValue={formatSize}
              worksLabel={t("datamgmt.storage_works_label")}
              repositoriesLabel={t("datamgmt.storage_repositories_label")}
            />
          ) : (
            <p className="py-8 text-center text-sm text-muted">{t("datamgmt.storage_no_data")}</p>
          )}

          {unlinkedRepositories.length > 0 ? (
            <section className="mt-5 border-t border-border pt-4" aria-labelledby="unlinked-storage-title">
              <h3 id="unlinked-storage-title" className="text-sm font-semibold text-warning">{t("datamgmt.storage_unlinked_title")}</h3>
              <p className="mt-1 text-xs text-muted">{t("datamgmt.storage_unlinked_desc")}</p>
              <ul className="mt-3 divide-y divide-border rounded-md border border-border bg-subtle">
                {unlinkedRepositories.map((repository) => (
                  <li
                    key={`${repository.disk_source}:${repository.directory_name}`}
                    className="grid min-h-11 grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-3 py-2 text-xs"
                  >
                    <span className="min-w-0 truncate text-fg">
                      <span className="mr-2 font-semibold">{repository.source}</span>
                      {repository.directory_name}
                    </span>
                    <span className="font-mono tabular-nums text-muted">{formatSize(repository.size_mb)}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </ChartFrame>
      </div>

      </>)}

      {activeTab === "maintenance" && (<>
      {/* ═══ Integrity Check ═══ */}
      <div className="card p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("datamgmt.integrity_title")}</h3>
          <button
            onClick={runIntegrity}
            disabled={integrity.isFetching}
            className="btn-primary px-3 py-1.5 text-xs"
          >
            {integrity.isFetching ? t("datamgmt.integrity_running") : t("datamgmt.integrity_run")}
          </button>
        </div>

        {integrity.data ? (
          <>
            {issues.length === 0 ? (
              <div className="text-center py-6 text-success">
                <div className="text-lg mb-1">&#10003;</div>
                <p className="text-sm font-medium">{t("datamgmt.integrity_clean")}</p>
                <p className="text-xs text-muted mt-1">{t("datamgmt.integrity_clean_desc")}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {issues.map((issue, index) => {
                  const entrance = issueEntrance(issue.type, index);
                  return (
                  <div key={issue.type} style={entrance.style} className={`${entrance.className} border rounded-lg p-3 flex items-center justify-between ${
                    issue.severity === "error" ? "border-danger/30 bg-danger-subtle/50" :
                    issue.severity === "warning" ? "border-warning/30 bg-warning-subtle/50" :
                    "border-accent/30 bg-accent-subtle/50"
                  }`}>
                    <div className="flex items-center gap-3">
                      {severityBadge(issue.severity, t)}
                      <div>
                        <p className="text-sm font-medium">
                          {issue.type === "orphaned_download_files" && t("datamgmt.integrity_orphaned_files")}
                          {issue.type === "missing_thumbnails" && t("datamgmt.integrity_missing_thumbs")}
                          {issue.type === "orphaned_creators" && t("datamgmt.integrity_orphaned_creators")}
                          {issue.type === "orphaned_tags" && t("datamgmt.integrity_orphaned_tags")}
                          {issue.type === "dead_links" && t("datamgmt.integrity_dead_links")}
                          <span className="text-muted font-normal ml-1">({issue.count})</span>
                        </p>
                        <p className="text-xs text-muted">{issue.description}</p>
                      </div>
                    </div>
                    {issue.items && issue.items.length > 0 && (
                      <button
                        onClick={() => setIntegrityItems(issue)}
                        className="shrink-0 text-xs text-accent hover:underline"
                      >
                        {t("datamgmt.integrity_view_items")} ({Math.min(issue.items.length, 50)})
                      </button>
                    )}
                  </div>
                  );
                })}
              </div>
            )}

            {dbStats && (
              <div className="mt-4 pt-3 border-t">
                <h4 className="text-xs font-medium text-muted mb-2">{t("datamgmt.integrity_db_stats")}</h4>
                <div className="grid grid-cols-3 md:grid-cols-5 gap-2">
                  {Object.entries(dbStats).map(([tbl, count]) => (
                    <div key={tbl} className="text-center bg-subtle rounded p-2">
                      <div className="text-sm font-mono font-bold">{count}</div>
                      <div className="text-[10px] text-muted">{tbl}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {integrity.data.checked_at && (
              <p className="text-xs text-muted mt-3">
                {t("datamgmt.integrity_checked_at")}: {fmt.dateTime(integrity.data.checked_at)}
              </p>
            )}
          </>
        ) : (
          <p className="text-sm text-muted text-center py-6">
            {t("datamgmt.integrity_clean_desc")}
          </p>
        )}

        {/* Integrity items modal */}
        {integrityItems && (
          <Modal
            open
            onClose={() => setIntegrityItems(null)}
            title={t("datamgmt.integrity_items_modal_title")
              .replace("{type}", integrityItems.description)
              .replace("{count}", String(integrityItems.count))}
          >
              <div className="space-y-1 max-h-96 overflow-auto">
                {integrityItems.items.map((item, i) => {
                  const itemKey = integrityItemKeys[i];
                  const entrance = integrityItemEntrance(itemKey, i);
                  return (
                  <div key={itemKey} className={`${entrance.className} text-xs p-2 bg-subtle rounded flex items-center justify-between`} style={entrance.style}>
                    <div className="truncate flex-1">
                      {item.path && <span className="font-mono text-fg">{item.path}</span>}
                      {item.file_name && !item.path && <span className="font-mono">{item.file_name}</span>}
                      {item.name && !item.file_name && !item.path && <span>{item.name}</span>}
                      {item.id && <span className="text-muted ml-1">({item.id})</span>}
                    </div>
                    {item.file_count !== undefined && (
                      <span className="text-muted ml-2 shrink-0">{item.file_count} {t("datamgmt.integrity_files")}</span>
                    )}
                    {item.asset_id && (
                      <span className="text-muted ml-2 shrink-0 text-[10px]">{item.source}/{item.source_work_id}</span>
                    )}
                  </div>
                  );
                })}
              </div>
          </Modal>
        )}
      </div>

      <div className="mb-6">
        {/* ═══ Cleanup Tools ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.cleanup_title")}</h3>
          <div className="space-y-3">
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("settings.reindex_label")}</p>
                <p className="text-xs text-muted">{t("settings.search_index.desc")}</p>
              </div>
              <button onClick={() => rebuildSearch.mutate()} disabled={rebuildSearch.isPending}
                className="btn-primary ml-3 shrink-0 text-xs">
                {rebuildSearch.isPending ? "..." : t("settings.reindex")}
              </button>
            </div>
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.cleanup_json")}</p>
                <p className="text-xs text-muted">{t("datamgmt.cleanup_json_desc")}</p>
              </div>
              <button onClick={() => cleanupJSON.mutate()} disabled={cleanupJSON.isPending}
                className="btn-ghost ml-3 min-h-11 shrink-0 text-xs text-warning">
                {cleanupJSON.isPending ? "..." : t("datamgmt.cleanup_json_btn")}
              </button>
            </div>
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.library_rebuild")}</p>
                <p className="text-xs text-muted">{t("datamgmt.library_rebuild_desc")}</p>
              </div>
              <button onClick={() => rebuildLibrary.mutate()} disabled={rebuildLibrary.isPending}
                className="btn-primary ml-3 shrink-0 text-xs">
                {rebuildLibrary.isPending ? "..." : t("datamgmt.library_rebuild_btn")}
              </button>
            </div>
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.disk_import")}</p>
                <p className="text-xs text-muted">{t("datamgmt.disk_import_desc")}</p>
                <label className="mt-1.5 flex items-center gap-1.5 text-xs text-muted cursor-pointer">
                  <input type="checkbox" checked={resetLedger} onChange={(e) => setResetLedger(e.target.checked)}
                    className="h-3.5 w-3.5 rounded border-border" />
                  {t("datamgmt.disk_import_reset_ledger")}
                </label>
              </div>
              <button onClick={() => importFromDisk.mutate()} disabled={importFromDisk.isPending}
                className="btn-primary shrink-0 ml-3 text-xs">
                {importFromDisk.isPending ? "..." : t("datamgmt.disk_import_btn")}
              </button>
            </div>
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.reenrich")}</p>
                <p className="text-xs text-muted">{t("datamgmt.reenrich_desc")}</p>
              </div>
              <button onClick={() => reenrichCreators.mutate()} disabled={reenrichCreators.isPending}
                className="btn-primary shrink-0 ml-3 text-xs">
                {reenrichCreators.isPending ? "..." : t("datamgmt.reenrich_btn")}
              </button>
            </div>
          </div>
        </div>
      </div>

      </>)}

      {activeTab === "backups" && (
        <section role="tabpanel" aria-label={t("backup.title")}>
          <BackupManagementContent />
        </section>
      )}

      {/* ═══ Danger Zone ═══ */}
      {activeTab === "danger" && (
      <div className="card border-danger p-4 dark:border-danger">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-danger text-lg">&#9888;</span>
          <h3 className="font-medium text-sm text-danger">{t("datamgmt.danger_title")}</h3>
        </div>
        <p className="text-xs text-danger mb-4">{t("datamgmt.danger_warning")}</p>

        {/* Confirmation input */}
        <div className="mb-4">
          <input
            type="text"
            value={dangerConfirm}
            onChange={(e) => setDangerConfirm(e.target.value)}
            placeholder={t("datamgmt.danger_confirm_text")}
            className="input w-full max-w-sm border-danger dark:border-danger"
          />
          <p className="text-xs text-muted mt-1">{t("datamgmt.danger_confirm_hint")}</p>
        </div>

        <div className="space-y-2">
          {dangerActions.map((a) => {
            const actionEntity = clearEntityForAction(a.key);
            const isCurrentOperation = clearOperation?.status === "running" && clearOperation.meta?.entity === actionEntity;
            const isPending = (clearOperationMutation.isPending && activeAction === a.key) || isCurrentOperation;
            return (
              <div key={a.key} className={`flex min-w-0 flex-col items-stretch gap-3 rounded-md border-l-4 p-3 sm:flex-row sm:items-center sm:justify-between ${
                a.color === "red" ? "border-l-danger bg-danger-subtle/30" :
                a.color === "orange" ? "border-l-warning bg-warning-subtle/30" :
                "border-l-accent bg-accent-subtle/30"
              }`}>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-fg">{a.title}</p>
                  <p className="text-xs text-muted">{a.desc}</p>
                </div>
                <button
                  onClick={() => setConfirmAction(a.key)}
                  disabled={!dangerUnlocked || isPending}
                  className={`min-h-11 w-full shrink-0 rounded-md px-4 py-1.5 text-xs font-semibold text-white transition-opacity disabled:opacity-30 sm:ml-3 sm:w-auto ${
                    a.color === "red" ? "bg-danger hover:bg-danger/90" :
                    a.color === "orange" ? "bg-warning text-canvas hover:bg-warning/90" :
                    "bg-accent hover:bg-accent/90"
                  }`}
                >
                  {isPending ? "..." : a.title}
                </button>
              </div>
            );
          })}
        </div>
      </div>
      )}
      </div>

      {/* Global confirm dialog */}
      {confirmAction && (
        <ConfirmDialog
          open
          title={t("datamgmt.confirm_title").replace("{action}", dangerActions.find((a) => a.key === confirmAction)?.title || confirmAction)}
          message={t("datamgmt.confirm_msg").replace("{action}", confirmAction)}
          onConfirm={() => {
            const action = dangerActions.find((a) => a.key === confirmAction);
            if (action) {
              clearOperationMutation.mutate({
                key: action.key,
                entity: clearEntityForAction(action.key),
                title: action.title,
              });
            }
          }}
          onCancel={() => setConfirmAction(null)}
          isPending={clearOperationMutation.isPending && activeAction === confirmAction}
        />
      )}
    </PageShell>
    </PermissionGuard>
  );
}
