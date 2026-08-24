"use client";
import { useCallback, useState, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import type { AdminOperationAccepted, RestoreReceipt, RestoreUploadSession, RestoreValidationResult } from "@/lib/api/types";
import { useT } from "@/lib/i18n";
import { sha256Blob } from "@/lib/sha256";
import { useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageShell, ConfirmDialog, EmptyState, ErrorState, RowActionMenu } from "@/components";
import { useToast } from "@/components/Toast";
import { useI18nFormat } from "@/lib/i18n-format";
import { Archive, Database, FileJson, FileText, Settings } from "lucide-react";
import { AdminOperationStatus } from "@/components/AdminOperationStatus";
import { useAdminOperation } from "@/lib/useAdminOperation";

const ALL_CONTENTS = ["database", "gallerydl-config", "app-config", "download-archives", "library-metadata"] as const;
type BackupEstimateResult = { components: Record<string, number>; message?: string };
type BackupCreateResult = {
  status: string;
  filename: string;
  size_bytes: number;
  size_mb: number;
  contents: string[];
  component_sizes: Record<string, number>;
  message?: string;
};
type RestoreFlow = {
  session: RestoreUploadSession;
  token: string;
  accepted: AdminOperationAccepted;
};

const RESTORE_CHUNK_SIZE = 1024 * 1024;
const RESTORE_STORAGE_KEY = "auto-gallery-restore-upload-v1";

const CONTENT_ICONS = {
  database: Database,
  "gallerydl-config": Settings,
  "app-config": FileJson,
  "download-archives": Archive,
  "library-metadata": FileText,
};

function fmtKB(kb: number): string {
  if (kb >= 1024) return `${(kb / 1024).toFixed(1)} MB`;
  return `${kb.toFixed(0)} KB`;
}

function contentBadgeColor(content: string): string {
  const colors: Record<string, string> = {
    database: "bg-accent-subtle text-accent",
    "gallerydl-config": "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
    "app-config": "bg-success-subtle text-success",
    "download-archives": "bg-warning-subtle text-warning",
    "library-metadata": "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
  };
  return colors[content] || "bg-subtle text-fg";
}

function loadSavedRestoreFlow(): RestoreFlow | null {
  if (typeof window === "undefined") return null;
  try {
    const value = JSON.parse(localStorage.getItem(RESTORE_STORAGE_KEY) || "null");
    return value?.session?.upload_id && value?.token && value?.accepted?.task_id
      ? value as RestoreFlow
      : null;
  } catch {
    return null;
  }
}

function RestoreValidationFlow({ flow }: { flow: RestoreFlow }) {
  const t = useT();
  const validation = useAdminOperation<RestoreValidationResult>({
    operationType: "admin-restore-validate",
    scope: flow.session.upload_id,
    initialAccepted: flow.accepted,
    startOperation: () => api.startRestoreValidation(flow.session.upload_id, flow.token),
    loadLatest: () => api.getLatestRestoreValidation(flow.session.upload_id, flow.token),
  });
  const requestId = validation.result?.request_id ?? null;
  const receipt = useQuery<RestoreReceipt>({
    queryKey: ["restore-receipt", requestId],
    queryFn: () => api.getRestoreReceipt(requestId!, flow.token),
    enabled: requestId !== null,
    refetchInterval: (query) => query.state.data?.status === "pending" ? 1_000 : false,
    refetchOnWindowFocus: false,
  });
  const rollbackComplete = receipt.data?.status === "rolled_back"
    && receipt.data.rollback_status === "complete";

  return (
    <div className="mt-4 space-y-3">
      <AdminOperationStatus controller={validation} />
      {validation.result ? (
        <div className="rounded-md border border-warning/40 bg-warning-subtle p-4 text-sm">
          <h4 className="font-medium text-fg">{t("backup.restore_ready")}</h4>
          <p className="mt-1 text-xs text-muted">{t("backup.restore_handoff_desc")}</p>
          <p className="mt-3 text-xs text-muted">{t("backup.restore_request_id")}</p>
          <code className="block break-all rounded bg-surface p-2 text-xs">{validation.result.request_id}</code>
          <p className="mt-3 text-xs text-muted">{t("backup.restore_host_command")}</p>
          <code className="block overflow-x-auto rounded bg-surface p-2 text-xs">{validation.result.host_command}</code>
        </div>
      ) : null}
      {requestId ? (
        <div className="rounded-md border border-border bg-surface p-4 text-sm" aria-live="polite">
          <h4 className="font-medium text-fg">{t("backup.restore_receipt")}</h4>
          {receipt.isLoading || receipt.data?.status === "pending" ? (
            <p role="status" className="mt-1 text-xs text-muted">{t("backup.restore_receipt_pending")}</p>
          ) : null}
          {receipt.error ? <p role="alert" className="mt-1 text-xs text-danger">{(receipt.error as Error).message}</p> : null}
          {receipt.data?.status === "success" ? (
            <p className="mt-1 text-xs text-success">{t("backup.restore_receipt_success")}</p>
          ) : null}
          {receipt.data?.status === "rolled_back" ? (
            <div role="alert" className="mt-2 rounded border border-danger/30 bg-danger-subtle p-3 text-danger">
              <p className="font-medium">{rollbackComplete ? t("backup.restore_rollback_complete") : t("backup.restore_rollback_failed")}</p>
              {receipt.data.error ? <p className="mt-1 text-xs">{receipt.data.error}</p> : null}
              {receipt.data.diagnostic ? <p className="mt-1 text-xs">{receipt.data.diagnostic}</p> : null}
              <code className="mt-2 block text-[11px]">{receipt.data.phase}</code>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function BackupPage() {
  const toast = useToast();
  const t = useT();
  const fmt = useI18nFormat();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set(ALL_CONTENTS));
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [isRestoring, setIsRestoring] = useState(false);
  const [restoreProgress, setRestoreProgress] = useState<{ current: number; total: number } | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [restoreFlow, setRestoreFlow] = useState<RestoreFlow | null>(loadSavedRestoreFlow);

  const backups = useQuery({ queryKey: queryKeys.backups.list, queryFn: api.listBackups });
  const handleBackupCompleted = useCallback(() => {
    void qc.invalidateQueries({ queryKey: queryKeys.backups.list });
  }, [qc]);
  const estimate = useAdminOperation<BackupEstimateResult>({
    operationType: "admin-backup-estimate",
    scope: "global",
    startOperation: () => api.startBackupEstimate(),
    loadLatest: () => api.getLatestBackupEstimate(),
  });
  const createOperation = useAdminOperation<BackupCreateResult, string[]>({
    operationType: "admin-backup-create",
    scope: "global",
    startOperation: (contents) => api.createBackup(contents),
    loadLatest: () => api.getLatestBackup(),
    onCompleted: handleBackupCompleted,
  });
  const backupItems = backups.data?.backups || [];
  const backupEntrance = useStaggeredEntrance(backupItems.map((backup) => backup.filename));

  const toggle = (c: string) => {
    const next = new Set(selected);
    next.has(c) ? next.delete(c) : next.add(c);
    setSelected(next);
  };
  const toggleAll = () => setSelected(selected.size === ALL_CONTENTS.length ? new Set() : new Set(ALL_CONTENTS));
  const selectedArr = [...selected];
  const estTotal = estimate.result?.components
    ? selectedArr.reduce((sum, c) => sum + (estimate.result!.components[c] || 0), 0) : 0;

  const handleRestoreClick = () => fileRef.current?.click();

  const handleFileSelected = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setRestoreFile(file);
    setConfirmRestore(true);
    if (fileRef.current) fileRef.current.value = "";
  };

  const doRestore = async () => {
    if (!restoreFile) return;
    setIsRestoring(true);
    setConfirmRestore(false);
    setRestoreError(null);
    try {
      const archiveHash = await sha256Blob(restoreFile);
      const saved = (() => {
        try { return JSON.parse(localStorage.getItem(RESTORE_STORAGE_KEY) || "null"); }
        catch { return null; }
      })();
      let token: string;
      let session: RestoreUploadSession;
      if (
        saved?.session?.filename === restoreFile.name
        && saved?.session?.size_bytes === restoreFile.size
        && saved?.session?.sha256 === archiveHash
        && saved?.token
      ) {
        token = saved.token;
        session = await api.getRestoreUpload(saved.session.upload_id, token);
      } else {
        const totalChunks = Math.ceil(restoreFile.size / RESTORE_CHUNK_SIZE);
        const created = await api.createRestoreUpload({
          filename: restoreFile.name,
          size_bytes: restoreFile.size,
          sha256: archiveHash,
          chunk_size: RESTORE_CHUNK_SIZE,
          total_chunks: totalChunks,
        });
        token = created.upload_token;
        session = created;
      }
      localStorage.setItem(RESTORE_STORAGE_KEY, JSON.stringify({ session, token }));
      for (let index = session.next_chunk; index < session.total_chunks; index += 1) {
        const start = index * session.chunk_size;
        const chunk = restoreFile.slice(start, Math.min(start + session.chunk_size, restoreFile.size));
        const chunkHash = await sha256Blob(chunk);
        const updated = await api.uploadRestoreChunk(
          session.upload_id,
          token,
          index,
          chunk,
          chunkHash,
        );
        session = { ...session, ...updated };
        setRestoreProgress({ current: session.received_chunks, total: session.total_chunks });
        localStorage.setItem(RESTORE_STORAGE_KEY, JSON.stringify({ session, token }));
      }
      const accepted = await api.startRestoreValidation(session.upload_id, token);
      const flow = { session, token, accepted };
      localStorage.setItem(RESTORE_STORAGE_KEY, JSON.stringify(flow));
      setRestoreFlow(flow);
    } catch (e) {
      setRestoreError((e as Error).message);
    } finally {
      setIsRestoring(false);
      setRestoreFile(null);
    }
  };

  const handleDelete = async (filename: string) => {
    try { await api.deleteBackup(filename); qc.invalidateQueries({ queryKey: ["backups"] }); }
    catch (e) { toast.error({ message: (e as Error).message }); }
    setDeleteTarget(null);
  };

  const doDownload = (filename: string) => {
    const a = document.createElement("a");
    a.href = api.downloadBackup(filename);
    a.download = filename;
    a.click();
  };

  return (
    <PageShell>
      <PageHeader title={t("backup.title")} description={t("backup.desc")} />

      {/* Create Backup */}
      <div className="card p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium dark:text-white">{t("backup.create")}</h3>
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted">
              {t("backup.estimated_size")}: <span className="font-mono font-medium">{fmtKB(estTotal)}</span>
            </span>
            <button
              type="button"
              onClick={() => estimate.start(undefined)}
              disabled={!estimate.canStart}
              className="btn-ghost"
            >
              {estimate.isStarting ? t("admin_operation.starting") : t("backup.refresh_estimate")}
            </button>
            <button onClick={() => createOperation.start(selectedArr)} disabled={!createOperation.canStart || selected.size === 0}
              className="btn-primary">
              {createOperation.isStarting || createOperation.isActive ? t("backup.creating") : t("backup.create")}
            </button>
          </div>
        </div>

        <AdminOperationStatus controller={estimate} />
        <AdminOperationStatus controller={createOperation} />
        {createOperation.result?.filename ? (
          <p className="mt-2 text-xs text-success">{createOperation.result.filename}</p>
        ) : null}

        <label className="flex items-center gap-2 mb-3 text-xs text-muted cursor-pointer">
          <input type="checkbox" aria-label={t("backup.select_all")} checked={selected.size === ALL_CONTENTS.length} onChange={toggleAll} className="rounded" />
          {t("backup.select_all")}
        </label>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {ALL_CONTENTS.map((c) => {
            const size = estimate.result?.components?.[c];
            const checked = selected.has(c);
            const keyMap: Record<string, string> = {
              database: "db", "gallerydl-config": "config", "app-config": "appconfig",
              "download-archives": "archives", "library-metadata": "library",
            };
            const sk = keyMap[c] || c;
            const ContentIcon = CONTENT_ICONS[c];
            return (
              <label key={c} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                checked ? "border-border bg-subtle"
                  : "border-border hover:border-border dark:hover:border-border"}`}>
                <input type="checkbox" aria-label={t(`backup.item_${sk}`)} checked={checked} onChange={() => toggle(c)} className="mt-0.5 rounded" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <ContentIcon className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
                    <span className="text-sm font-medium dark:text-white">{t(`backup.item_${sk}`)}</span>
                    {size !== undefined && <span className="text-xs text-muted ml-auto">{fmtKB(size)}</span>}
                  </div>
                  <p className="text-xs text-muted mt-0.5 ml-6">{t(`backup.item_${sk}_desc`)}</p>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      {/* Existing Backups */}
      <div className="card p-6 mb-6">
        <h3 className="font-medium dark:text-white mb-4">{t("backup.list_title")}</h3>
        {backups.isLoading && <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 rounded-md bg-subtle dark:bg-subtle" />)}</div>}
        {backups.error && <ErrorState message={(backups.error as Error).message} onRetry={() => backups.refetch()} />}
        {backups.data?.backups && backups.data.backups.length === 0 && <EmptyState title={t("backup.no_backups")} description={t("backup.no_backups_desc")} />}
        {backups.data?.backups && backups.data.backups.length > 0 && (
          <div className="space-y-2">
            {backups.data.backups.map((b, index) => {
              const entrance = backupEntrance(b.filename, index);
              return (
              <div key={b.filename} className={`${entrance.className} flex items-start justify-between p-3 bg-subtle rounded-lg text-sm`} style={entrance.style}>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-medium dark:text-white font-mono text-xs">{b.filename}</span>
                    {b.version && <span className="text-[10px] text-muted">{t("backup.manifest_version")} {b.version}</span>}
                  </div>
                  <div className="text-xs text-muted mt-1">{b.size_mb} MB &middot; {fmt.dateTime(b.created_at)}</div>
                  {b.contents && b.contents.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {b.contents.map((c: string) => (
                        <span key={c} className={`text-[10px] px-1.5 py-0.5 rounded-full ${contentBadgeColor(c)}`}>
                          {c}{b.component_sizes?.[c] !== undefined ? ` ${fmtKB(b.component_sizes![c])}` : ""}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 ml-3 shrink-0">
                  <button onClick={() => doDownload(b.filename)} className="btn-ghost px-2.5 py-1 text-xs">{t("backup.download")}</button>
                  <RowActionMenu
                    label={t("common.more_actions")}
                    items={[{
                      label: t("common.delete"),
                      tone: "danger",
                      onSelect: () => setDeleteTarget(b.filename),
                    }]}
                  />
                </div>
              </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Restore */}
      <div className="card p-6" data-restore-flow>
        <h3 className="font-medium dark:text-white mb-1">{t("backup.restore_title")}</h3>
        <p className="text-xs text-muted mb-4">{t("backup.restore_desc")}</p>
        <input ref={fileRef} type="file" accept=".tar.gz" className="hidden" onChange={handleFileSelected} />
        <button onClick={handleRestoreClick} disabled={isRestoring}
          className="btn-danger">
          {isRestoring ? t("backup.restore_staging") : t("backup.restore_btn")}
        </button>
        {restoreProgress ? (
          <p role="status" className="mt-3 text-xs text-muted">
            {t("backup.restore_upload_progress", {
              current: restoreProgress.current,
              total: restoreProgress.total,
            })}
          </p>
        ) : null}
        {restoreError ? <p role="alert" className="mt-3 text-xs text-danger">{restoreError}</p> : null}
        {restoreFlow ? <RestoreValidationFlow key={restoreFlow.session.upload_id} flow={restoreFlow} /> : null}
      </div>

      {confirmRestore && restoreFile && (
        <ConfirmDialog open title={t("backup.restore_title")}
          message={t("backup.restore_confirm")}
          onConfirm={doRestore}
          onCancel={() => { setConfirmRestore(false); setRestoreFile(null); }}
          isPending={isRestoring} />
      )}

      {deleteTarget && (
        <ConfirmDialog open title={t("backup.delete_confirm")}
          message={`${t("backup.delete_confirm")}\n\n${deleteTarget}`}
          onConfirm={() => handleDelete(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
          isPending={false} />
      )}
    </PageShell>
  );
}
