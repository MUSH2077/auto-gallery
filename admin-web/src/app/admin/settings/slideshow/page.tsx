"use client";

import type { ReactNode } from "react";
import { PageHeader, PageShell, PermissionGuard } from "@/components";
import { useSlideshowConfig, type SlideshowConfig } from "@/lib/slideshow/config";
import { useT } from "@/lib/i18n";

function SettingRow({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 border-b border-border py-4 last:border-0 md:flex-row md:items-center md:justify-between">
      <div className="text-sm font-medium text-fg">{title}</div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Toggle({ checked, label, onToggle }: { checked: boolean; label: string; onToggle: () => void }) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-pressed={checked}
      aria-label={label}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${checked ? "border-accent bg-accent-subtle text-accent" : "border-border text-muted hover:bg-subtle"}`}
    >
      {checked ? t("common.on") : t("common.off")}
    </button>
  );
}

export default function SlideshowSettingsPage() {
  const t = useT();
  const { config, update } = useSlideshowConfig();
  const transitions: Array<{ value: SlideshowConfig["slideTransition"]; label: string }> = [
    { value: "kenburns", label: t("slideshow_settings.transition_kenburns") },
    { value: "crossfade", label: t("slideshow_settings.transition_crossfade") },
  ];

  return (
    <PermissionGuard module="system">
      <PageShell>
        <PageHeader title={t("slideshow_settings.title")} description={t("slideshow_settings.desc")} />
        <section className="card p-5">
          <SettingRow title={`${t("slideshow_settings.dwell")} (${config.slideDwellMs}ms)`}>
            <input
              type="range"
              min={2000}
              max={15000}
              step={500}
              value={config.slideDwellMs}
              aria-label={t("slideshow_settings.dwell")}
              onChange={(event) => update({ slideDwellMs: Number(event.target.value) })}
              className="w-40 sm:w-56"
            />
          </SettingRow>
          <SettingRow title={t("slideshow_settings.transition")}>
            <div className="segmented-control">
              {transitions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => update({ slideTransition: option.value })}
                  aria-pressed={option.value === config.slideTransition}
                  className={`segment ${option.value === config.slideTransition ? "segment-active" : ""}`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </SettingRow>
          <SettingRow title={t("slideshow_settings.loop")}>
            <Toggle checked={config.slideLoop} label={t("slideshow_settings.loop")} onToggle={() => update({ slideLoop: !config.slideLoop })} />
          </SettingRow>
          <SettingRow title={t("slideshow_settings.meta")}>
            <Toggle checked={config.slideShowMeta} label={t("slideshow_settings.meta")} onToggle={() => update({ slideShowMeta: !config.slideShowMeta })} />
          </SettingRow>
        </section>
      </PageShell>
    </PermissionGuard>
  );
}
