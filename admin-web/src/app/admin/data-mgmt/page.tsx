"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ConfirmDialog, PageShell, StatCard, StatusBadge } from "@/components";
import { useNotifications } from "@/components/NotificationCenter";
import { useT } from "@/lib/i18n";
import { useRouter } from "next/navigation";
import { getSourceColor } from "@/lib/sourceColors";

type Severity = "error" | "warning" | "info";

function formatSize(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return `${(mb * 1024).toFixed(0)} KB`;
}

function severityBadge(s: string, t: (k: string) => string) {
  const sev: Severity = (["error","warning","info"].includes(s) ? s : "info") as Severity;
  const map: Record<Severity, string> = {
    error: "bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400 border-red-200 dark:border-red-800",
    warning: "bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400 border-yellow-200 dark:border-yellow-800",
    info: "bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-800",
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
  const router = useRouter();
  const qc = useQueryClient();
  const notify = useNotifications();
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [confirmAction, setConfirmAction] = useState<string | null>(null);
  const [activeAction, setActiveAction] = useState<string | null>(null);
  const [integrityItems, setIntegrityItems] = useState<{ type: string; description: string; count: number; items: any[] } | null>(null);
  const [dangerConfirm, setDangerConfirm] = useState("");

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
  const creatorCount = useQuery({ queryKey: queryKeys.creators.count, queryFn: () => api.countCreators() });
  const subCount = useQuery({ queryKey: queryKeys.subscriptions.count, queryFn: () => api.countSubscriptions() });
  const integrity = useQuery({ queryKey: ["integrity-check"], queryFn: () => api.getIntegrityCheck(), enabled: false });
  const backups = useQuery({ queryKey: ["backups"], queryFn: () => api.listBackups() });

  // ── Mutations ──
  const cleanupJSON = useMutation({
    mutationFn: () => api.cleanupMetadataJSONs(),
    onSuccess: (d: any) => setResult({ ok: true, msg: t("datamgmt.cleanup_json_done").replace("{count}", String(d.removed)) }),
    onError: (e) => setResult({ ok: false, msg: (e as Error).message }),
  });

  const createBackupMut = useMutation({
    mutationFn: () => api.createBackup(),
    onSuccess: (d: any) => { setResult({ ok: true, msg: "Backup: " + d.filename + " (" + d.size_mb + " MB)" }); backups.refetch(); },
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
      setResult({ ok: true, msg: `${vars.title} queued` });
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
  const lastBackup = backups.data?.backups?.[0];
  const totalSourceSize = breakdown?.sources
    ? Object.values(breakdown.sources).reduce((sum, s) => sum + s.size_mb, 0)
    : 0;
  const layerEntries = breakdown?.layers ? Object.entries(breakdown.layers) : [];

  return (
    <PageShell size="normal">
      <PageHeader title={t("datamgmt.title")} description={t("datamgmt.desc")} />

      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm flex items-center justify-between ${result.ok ? "bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 text-green-700 dark:text-green-400" : "bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400"}`}>
          <span>{result.msg}</span>
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">{t("datamgmt.dismiss")}</button>
        </div>
      )}

      {clearOperation && (
        <div className={`mb-4 rounded-md border p-3 text-sm ${
          clearOperation.status === "error"
            ? "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-400"
            : clearOperation.status === "completed"
              ? "border-green-200 bg-green-50 text-green-700 dark:border-green-800 dark:bg-green-900/30 dark:text-green-400"
              : "border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-400"
        }`}>
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">{clearOperation.title}</span>
            <StatusBadge status={clearOperation.status} className="uppercase" />
          </div>
          <div className="mt-1 text-xs opacity-80">
            {clearOperation.error || clearOperation.result?.message || clearOperation.progress?.label || "Queued in the background"}
          </div>
        </div>
      )}

      {/* ═══ Stats Cards ═══ */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 mb-6">
        {[
          { label: t("datamgmt.stats_works"), value: dbStats?.works ?? "-", color: "blue" },
          { label: t("datamgmt.stats_assets"), value: dbStats?.assets ?? "-", color: "indigo" },
          { label: t("datamgmt.stats_creators"), value: creatorCount.data?.count ?? "-", color: "purple" },
          { label: t("datamgmt.stats_subs"), value: subCount.data?.count ?? "-", color: "green" },
          { label: t("datamgmt.stats_tags"), value: dbStats?.tags ?? "-", color: "amber" },
          { label: t("datamgmt.stats_downloads"), value: info ? formatSize(info.downloads_size_mb) : "-", color: "pink" },
          { label: t("datamgmt.stats_library"), value: info ? formatSize(info.library_size_mb) : "-", color: "teal" },
        ].map((s) => (
          <StatCard key={s.label} label={s.label} value={s.value} tone={s.color === "green" ? "success" : s.color === "amber" ? "warning" : "active"} className="p-3 text-center [&>div:first-child]:text-xl" />
        ))}
      </div>

      {layerEntries.length > 0 && (
        <div className="card mb-6 p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.storage_layers_title")}</h3>
          <div className="grid gap-3 md:grid-cols-4">
            {layerEntries.map(([key, layer]) => (
              <div key={key} className="rounded-md border border-border bg-subtle p-3 dark:border-border dark:bg-surface">
                <div className="text-xs text-muted">{t(`datamgmt.layer_${key}`)}</div>
                <div className="mt-1 text-lg font-semibold dark:text-white">{formatSize(layer.size_mb)}</div>
                <div className="mt-1 truncate font-mono text-[10px] text-muted" title={layer.path}>{layer.path}</div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-muted">
            DOWNLOAD_ROOT is the long-term original media store. LIBRARY_ROOT keeps metadata and thumbnails for indexing and browsing.
          </p>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* ═══ Storage Distribution ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.storage_title")}</h3>
          {breakdown?.sources && Object.keys(breakdown.sources).length > 0 ? (
            <div className="space-y-2">
              {Object.entries(breakdown.sources)
                .sort(([, a], [, b]) => b.size_mb - a.size_mb)
                .map(([source, s]) => {
                  const pct = totalSourceSize > 0 ? (s.size_mb / totalSourceSize) * 100 : 0;
                  const color = getSourceColor(source);
                  return (
                    <div key={source}>
                      <div className="flex items-center justify-between text-xs mb-0.5">
                        <span className="font-medium capitalize">{source}</span>
                        <span className="text-muted">
                          {formatSize(s.size_mb)} · {s.work_count} {t("datamgmt.storage_works_label")}
                        </span>
                      </div>
                      <div className="w-full h-3 bg-subtle rounded-full overflow-hidden">
                        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${Math.max(pct, 2)}%`, backgroundColor: color }} />
                      </div>
                    </div>
                  );
                })}
              <div className="pt-2 border-t text-xs text-muted flex justify-between">
                <span>Total</span>
                <span className="font-medium">{formatSize(totalSourceSize)}</span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted text-center py-6">{t("datamgmt.storage_no_data")}</p>
          )}
        </div>

        {/* ═══ Creator Storage Top ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.storage_creators_title")}</h3>
          {breakdown?.creators && breakdown.creators.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-muted border-b">
                    <th className="text-left py-1.5 font-medium">#</th>
                    <th className="text-left py-1.5 font-medium">{t("datamgmt.storage_creator_col")}</th>
                    <th className="text-left py-1.5 font-medium">{t("datamgmt.storage_source_label")}</th>
                    <th className="text-right py-1.5 font-medium">{t("datamgmt.storage_works_label")}</th>
                    <th className="text-right py-1.5 font-medium">Size</th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.creators.map((c, i) => (
                    <tr key={`${c.source}/${c.name}`} className="border-b border-border/50 hover:bg-subtle dark:hover:bg-subtle cursor-pointer"
                        onClick={() => c.creator_id && router.push(`/admin/creators/${c.creator_id}`)}
                        title={c.creator_id ? "View creator detail" : ""}>
                      <td className="py-1.5 text-muted">{i + 1}</td>
                      <td className="py-1.5 font-medium truncate max-w-[120px]" title={c.display_name || c.name}>
                        <span className={c.creator_id ? "text-blue-600 dark:text-blue-400 hover:underline" : ""}>{c.display_name || c.name}</span>
                      </td>
                      <td className="py-1.5 capitalize">
                        <span className="inline-block w-2 h-2 rounded-full mr-1" style={{ backgroundColor: getSourceColor(c.source) }} />
                        {c.source}
                      </td>
                      <td className="py-1.5 text-right">{c.work_count}</td>
                      <td className="py-1.5 text-right font-mono text-fg">{formatSize(c.size_mb)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-muted text-center py-6">{t("datamgmt.storage_no_data")}</p>
          )}
        </div>
      </div>

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
              <div className="text-center py-6 text-green-600 dark:text-green-400">
                <div className="text-lg mb-1">&#10003;</div>
                <p className="text-sm font-medium">{t("datamgmt.integrity_clean")}</p>
                <p className="text-xs text-muted mt-1">{t("datamgmt.integrity_clean_desc")}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {issues.map((issue) => (
                  <div key={issue.type} className={`border rounded-lg p-3 flex items-center justify-between ${
                    issue.severity === "error" ? "border-red-200 dark:border-red-800 bg-red-50/50 dark:bg-red-900/10" :
                    issue.severity === "warning" ? "border-yellow-200 dark:border-yellow-800 bg-yellow-50/50 dark:bg-yellow-900/10" :
                    "border-blue-200 dark:border-blue-800 bg-blue-50/50 dark:bg-blue-900/10"
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
                        className="shrink-0 text-xs text-blue-600 hover:underline"
                      >
                        {t("datamgmt.integrity_view_items")} ({Math.min(issue.items.length, 50)})
                      </button>
                    )}
                  </div>
                ))}
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
                {t("datamgmt.integrity_checked_at")}: {new Date(integrity.data.checked_at).toLocaleString()}
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
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setIntegrityItems(null)}>
            <div className="card-elevated p-4 max-w-xl w-full mx-4 max-h-[70vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
              <div className="flex items-center justify-between mb-3">
                <h3 className="font-medium text-sm">
                  {t("datamgmt.integrity_items_modal_title")
                    .replace("{type}", integrityItems.description)
                    .replace("{count}", String(integrityItems.count))}
                </h3>
                <button onClick={() => setIntegrityItems(null)} className="text-muted hover:text-muted text-lg">&times;</button>
              </div>
              <div className="space-y-1 max-h-96 overflow-auto">
                {integrityItems.items.map((item, i) => (
                  <div key={i} className="text-xs p-2 bg-subtle rounded flex items-center justify-between">
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
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* ═══ Cleanup Tools ═══ */}
        <div className="card p-4">
          <h3 className="font-medium text-sm mb-3">{t("datamgmt.cleanup_title")}</h3>
          <div className="space-y-3">
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.cleanup_json")}</p>
                <p className="text-xs text-muted">{t("datamgmt.cleanup_json_desc")}</p>
              </div>
              <button onClick={() => cleanupJSON.mutate()} disabled={cleanupJSON.isPending}
                className="shrink-0 ml-3 px-3 py-1.5 text-xs bg-yellow-600 text-white rounded hover:bg-yellow-700 disabled:opacity-50">
                {cleanupJSON.isPending ? "..." : t("datamgmt.cleanup_json_btn")}
              </button>
            </div>
            <div className="flex items-center justify-between p-3 border rounded-lg">
              <div>
                <p className="text-sm font-medium">{t("datamgmt.cleanup_reindex")}</p>
                <p className="text-xs text-muted">{t("datamgmt.cleanup_reindex_desc")}</p>
              </div>
              <button className="shrink-0 ml-3 px-3 py-1.5 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700">
                {t("datamgmt.cleanup_reindex_btn")}
              </button>
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
                    ? new Date(lastBackup.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
                    : t("datamgmt.backup_none")}
                </div>
              </div>
              <div className="bg-subtle rounded p-3 text-center">
                <div className="text-xs text-muted">{t("datamgmt.backup_count")}</div>
                <div className="text-sm font-medium mt-0.5">{backups.data?.backups?.length ?? 0}</div>
              </div>
            </div>
            <button onClick={() => createBackupMut.mutate()} disabled={createBackupMut.isPending}
              className="btn-primary w-full">
              {createBackupMut.isPending ? t("datamgmt.backup_creating") : t("datamgmt.backup_create")}
            </button>

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
      <div className="card border-[#ff8182] p-4 dark:border-danger">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-red-500 text-lg">&#9888;</span>
          <h3 className="font-medium text-sm text-red-700 dark:text-red-400">{t("datamgmt.danger_title")}</h3>
        </div>
        <p className="text-xs text-red-500 dark:text-red-400 mb-4">{t("datamgmt.danger_warning")}</p>

        {/* Confirmation input */}
        <div className="mb-4">
          <input
            type="text"
            value={dangerConfirm}
            onChange={(e) => setDangerConfirm(e.target.value)}
            placeholder={t("datamgmt.danger_confirm_text")}
            className="input w-full max-w-sm border-[#ff8182] dark:border-danger"
          />
          <p className="text-xs text-muted mt-1">{t("datamgmt.danger_confirm_hint")}</p>
        </div>

        <div className="space-y-2">
          {dangerActions.map((a) => {
            const actionEntity = clearEntityForAction(a.key);
            const isCurrentOperation = clearOperation?.status === "running" && clearOperation.meta?.entity === actionEntity;
            const isPending = (clearOperationMutation.isPending && activeAction === a.key) || isCurrentOperation;
            return (
              <div key={a.key} className={`flex items-center justify-between p-3 rounded-lg border-l-4 ${
                a.color === "red" ? "border-l-red-500 bg-subtle" :
                a.color === "orange" ? "border-l-orange-500 bg-subtle" :
                "border-l-blue-500 bg-subtle"
              }`}>
                <div>
                  <p className="text-sm font-medium dark:text-white">{a.title}</p>
                  <p className="text-xs text-muted">{a.desc}</p>
                </div>
                <button
                  onClick={() => setConfirmAction(a.key)}
                  disabled={!dangerUnlocked || isPending}
                  className={`shrink-0 ml-3 px-4 py-1.5 text-xs text-white rounded disabled:opacity-30 transition-opacity ${
                    a.color === "red" ? "bg-red-600 hover:bg-red-700" :
                    a.color === "orange" ? "bg-orange-600 hover:bg-orange-700" :
                    "bg-blue-600 hover:bg-blue-700"
                  }`}
                >
                  {isPending ? "..." : a.title}
                </button>
              </div>
            );
          })}
        </div>
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
  );
}
