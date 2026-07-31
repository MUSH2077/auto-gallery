"use client";
import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys, DownloadDefaults } from "@/lib/api";
import { PageHeader, PageShell, ErrorState } from "@/components";
import { useT } from "@/lib/i18n";

export function DownloadDefaultsSettingsContent({ onDirtyChange }: { onDirtyChange?: (dirty: boolean) => void }) {
  const t = useT();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });
  const [local, setLocal] = useState<DownloadDefaults | null>(null);

  useEffect(() => {
    if (settings.data?.download_defaults) {
      setLocal((current) => current ?? { ...settings.data!.download_defaults });
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: (data: DownloadDefaults) => api.updateAdminSettings({ download_defaults: data }),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.admin.settings }),
  });

  const current = local || settings.data?.download_defaults;
  const dirty = Boolean(local && settings.data?.download_defaults
    && JSON.stringify(local) !== JSON.stringify(settings.data.download_defaults));

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  if (settings.isError) {
    return (
      <ErrorState message={settings.error?.message || t("dldefaults.failed")} onRetry={() => settings.refetch()} />
    );
  }

  if (!settings.data) {
    return (
      <div className="animate-pulse space-y-4">
          <div className="h-8 rounded-md bg-subtle dark:bg-subtle w-1/3" />
          <div className="h-48 rounded-md bg-subtle dark:bg-subtle" />
      </div>
    );
  }

  const setNum = (key: keyof DownloadDefaults, val: number) => {
    if (!current) return;
    setLocal({ ...current, [key]: val });
  };

  return (
    <>
      {!current ? null : (
        <>
          <div className="card p-6 space-y-5 text-sm">
            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.timeout")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.timeout.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.timeout")}
                type="number" min={60} max={3600} step={60}
                value={current.timeout_seconds}
                onChange={(e) => setNum("timeout_seconds", parseInt(e.target.value) || 600)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.stall_timeout")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.stall_timeout.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.stall_timeout")}
                type="number" min={30} max={600} step={30}
                value={current.stall_timeout_seconds}
                onChange={(e) => setNum("stall_timeout_seconds", parseInt(e.target.value) || 120)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.retries")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.retries.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.retries")}
                type="number" min={0} max={10}
                value={current.max_retries}
                onChange={(e) => setNum("max_retries", parseInt(e.target.value) || 3)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">{t("dldefaults.backoff")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.backoff.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.backoff")}
                type="number" min={10} max={600} step={10}
                value={current.retry_backoff_base_seconds}
                onChange={(e) => setNum("retry_backoff_base_seconds", parseInt(e.target.value) || 60)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.gallerydl_retries")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.gallerydl_retries.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.gallerydl_retries")}
                type="number" min={1} max={10}
                value={current.gallerydl_retries}
                onChange={(e) => setNum("gallerydl_retries", parseInt(e.target.value) || 3)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.gallerydl_timeout")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.gallerydl_timeout.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.gallerydl_timeout")}
                type="number" min={10} max={120} step={5}
                value={current.gallerydl_timeout}
                onChange={(e) => setNum("gallerydl_timeout", parseInt(e.target.value) || 30)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3">
              <div>
                <span className="font-medium">{t("dldefaults.gallerydl_abort")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.gallerydl_abort.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.gallerydl_abort")}
                type="number" min={1} max={20}
                value={current.gallerydl_abort}
                onChange={(e) => setNum("gallerydl_abort", parseInt(e.target.value) || 5)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.concurrency")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.concurrency.desc")}</p>
              </div>
              <select
                aria-label={t("dldefaults.concurrency")}
                value={current.download_concurrency ?? 3}
                onChange={(e) => setNum("download_concurrency", parseInt(e.target.value) || 3)}
                className="input w-20 px-2 py-1 text-center font-mono"
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.max_posts")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.max_posts.desc")}</p>
              </div>
              <input
                aria-label={t("dldefaults.max_posts")}
                type="number" min={10} max={10000} step={10}
                value={current.max_posts}
                onChange={(e) => setNum("max_posts", parseInt(e.target.value) || 200)}
                className="input w-20 px-2 py-1 text-center font-mono"
              />
            </div>

            <div className="flex items-center justify-between py-3 border-b border-border">
              <div>
                <span className="font-medium">{t("dldefaults.skip_ai")}</span>
                <p className="text-xs text-muted mt-1">{t("dldefaults.skip_ai.desc")}</p>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" aria-label={t("dldefaults.skip_ai")}
                  checked={current.skip_ai_generated}
                  onChange={(e) => { if (current) setLocal({ ...current, skip_ai_generated: e.target.checked }); }}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-subtle peer-focus:outline-none rounded-full peer dark:bg-subtle peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-border after:border after:rounded-full after:h-4 after:w-4 after:transition-all dark:border-border peer-checked:bg-subtle dark:peer-checked:bg-subtle"></div>
              </label>
            </div>
          </div>

          <div className="mt-4 p-4 bg-accent-subtle border border-accent/30 rounded-lg text-sm text-accent">
            <strong>{t("dldefaults.schedule")}:</strong>{" "}
            {t("dldefaults.schedule.desc").replace("{base}", String(current.retry_backoff_base_seconds)).replace("{double}", String(current.retry_backoff_base_seconds * 2)).replace("{max}", String(current.max_retries))}
          </div>

          <div className="mt-4 flex justify-end">
            <button
              onClick={() => save.mutate(current)}
              disabled={save.isPending}
              className="btn-primary px-6"
            >
              {save.isPending ? t("common.saving") : t("dldefaults.save")}
            </button>
          </div>
          {save.isSuccess && <p className="text-success text-sm mt-2">{t("dldefaults.saved")}</p>}
          {save.error && <p className="text-danger text-sm mt-2">{(save.error as Error).message}</p>}
        </>
      )}
    </>
  );
}

export default function DownloadDefaultsPage() {
  const t = useT();
  return (
    <PageShell>
      <PageHeader title={t("dldefaults.title")} description={t("dldefaults.desc")} />
      <DownloadDefaultsSettingsContent />
    </PageShell>
  );
}
