"use client";
import { useCallback, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, type AdminOperationAccepted, type ClearEntity, type ImportFromDiskRequest } from "@/lib/api";
import { PageHeader, ConfirmDialog, Modal, PageShell, PermissionGuard } from "@/components";
import ChartFrame from "@/components/charts/ChartFrame";
import StorageColonnade, { type StorageColonnadeGroup } from "@/components/charts/StorageColonnade";
import TickDonut from "@/components/charts/TickDonut";
import type { ChartDatum } from "@/components/charts/types";
import { useNotifications } from "@/components/NotificationCenter";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { useRouter } from "next/navigation";
import { useI18nFormat } from "@/lib/i18n-format";
import { adminRoutes } from "@/lib/adminRoutes";
import { AdminOperationStatus } from "@/components/AdminOperationStatus";
import { useAdminOperation } from "@/lib/useAdminOperation";

type Severity = "error" | "warning" | "info";
type IntegrityResult = {
  issues: { type: string; severity: string; count: number; description: string; items: any[] }[];
  db_stats: Record<string, number>;
  checked_at: string;
  message?: string;
};
type BackupResult = {
  status: string;
  filename: string;
  size_bytes: number;
  size_mb: number;
  contents: string[];
  component_sizes: Record<string, number>;
  message?: string;
};
type MetadataCleanupResult = {
  status: string;
  removed: number;
  skipped: number;
  failed: number;
  skipped_by_reason?: Record<string, number>;
  errors?: { path?: string; reason?: string }[];
  message?: string;
};
type MessageResult = { message?: string };
type ClearResult = { status: string; message?: string; deleted?: Record<string, number> };
type ClearVariables = { entity: ClearEntity; confirmation: string; title: string };

