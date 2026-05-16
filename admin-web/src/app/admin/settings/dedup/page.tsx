"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DedupSettings } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function DedupSettingsPage() {
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
      <PageHeader title="Deduplication Settings" description="Control duplicate detection and merge behavior." />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 text-sm space-y-2">
            {([
              ["source_level_enabled", "Same source + same ID = skip download. Safe to enable.", "source-level"],
              ["cross_source_enabled", "SHA-256 match across sources = reuse asset record.", "cross-source"],
              ["auto_merge", "Automatically merge visually similar works. DANGEROUS — may irreversibly modify your library.", "auto-merge"],
            ] as [keyof DedupSettings, string, string][]).map(([key, desc, label]) => (
              <div key={key} className="flex items-center justify-between py-3 border-b dark:border-slate-700 last:border-0">
                <div>
                  <span className="font-medium capitalize">{key.replace(/_/g, " ")}</span>
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
                <span className="font-medium">phash threshold</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Perceptual hash Hamming distance (0-64). Lower = stricter matching. Default 8.</p>
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
            <strong>Changes take effect immediately.</strong> Enable dedup settings only after reviewing the risk documentation.
            {current.auto_merge && <span className="block mt-1 text-red-600 font-medium">Auto-merge is enabled — this may irreversibly modify your library!</span>}
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600 disabled:opacity-50"
            >
              {save.isPending ? "Saving..." : "Save Settings"}
            </button>
          </div>
          {save.isSuccess && <p className="text-green-600 text-sm mt-2">Settings saved.</p>}
          {save.error && <p className="text-red-600 text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </main>
  );
}
