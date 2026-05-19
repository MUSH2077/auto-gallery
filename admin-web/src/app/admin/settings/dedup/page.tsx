"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DedupSettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";
import Link from "next/link";

export default function DedupSettingsPage() {
  const t = useT();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<DedupSettings | null>(null);

  const save = useMutation({
    mutationFn: (data: DedupSettings) => api.updateAdminSettings({ dedup: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const current = local || settings.data?.dedup;

  if (settings.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={settings.error?.message || "Failed"} onRetry={() => settings.refetch()} />
      </main>
    );
  }

  if (!settings.data) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-64 bg-gray-200 rounded" />
        </div>
      </main>
    );
  }

  // Init local state on first load
  if (!local && settings.data.dedup) {
    setLocal({ ...settings.data.dedup });
  }

  const toggle = (key: keyof DedupSettings) => {
    if (!current) return;
    const next = { ...current };
    if (typeof next[key] === "boolean") {
      (next as Record<string, unknown>)[key] = !next[key];
    }
    setLocal(next);
  };

  const setNumber = (key: keyof DedupSettings, val: number) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title={t("dedup.title")} description={t("dedup.desc")} />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 text-sm space-y-2">
            {([
              ["source_level_enabled", t("dedup.source_level.desc")],
              ["cross_source_enabled", t("dedup.cross_source.desc")],
              ["auto_merge", t("dedup.auto_merge.desc")],
            ] as [keyof DedupSettings, string][]).map(([key, desc]) => (
              <div key={key} className="flex items-center justify-between py-3 border-b dark:border-slate-700 last:border-0">
                <div>
                  <span className="font-medium capitalize">{t(`dedup.${key}` as any)}</span>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{desc}</p>
                </div>
                <button
                  onClick={() => toggle(key)}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors shrink-0 ${
                    current[key] ? "bg-green-600" : "bg-gray-300"
                  }`}
                >
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    current[key] ? "translate-x-6" : "translate-x-1"
                  }`} />
                </button>
              </div>
            ))}

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">{t("dedup.phash")}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{t("dedup.phash.desc")}</p>
              </div>
              <input
                type="number" min={0} max={64}
                value={current.phash_threshold}
                onChange={(e) => setNumber("phash_threshold", parseInt(e.target.value) || 0)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>
          </div>

          <div className="mt-4 p-4 bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg text-sm text-yellow-800 dark:text-yellow-300">
            <strong>{t("dedup.warning")}</strong>
            {current.auto_merge && <span className="block mt-1 text-red-600 font-medium">{t("dedup.warning_auto")}</span>}
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
            >
              {save.isPending ? t("common.saving") : t("dedup.save")}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">{t("dedup.saved")}</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}
