"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, DedupSettings, queryKeys } from "@/lib/api";
import { ErrorState, PageHeader, PageShell } from "@/components";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";

function NumberSetting({
  label,
  description,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  label: string;
  description: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix: string;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-border py-4 last:border-0 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div className="text-sm font-medium text-fg">{label}</div>
        <p className="mt-1 max-w-2xl text-xs leading-5 text-muted">{description}</p>
      </div>
      <label className="flex shrink-0 items-center gap-2">
        <input
          aria-label={label}
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(event) => {
            const next = Number(event.target.value);
            if (Number.isFinite(next)) onChange(next);
          }}
          className="input w-24 text-center font-mono"
        />
        <span className="w-10 text-xs text-muted">{suffix}</span>
      </label>
    </div>
  );
}

export default function DedupSettingsPage() {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: queryKeys.admin.settings,
    queryFn: api.getAdminSettings,
  });
  const [local, setLocal] = useState<DedupSettings | null>(null);

  useEffect(() => {
    if (settings.data?.dedup) setLocal({ ...settings.data.dedup });
  }, [settings.data?.dedup]);

  const save = useMutation({
    mutationFn: (data: DedupSettings) =>
      api.updateAdminSettings({ dedup: data }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.admin.settings });
      toast.success({ message: t("notification.saved") });
    },
  });

  if (settings.isError) {
    return (
      <PageShell>
        <ErrorState
          message={settings.error?.message || t("dedup.failed")}
          onRetry={() => settings.refetch()}
        />
      </PageShell>
    );
  }
  if (!local) {
    return (
      <PageShell>
        <div className="space-y-4 animate-pulse">
          <div className="h-8 w-1/3 rounded-md bg-subtle" />
          <div className="h-96 rounded-md bg-subtle" />
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <PageHeader title={t("dedup.title")} description={t("dedup.desc")} />

      <section className="card p-5">
        <div className="flex items-center justify-between border-b border-border pb-4">
          <div>
            <div className="text-sm font-medium text-fg">
              {t("dedup.auto_group")}
            </div>
            <p className="mt-1 text-xs leading-5 text-muted">
              {t("dedup.auto_group.desc")}
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={Boolean(local.auto_group_enabled)}
            aria-label={t("dedup.auto_group")}
            onClick={() =>
              setLocal({
                ...local,
                auto_group_enabled: !local.auto_group_enabled,
              })
            }
            className="relative inline-flex h-11 w-12 shrink-0 items-center justify-center rounded-md"
          >
            <span className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors ${
              local.auto_group_enabled ? "bg-success" : "bg-subtle"
            }`}>
              <span
                className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${
                  local.auto_group_enabled ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </span>
          </button>
        </div>

        <NumberSetting
          label={t("dedup.phash")}
          description={t("dedup.phash.desc")}
          value={local.phash_threshold}
          min={0}
          max={4}
          step={1}
          suffix={t("dedup.bits")}
          onChange={(value) => setLocal({ ...local, phash_threshold: value })}
        />
        <NumberSetting
          label={t("dedup.ssim")}
          description={t("dedup.ssim.desc")}
          value={local.ssim_threshold}
          min={0.9}
          max={1}
          step={0.001}
          suffix=""
          onChange={(value) => setLocal({ ...local, ssim_threshold: value })}
        />
        <NumberSetting
          label={t("dedup.aspect")}
          description={t("dedup.aspect.desc")}
          value={local.aspect_ratio_tolerance}
          min={0}
          max={0.05}
          step={0.001}
          suffix=""
          onChange={(value) =>
            setLocal({ ...local, aspect_ratio_tolerance: value })
          }
        />
        <NumberSetting
          label={t("dedup.auto_score")}
          description={t("dedup.auto_score.desc")}
          value={local.auto_group_score}
          min={70}
          max={100}
          step={1}
          suffix="/100"
          onChange={(value) =>
            setLocal({
              ...local,
              auto_group_score: value,
              review_score: Math.min(local.review_score, value),
            })
          }
        />
        <NumberSetting
          label={t("dedup.review_score")}
          description={t("dedup.review_score.desc")}
          value={local.review_score}
          min={0}
          max={local.auto_group_score}
          step={1}
          suffix="/100"
          onChange={(value) => setLocal({ ...local, review_score: value })}
        />
        <NumberSetting
          label={t("dedup.quarantine")}
          description={t("dedup.quarantine.desc")}
          value={local.quarantine_days}
          min={1}
          max={365}
          step={1}
          suffix={t("dedup.days")}
          onChange={(value) => setLocal({ ...local, quarantine_days: value })}
        />
      </section>

      <div className="mt-4 rounded-md border border-border bg-subtle/40 p-4 text-sm leading-6 text-muted">
        {t("dedup.safety_note")}
      </div>

      <div className="mt-5 flex items-center justify-end">
        {save.error && (
          <span className="mr-3 text-sm text-danger">
            {(save.error as Error).message}
          </span>
        )}
        <button
          type="button"
          onClick={() => save.mutate(local)}
          disabled={save.isPending}
          className="btn-primary min-h-11 px-6"
        >
          {save.isPending ? t("common.saving") : t("dedup.save")}
        </button>
      </div>
    </PageShell>
  );
}
