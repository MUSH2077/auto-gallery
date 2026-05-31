"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DedupSettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

function Toggle({ checked, onChange }: { checked: boolean; onChange: () => void }) {
  return (
    <button onClick={onChange}
      className={"relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 " + (checked ? "bg-green-600" : "bg-gray-300 dark:bg-gray-600")}>
      <span className={"inline-block h-4 w-4 transform rounded-full bg-white transition-transform " + (checked ? "translate-x-6" : "translate-x-1")} />
    </button>
  );
}

export default function DedupSettingsPage() {
  const t = useT();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<DedupSettings | null>(null);
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (data: DedupSettings) => api.updateAdminSettings({ dedup: data }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.admin.settings }); setSaved(true); setTimeout(() => setSaved(false), 2000); },
  });

  const current = local || settings.data?.dedup;

  if (settings.isError) return <main className="max-w-4xl mx-auto p-6"><ErrorState message={settings.error?.message || t("dedup.failed")} onRetry={() => settings.refetch()} /></main>;
  if (!settings.data) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/3" /><div className="h-64 bg-gray-200 rounded" /></div></main>;
  if (!local && settings.data.dedup) setLocal({ ...settings.data.dedup });

  const toggle = (key: keyof DedupSettings) => {
    if (!current) return;
    const next = { ...current };
    if (typeof next[key] === "boolean") (next as any)[key] = !next[key];
    setLocal(next);
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; {t("dedup.back")}</Link>
      </div>
      <PageHeader title={t("dedup.title")} description={t("dedup.desc")} />

      {current && (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-sm p-6 space-y-1">
            {([
              ["source_level_enabled", t("dedup.source_level.desc")],
              ["cross_source_enabled", t("dedup.cross_source.desc")],
              ["auto_merge", t("dedup.auto_merge.desc")],
            ] as [keyof DedupSettings, string][]).map(([key, desc]) => (
              <div key={key} className="flex items-center justify-between py-3.5 border-b dark:border-slate-700 last:border-0">
                <div>
                  <span className="font-medium text-sm dark:text-white">{t("dedup." + key as any)}</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{desc}</p>
                </div>
                <Toggle checked={!!current[key]} onChange={() => toggle(key)} />
              </div>
            ))}

            <div className="flex items-center justify-between py-3.5">
              <div>
                <span className="font-medium text-sm dark:text-white">{t("dedup.phash")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{t("dedup.phash.desc")}</p>
              </div>
              <div className="flex items-center gap-1 bg-gray-100 dark:bg-slate-700 rounded-lg px-1">
                <input type="number" min={0} max={64} value={current.phash_threshold}
                  onChange={(e) => setLocal({ ...current, phash_threshold: parseInt(e.target.value) || 0 })}
                  className="w-14 border-0 bg-transparent px-2 py-1.5 text-sm font-mono text-center dark:text-white" />
                <span className="text-xs text-gray-500 dark:text-gray-400 pr-2">bits</span>
              </div>
            </div>
          </div>

          <div className="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-100 dark:border-yellow-900/50 rounded-xl text-sm text-yellow-800 dark:text-yellow-300">
            <strong>&#9888; {t("dedup.warning")}</strong>
            {current.auto_merge && <span className="block mt-1 text-red-600 dark:text-red-400 font-medium">{t("dedup.warning_auto")}</span>}
          </div>

          <div className="mt-4 flex justify-end items-center">
            {saved && <span className="mr-3 text-green-600 dark:text-green-400 text-sm">{t("common.saved")}</span>}
            {save.error && <span className="mr-3 text-red-600 text-sm">{(save.error as Error).message}</span>}
            <button onClick={() => save.mutate(current)} disabled={save.isPending}
              className="px-6 py-2.5 bg-slate-900 dark:bg-slate-700 text-white rounded-lg text-sm font-medium hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50 transition-colors">
              {save.isPending ? t("common.saving") : t("dedup.save")}
            </button>
          </div>
        </>
      )}
    </main>
  );
}