function formatSize(mb: number): string {
  if (!Number.isFinite(mb)) return "-";
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

function DataManagementContent() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const qc = useQueryClient();
  const notify = useNotifications();
  const toast = useToast();
  const [resetLedger, setResetLedger] = useState(false);
  const [confirmRebuild, setConfirmRebuild] = useState(false);
  const [confirmAction, setConfirmAction] = useState<ClearEntity | null>(null);
  const [integrityItems, setIntegrityItems] = useState<{ type: string; description: string; count: number; items: any[] } | null>(null);

  // ── Data queries ──
  const systemInfo = useQuery({
    queryKey: ["system-info"],
    queryFn: () => api.getSystemInfo(),
    refetchInterval: 60000,
    placeholderData: (previousData) => previousData,
  });
  const storageBreakdown = useQuery({
    queryKey: ["storage-breakdown"],
    queryFn: () => api.getStorageBreakdown(),
    placeholderData: (previousData) => previousData,
  });
  const backups = useQuery({ queryKey: ["backups"], queryFn: () => api.listBackups() });
  const refreshBackups = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["backups"] });
  }, [qc]);
  const refreshDataViews = useCallback(() => {
    void qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
    void qc.invalidateQueries({ queryKey: ["system-info"] });
    void qc.invalidateQueries({ queryKey: ["storage-breakdown"] });
  }, [qc]);
  const refreshClearedDataViews = useCallback(() => {
    refreshDataViews();
    void qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    void qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
    void qc.invalidateQueries({ queryKey: queryKeys.sources });
    void qc.invalidateQueries({ queryKey: queryKeys.works.all });
    void qc.invalidateQueries({ queryKey: queryKeys.tags.all });
    void qc.invalidateQueries({ queryKey: queryKeys.downloadJobs.all });
    void qc.invalidateQueries({ queryKey: queryKeys.importJobs.all });
    void qc.invalidateQueries({ queryKey: ["search"] });
    void qc.invalidateQueries({ queryKey: ["reference-name-anchors"] });
  }, [qc, refreshDataViews]);
  const announceAccepted = useCallback((
    accepted: AdminOperationAccepted,
    title: string,
    message: string,
    meta?: Record<string, unknown>,
  ) => {
    toast.info({
      title,
      message,
      persistent: true,
      action: {
        label: t("jobs.task_detail"),
        onClick: () => router.push(`/admin/jobs?tab=admin&task=${accepted.task_id}`),
      },
    });
    notify.startOperationJob(
      accepted.job_id,
      accepted.operation_type,
      title,
      meta,
      accepted.task_id,
    );
    void qc.invalidateQueries({ queryKey: queryKeys.tasks.all });
  }, [notify, qc, router, t, toast]);
  const integrity = useAdminOperation<IntegrityResult>({
    operationType: "admin-integrity-scan",
    scope: "global",
    startOperation: async () => {
      const accepted = await api.startIntegrityCheck();
      announceAccepted(
        accepted,
        t("datamgmt.integrity_title"),
        t("datamgmt.operation_submitted", { operation: t("datamgmt.integrity_title") }),
      );
      return accepted;
    },
    loadLatest: () => api.getLatestIntegrityCheck(),
  });
  const backupOperation = useAdminOperation<BackupResult>({
    operationType: "admin-backup-create",
    scope: "global",
    startOperation: async () => {
      const accepted = await api.createBackup();
      announceAccepted(
        accepted,
        t("datamgmt.backup_create"),
        t("datamgmt.operation_submitted", { operation: t("datamgmt.backup_create") }),
      );
      return accepted;
    },
    loadLatest: () => api.getLatestBackup(),
    onCompleted: refreshBackups,
  });
  const cleanupJSON = useAdminOperation<MetadataCleanupResult>({
    operationType: "admin-cleanup-metadata-jsons",
    scope: "global",
    startOperation: async () => {
      const accepted = await api.cleanupMetadataJSONs();
      announceAccepted(accepted, t("datamgmt.cleanup_json"), t("datamgmt.cleanup_json_accepted"));
      return accepted;
    },
    loadLatest: () => api.getLatestMetadataJSONCleanup(),
    onCompleted: refreshDataViews,
  });
  const rebuildLibrary = useAdminOperation<MessageResult>({
    operationType: "admin-rebuild",
    scope: "global",
    startOperation: async () => {
      const accepted = await api.rebuildLibrary();
      announceAccepted(accepted, t("datamgmt.cleanup_reindex"), t("datamgmt.cleanup_reindex_accepted"));
      setConfirmRebuild(false);
      return accepted;
    },
    loadLatest: () => api.getLatestLibraryRebuild(),
    onCompleted: refreshDataViews,
  });
  const importFromDisk = useAdminOperation<MessageResult, ImportFromDiskRequest>({
    operationType: "admin-disk-import",
    scope: "global",
    startOperation: async (options) => {
      const accepted = await api.importFromDisk(options);
      announceAccepted(
        accepted,
        t("datamgmt.disk_import"),
        t("datamgmt.operation_submitted", { operation: t("datamgmt.disk_import") }),
      );
      return accepted;
    },
    loadLatest: () => api.getLatestImportFromDisk(),
    onCompleted: refreshDataViews,
  });
  const reenrichCreators = useAdminOperation<MessageResult>({
    operationType: "admin-creator-reenrich",
    scope: "global",
    startOperation: async () => {
      const accepted = await api.reenrichCreators();
      announceAccepted(
        accepted,
        t("datamgmt.reenrich"),
        t("datamgmt.operation_submitted", { operation: t("datamgmt.reenrich") }),
      );
      return accepted;
    },
    loadLatest: () => api.getLatestCreatorReenrichment(),
    onCompleted: refreshDataViews,
  });

  const dangerActions = [
    { key: "all", title: t("datamgmt.danger_clear_all"), desc: t("datamgmt.danger_clear_all_desc"), color: "red" },
  ] satisfies { key: ClearEntity; title: string; desc: string; color: string }[];

  const clearPreview = useQuery({
    queryKey: ["clear-impact-preview", confirmAction],
    queryFn: () => api.previewClearEntity(confirmAction!),
    enabled: !!confirmAction,
    staleTime: 0,
  });
  const clearOperation = useAdminOperation<ClearResult, ClearVariables>({
    operationType: "admin-clear",
    scope: "all",
    startOperation: async ({ entity, confirmation, title }) => {
      const accepted = await api.startClearOperation(entity, confirmation);
      announceAccepted(
        accepted,
        title,
        t("datamgmt.action_queued", { action: title }),
        { entity },
      );
      setConfirmAction(null);
      return accepted;
    },
    loadLatest: () => api.getLatestClearOperation("all"),
    onCompleted: refreshClearedDataViews,
  });

  // Computed
  const info = systemInfo.data;
  const breakdown = storageBreakdown.data;
  const issues = integrity.result?.issues || [];
  const dbStats = breakdown?.db_stats || info?.db_stats;
  const integrityDbStats = integrity.result?.db_stats;
  const overviewError = (systemInfo.isError && !info) || (storageBreakdown.isError && !breakdown);
  const overviewLoading = !overviewError && (!info || !breakdown);
  const lastBackup = backups.data?.backups?.[0];
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
    <PageShell>
      <PageHeader title={t("datamgmt.title")} description={t("datamgmt.desc")} />

      <section
        data-page-primary-content
        aria-labelledby="data-ledger-title"
        className="mb-6 overflow-hidden rounded-md border border-border bg-border"
      >
        <div className="bg-surface px-4 py-3">
          <h2 id="data-ledger-title" className="text-sm font-semibold text-fg">{t("charts.metric_ledger_title")}</h2>
          <p className="mt-1 text-xs text-muted">{t("charts.metric_ledger_desc")}</p>
        </div>
        {overviewError ? (
          <div role="alert" className="m-4 rounded-md border border-danger/30 bg-danger-subtle p-4 text-sm text-danger">
            <p className="font-medium">{t("datamgmt.data_center_error")}</p>
            <button
              type="button"
              className="btn-ghost mt-3 text-danger"
              onClick={() => {
                void systemInfo.refetch();
                void storageBreakdown.refetch();
              }}
            >
              {t("common.retry")}
            </button>
          </div>
        ) : overviewLoading ? (
          <div aria-label={t("datamgmt.data_center_loading")} className="grid grid-cols-2 gap-px bg-border md:grid-cols-4 2xl:grid-cols-7">
            {Array.from({ length: 7 }).map((_, index) => (
              <div key={index} className="min-h-24 animate-pulse bg-surface px-4 py-3">
                <div className="h-3 w-2/3 rounded bg-subtle" />
                <div className="mt-4 h-6 w-1/2 rounded bg-subtle" />
              </div>
            ))}
          </div>
        ) : (
          <dl className="grid grid-cols-2 gap-px md:grid-cols-4 2xl:grid-cols-7">
            {[
              { label: t("datamgmt.stats_works"), value: dbStats!.works },
              { label: t("datamgmt.stats_assets"), value: dbStats!.assets },
              { label: t("datamgmt.stats_creators"), value: dbStats!.creators },
              { label: t("datamgmt.stats_subs"), value: dbStats!.subscriptions },
              { label: t("datamgmt.stats_tags"), value: dbStats!.tags },
              { label: t("datamgmt.stats_downloads"), value: formatSize(info!.downloads_size_mb) },
              { label: t("datamgmt.stats_library"), value: formatSize(info!.library_size_mb) },
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
        )}
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
          {overviewLoading ? (
            <div aria-label={t("datamgmt.data_center_loading")} className="h-48 animate-pulse rounded-md bg-subtle" />
          ) : sourceStorageData.length ? (
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
          {overviewLoading ? (
            <div aria-label={t("datamgmt.data_center_loading")} className="h-48 animate-pulse rounded-md bg-subtle" />
          ) : storageGroups.length ? (
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

      {/* ═══ Integrity Check ═══ */}
      <div className="card p-4 mb-6">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-medium text-sm">{t("datamgmt.integrity_title")}</h3>
          <button
            aria-label={t("datamgmt.integrity_run")}
            onClick={() => integrity.start(undefined)}
            disabled={!integrity.canStart}
            className="btn-primary px-3 py-1.5 text-xs"
          >
            {integrity.isStarting || integrity.isActive ? t("datamgmt.integrity_running") : t("datamgmt.integrity_run")}
          </button>
        </div>
        <AdminOperationStatus controller={integrity} />

        {integrity.result ? (
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

            {integrityDbStats && (
              <div className="mt-4 pt-3 border-t">
                <h4 className="text-xs font-medium text-muted mb-2">{t("datamgmt.integrity_db_stats")}</h4>
                <div className="grid grid-cols-3 md:grid-cols-5 gap-2">
                  {Object.entries(integrityDbStats).map(([tbl, count]) => (
                    <div key={tbl} className="text-center bg-subtle rounded p-2">
                      <div className="text-sm font-mono font-bold">{count}</div>
                      <div className="text-[10px] text-muted">{tbl}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {integrity.result.checked_at && (
              <p className="text-xs text-muted mt-3">
                {t("datamgmt.integrity_checked_at")}: {fmt.dateTime(integrity.result.checked_at)}
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

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* ═══ Cleanup Tools ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.cleanup_title")}</h3>
          <div className="space-y-3">
            <div className="p-3 border rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t("datamgmt.cleanup_json")}</p>
                  <p className="text-xs text-muted">{t("datamgmt.cleanup_json_desc")}</p>
                </div>
                <button onClick={() => cleanupJSON.start(undefined)} disabled={!cleanupJSON.canStart}
                  className="btn-ghost ml-3 min-h-11 shrink-0 text-xs text-warning">
                  {cleanupJSON.isStarting || cleanupJSON.isActive ? "..." : t("datamgmt.cleanup_json_btn")}
                </button>
              </div>
              <AdminOperationStatus controller={cleanupJSON} />
              {cleanupJSON.result ? (
                <p className={`mt-2 text-xs ${cleanupJSON.result.failed > 0 ? "text-warning" : "text-success"}`}>
                  {t("datamgmt.cleanup_json_result", {
                    removed: cleanupJSON.result.removed,
                    skipped: cleanupJSON.result.skipped,
                    failed: cleanupJSON.result.failed,
                  })}
                </p>
              ) : null}
            </div>
            <div className="p-3 border rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t("datamgmt.cleanup_reindex")}</p>
                  <p className="text-xs text-muted">{t("datamgmt.cleanup_reindex_desc")}</p>
                </div>
                <button onClick={() => {
                  rebuildLibrary.resetStart();
                  setConfirmRebuild(true);
                }} disabled={!rebuildLibrary.canStart}
                  className="btn-primary ml-3 shrink-0 text-xs">
                  {rebuildLibrary.isStarting || rebuildLibrary.isActive ? "..." : t("datamgmt.cleanup_reindex_btn")}
                </button>
              </div>
              <AdminOperationStatus controller={rebuildLibrary} />
            </div>
            <div className="p-3 border rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t("datamgmt.disk_import")}</p>
                  <p className="text-xs text-muted">{t("datamgmt.disk_import_desc")}</p>
                  <label className="mt-1.5 flex items-center gap-1.5 text-xs text-muted cursor-pointer">
                    <input type="checkbox" checked={resetLedger} onChange={(e) => setResetLedger(e.target.checked)}
                      className="h-3.5 w-3.5 rounded border-border" />
                    {t("datamgmt.disk_import_reset_ledger")}
                  </label>
                  {breakdown?.pipeline_stats ? (
                    <div className="mt-2 rounded border border-border bg-subtle px-2 py-1.5 text-xs text-muted">
                      <span className="font-medium text-fg">{t("datamgmt.import_backlog")}</span>
                      <span className="ml-2">{t("datamgmt.pending_import_works")}: {breakdown.pipeline_stats.pending_import_works}</span>
                      <span className="ml-2">{t("datamgmt.orphan_pending_artifacts")}: {breakdown.pipeline_stats.orphan_pending_artifacts}</span>
                      <span className="ml-2">{t("datamgmt.failed_artifacts")}: {breakdown.pipeline_stats.failed_artifacts}</span>
                    </div>
                  ) : null}
                </div>
                <button onClick={() => importFromDisk.start(resetLedger ? { reset_ledger: true } : {})} disabled={!importFromDisk.canStart}
                  className="btn-primary shrink-0 ml-3 text-xs">
                  {importFromDisk.isStarting || importFromDisk.isActive ? "..." : t("datamgmt.disk_import_btn")}
                </button>
              </div>
              <AdminOperationStatus controller={importFromDisk} />
            </div>
            <div className="p-3 border rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t("datamgmt.reenrich")}</p>
                  <p className="text-xs text-muted">{t("datamgmt.reenrich_desc")}</p>
                </div>
                <button onClick={() => reenrichCreators.start(undefined)} disabled={!reenrichCreators.canStart}
                  className="btn-primary shrink-0 ml-3 text-xs">
                  {reenrichCreators.isStarting || reenrichCreators.isActive ? "..." : t("datamgmt.reenrich_btn")}
                </button>
              </div>
              <AdminOperationStatus controller={reenrichCreators} />
            </div>
          </div>
        </div>

        {/* ═══ Backup & Database ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.backup_section")}</h3>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-subtle rounded p-3 text-center">
                <div className="text-xs text-muted">{t("datamgmt.backup_recent")}</div>
                <div className="text-sm font-medium mt-0.5">
                  {lastBackup
                    ? fmt.dateTime(lastBackup.created_at)
                    : t("datamgmt.backup_none")}
                </div>
              </div>
              <div className="bg-subtle rounded p-3 text-center">
                <div className="text-xs text-muted">{t("datamgmt.backup_count")}</div>
                <div className="text-sm font-medium mt-0.5">{backups.data?.backups?.length ?? 0}</div>
              </div>
            </div>
            <button onClick={() => backupOperation.start(undefined)} disabled={!backupOperation.canStart}
              className="btn-primary w-full">
              {backupOperation.isStarting || backupOperation.isActive ? t("datamgmt.backup_creating") : t("datamgmt.backup_create")}
            </button>
            <AdminOperationStatus controller={backupOperation} />
            {backupOperation.result?.filename ? (
              <p className="text-xs text-success">{backupOperation.result.filename}</p>
            ) : null}

            <div className="pt-3 border-t">
              <h4 className="text-xs font-medium text-muted mb-2">{t("datamgmt.db_stats_title")}</h4>
              <div className="grid grid-cols-2 gap-2 text-xs">
                {info && (
                  <>
                    <div className="bg-subtle rounded p-2 flex justify-between">
                      <span className="text-muted">{t("datamgmt.db_stats_title")}</span>
                      <span>{((info.downloads_size_mb || 0) + (info.library_size_mb || 0)).toFixed(1)} MB</span>
                    </div>
                    <div className="bg-subtle rounded p-2 flex justify-between">
                      <span>Meilisearch</span>
                      <span>-</span>
                    </div>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* ═══ Danger Zone ═══ */}
      <div className="card border-danger p-4 dark:border-danger">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-danger text-lg">&#9888;</span>
          <h3 className="font-medium text-sm text-danger">{t("datamgmt.danger_title")}</h3>
        </div>
        <p className="text-xs text-danger mb-4">{t("datamgmt.danger_warning")}</p>

        <div className="space-y-2">
          {dangerActions.map((a) => {
            const isPending = clearOperation.isStarting || clearOperation.isActive;
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
                  onClick={() => {
                    clearOperation.resetStart();
                    setConfirmAction(a.key);
                  }}
                  disabled={isPending}
                  className={`min-h-11 w-full shrink-0 rounded-md px-4 py-1.5 text-xs font-semibold transition-opacity disabled:opacity-30 sm:ml-3 sm:w-auto ${
                    a.color === "red" ? "border border-danger/40 bg-danger-subtle text-danger hover:bg-danger/20" :
                    a.color === "orange" ? "bg-warning text-canvas hover:bg-warning/90" :
                    "bg-accent text-white hover:bg-accent/90"
                  }`}
                >
                  {isPending ? "..." : a.title}
                </button>
              </div>
            );
          })}
        </div>
        <AdminOperationStatus controller={clearOperation} />
      </div>

      {/* Global confirm dialog */}
      <ConfirmDialog
        open={confirmRebuild}
        title={t("datamgmt.cleanup_reindex_confirm_title")}
        message={t("datamgmt.cleanup_reindex_confirm_msg")}
        onConfirm={() => rebuildLibrary.start(undefined)}
        onCancel={() => {
          setConfirmRebuild(false);
          rebuildLibrary.resetStart();
        }}
        isPending={rebuildLibrary.isStarting}
        error={rebuildLibrary.startError?.message}
      />
      {confirmAction && (
        <ConfirmDialog
          open
          title={t("datamgmt.confirm_title").replace("{action}", dangerActions.find((a) => a.key === confirmAction)?.title || confirmAction)}
          message={`${t("datamgmt.confirm_msg").replace("{action}", confirmAction)}${clearPreview.data ? ` ${Object.entries(clearPreview.data.counts).map(([name, count]) => `${name}: ${count}`).join(" · ")}` : ""}`}
          confirmationPhrase={clearPreview.data?.confirmation_phrase}
          onConfirm={() => {
            const action = dangerActions.find((a) => a.key === confirmAction);
            if (action && clearPreview.data) {
              clearOperation.start({
                entity: action.key,
                confirmation: clearPreview.data.confirmation_phrase,
                title: action.title,
              });
            }
          }}
          onCancel={() => {
            setConfirmAction(null);
            clearOperation.resetStart();
          }}
          isPending={clearOperation.isStarting}
          error={clearOperation.startError?.message || (clearPreview.error as Error | null)?.message}
          confirmDisabled={clearPreview.isLoading || clearPreview.isError || !clearPreview.data}
        />
      )}
    </PageShell>
  );
}

export default function DataManagementPage() {
  return (
    <PermissionGuard module="system">
      <DataManagementContent />
    </PermissionGuard>
  );
}
