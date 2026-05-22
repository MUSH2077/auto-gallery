"use client";
import { useState, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import Link from "next/link";
import { PageHeader, ConfirmDialog, EmptyState, ErrorState } from "@/components";

export default function BackupPage() {
  const t = useT();
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [confirmRestore, setConfirmRestore] = useState(false);
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const [result, setResult] = useState<{ ok: boolean; msg: string } | null>(null);

  const backups = useQuery({
    queryKey: ["backups"],
    queryFn: api.listBackups,
  });

  const createBackup = useMutation({
    mutationFn: () => api.createBackup(),
    onSuccess: (data) => {
      setResult({
        ok: true,
        msg: t("backup.created").replace("{filename}", data.filename).replace("{size}", String(data.size_mb)),
      });
      qc.invalidateQueries({ queryKey: ["backups"] });
    },
    onError: (e) => setResult({ ok: false, msg: (e as Error).message }),
  });

  const handleRestoreClick = () => {
    fileRef.current?.click();
  };

  const handleFileSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      setRestoreFile(file);
      setConfirmRestore(true);
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  const doRestore = async () => {
    if (!restoreFile) return;
    setConfirmRestore(false);
    try {
      const formData = new FormData();
      formData.append("file", restoreFile);
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/api/v1/admin/backup/restore`, {
        method: "POST",
        headers: { "X-Admin-Key": process.env.NEXT_PUBLIC_ADMIN_KEY || "changeme" },
        body: formData,
      });
      const data = await res.json();
      if (data.status === "ok") {
        setResult({ ok: true, msg: t("backup.restored") });
        qc.invalidateQueries();
      } else {
        setResult({ ok: false, msg: data.message || "Restore failed" });
      }
    } catch (e) {
      setResult({ ok: false, msg: (e as Error).message });
    }
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("common.back")}</Link>
      </div>
      <PageHeader title={t("backup.title")} description={t("backup.desc")} />

      {/* Result message */}
      {result && (
        <div className={`mb-4 p-3 rounded-lg text-sm ${result.ok ? "bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 text-green-700 dark:text-green-400" : "bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-700 dark:text-red-400"}`}>
          {result.msg}
          <button onClick={() => setResult(null)} className="ml-3 text-xs underline">{t("common.close")}</button>
        </div>
      )}

      {/* Create Backup */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="font-medium dark:text-white">{t("backup.create")}</h3>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("backup.desc")}</p>
          </div>
          <button
            onClick={() => createBackup.mutate()}
            disabled={createBackup.isPending}
            className="px-4 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
          >
            {createBackup.isPending ? t("backup.creating") : t("backup.create")}
          </button>
        </div>

        <div className="border-t dark:border-slate-700 pt-4">
          <h4 className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">{t("backup.contents")}</h4>
          <ul className="text-xs text-gray-500 dark:text-gray-400 space-y-1 list-disc list-inside">
            <li>{t("backup.item_db")}</li>
            <li>{t("backup.item_config")}</li>
            <li>{t("backup.item_appconfig")}</li>
            <li>{t("backup.item_archives")}</li>
          </ul>
        </div>
      </div>

      {/* Existing Backups */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 mb-6">
        <h3 className="font-medium dark:text-white mb-4">{t("backup.download")}</h3>
        {backups.isLoading && (
          <div className="animate-pulse space-y-2">
            {Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-10 bg-gray-100 dark:bg-slate-700 rounded" />)}
          </div>
        )}
        {backups.error && <ErrorState message={(backups.error as Error).message} onRetry={() => backups.refetch()} />}
        {backups.data?.backups && backups.data.backups.length === 0 && (
          <EmptyState title={t("backup.no_backups")} description={t("backup.no_backups_desc")} />
        )}
        {backups.data?.backups && backups.data.backups.length > 0 && (
          <div className="space-y-2">
            {backups.data.backups.map((b) => (
              <div key={b.filename} className="flex items-center justify-between p-3 bg-gray-50 dark:bg-slate-700/50 rounded-lg text-sm">
                <div>
                  <div className="font-medium dark:text-white">{b.filename}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">
                    {b.size_mb} MB &middot; {new Date(b.created_at).toLocaleString()}
                  </div>
                </div>
                <a
                  href={api.downloadBackup(b.filename)}
                  className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700"
                  download
                >
                  {t("backup.download")}
                </a>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Restore */}
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6">
        <h3 className="font-medium dark:text-white mb-1">{t("backup.restore_title")}</h3>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-4">{t("backup.restore_desc")}</p>
        <input ref={fileRef} type="file" accept=".tar.gz" className="hidden" onChange={handleFileSelected} />
        <button
          onClick={handleRestoreClick}
          className="px-4 py-2 bg-red-600 text-white rounded text-sm hover:bg-red-700"
        >
          {t("backup.restore_btn")}
        </button>
      </div>

      {confirmRestore && (
        <ConfirmDialog
          open
          title={t("backup.restore_title")}
          message={t("backup.restore_confirm")}
          onConfirm={doRestore}
          onCancel={() => setConfirmRestore(false)}
          isPending={false}
        />
      )}
    </main>
  );
}
