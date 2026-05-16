"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DownloadDefaults } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function DownloadDefaultsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<DownloadDefaults | null>(null);

  const save = useMutation({
    mutationFn: (data: DownloadDefaults) => api.updateAdminSettings({ download_defaults: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const current = local || settings.data?.download_defaults;

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
          <div className="h-48 bg-gray-200 rounded" />
        </div>
      </main>
    );
  }

  if (!local && settings.data.download_defaults) {
    setLocal({ ...settings.data.download_defaults });
  }

  const setNum = (key: keyof DownloadDefaults, val: number) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Download Job Defaults" description="Configure timeout, retry, and backoff for gallery-dl download jobs." />

      {!current ? null : (
        <>
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow p-6 space-y-5 text-sm">
            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">Timeout</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Maximum time for a single gallery-dl download job (seconds). Default 600.</p>
              </div>
              <input
                type="number" min={60} max={3600} step={60}
                value={current.timeout_seconds}
                onChange={(e) => setNum("timeout_seconds", parseInt(e.target.value) || 600)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b dark:border-slate-700">
              <div>
                <span className="font-medium">Max Retries</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Number of retry attempts after a failed download before marking as failed. Default 3.</p>
              </div>
              <input
                type="number" min={0} max={10}
                value={current.max_retries}
                onChange={(e) => setNum("max_retries", parseInt(e.target.value) || 3)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">Retry Backoff Base</span>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Base delay in seconds for exponential backoff. Formula: base × 2^(retry-1). Default 60.</p>
              </div>
              <input
                type="number" min={10} max={600} step={10}
                value={current.retry_backoff_base_seconds}
                onChange={(e) => setNum("retry_backoff_base_seconds", parseInt(e.target.value) || 60)}
                className="w-20 border rounded px-2 py-1 text-sm font-mono text-center"
              />
            </div>
          </div>

          <div className="mt-4 p-4 bg-blue-50 dark:bg-blue-900/30 border border-blue-200 dark:border-blue-800 rounded-lg text-sm text-blue-800 dark:text-blue-300">
            <strong>Current retry schedule:</strong> Attempt 1 → fail → wait {current.retry_backoff_base_seconds}s → Attempt 2
            → fail → wait {current.retry_backoff_base_seconds * 2}s → Attempt 3 → fail → marked as failed. Max {current.max_retries} retries.
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
