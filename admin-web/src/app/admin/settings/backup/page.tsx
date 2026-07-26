"use client";
import { useState, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import Link from "next/link";
import { PageHeader, PageShell, ConfirmDialog, EmptyState, ErrorState, RowActionMenu } from "@/components";
import { useToast } from "@/components/Toast";

const ALL_CONTENTS = ["database", "gallerydl-config", "app-config", "download-archives", "library-metadata"] as const;

const CONTENT_ICONS: Record<string, string> = {
  database: "🗄",
  "gallerydl-config": "⚙",
  "app-config": "📋",
  "download-archives": "📦",
  "library-metadata": "📄",
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

export default function BackupPage() {
  const toast = useToast();
  const t = useT();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set(ALL_CONTENTS));
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [restoreManifest, setRestoreManifest] = useState<any>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);

  const backups = useQuery({ queryKey: queryKeys.backups.list, queryFn: api.listBackups });
  const estimate = useQuery({ queryKey: queryKeys.backups.estimate, queryFn: () => api.estimateBackupSizes() });
  const backupItems = backups.data?.backups || [];
  const backupEntrance = useStaggeredEntrance(backupItems.map((backup) => backup.filename));

  const toggle = (c: string) => {
    const next = new Set(selected);
    next.has(c) ? next.delete(c) : next.add(c);
    setSelected(next);
  };
  const toggleAll = () => setSelected(selected.size === ALL_CONTENTS.length ? new Set() : new Set(ALL_CONTENTS));
  const selectedArr = [...selected];
  const estTotal = estimate.data?.components
    ? selectedArr.reduce((sum, c) => sum + (estimate.data!.components[c] || 0), 0) : 0;

  const handleCreate = async () => {
    setIsCreating(true);
    try {
      const data = await api.createBackup(selectedArr);
      toast.success({ message: t("backup.created").replace("{filename}", data.filename).replace("{size}", String(data.size_mb)) });
      qc.invalidateQueries({ queryKey: ["backups"] });
    } catch (e) { toast.error({ message: (e as Error).message }); }
    setIsCreating(false);
  };

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
    try {
      const data = await api.restoreBackup(restoreFile);
      if (data.status === "ok" || data.status === "partial") {
        setResult({ ok: data.status === "ok", msg: t("backup.restored").replace("{count}", String(data.restored.length)) + (data.errors.length ? ` (${data.errors.length} errors)` : "") });
        qc.invalidateQueries();
      } else {
        setResult({ ok: false, msg: data.errors?.join("; ") || "Restore failed" });
      }
    } catch (e) { setResult({ ok: false, msg: (e as Error).message }); }
    setIsRestoring(false);
    setRestoreFile(null);
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
    <PageShell size="normal">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">&larr; {t("common.back")}</Link>
      </div>
      <PageHeader title={t("backup.title")} description={t("backup.desc")} />

      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm flex items-center justify-between ${result.ok ? "bg-success-subtle border border-success/30 text-success" : "bg-danger-subtle border border-danger/30 text-danger"}`}>
          <span>{result.msg}</span>
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">{t("common.close")}</button>
        </div>
      )}

      {/* Create Backup */}
      <div className="card p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-medium dark:text-white">{t("backup.create")}</h3>
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted">
              {t("backup.estimated_size")}: <span className="font-mono font-medium">{fmtKB(estTotal)}</span>
            </span>
            <button onClick={handleCreate} disabled={isCreating || selected.size === 0}
              className="btn-primary">
              {isCreating ? t("backup.creating") : t("backup.create")}
            </button>
          </div>
        </div>

        <label className="flex items-center gap-2 mb-3 text-xs text-muted cursor-pointer">
          <input type="checkbox" aria-label="Select item" checked={selected.size === ALL_CONTENTS.length} onChange={toggleAll} className="rounded" />
          {t("backup.select_all")}
        </label>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {ALL_CONTENTS.map((c) => {
            const size = estimate.data?.components?.[c];
            const checked = selected.has(c);
            const keyMap: Record<string, string> = {
              database: "db", "gallerydl-config": "config", "app-config": "appconfig",
              "download-archives": "archives", "library-metadata": "library",
            };
            const sk = keyMap[c] || c;
            return (
              <label key={c} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                checked ? "border-border bg-subtle"
                  : "border-border hover:border-border dark:hover:border-border"}`}>
                <input type="checkbox" aria-label="Select item" checked={checked} onChange={() => toggle(c)} className="mt-0.5 rounded" />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm">{CONTENT_ICONS[c] || ""}</span>
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
                  <div className="text-xs text-muted mt-1">{b.size_mb} MB &middot; {new Date(b.created_at).toLocaleString()}</div>
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
      <div className="card p-6">
        <h3 className="font-medium dark:text-white mb-1">{t("backup.restore_title")}</h3>
        <p className="text-xs text-muted mb-4">{t("backup.restore_desc")}</p>
        <input ref={fileRef} type="file" accept=".tar.gz" className="hidden" onChange={handleFileSelected} />
        <button onClick={handleRestoreClick} disabled={isRestoring}
          className="btn-danger">
          {isRestoring ? t("backup.restoring") : t("backup.restore_btn")}
        </button>
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
