"use client";

import Link from "next/link";
import { PageHeader, PageShell } from "@/components";
import {
  type WorkPreviewDelayMs,
  type WorkPreviewSize,
  type WorkPreviewWheelSensitivity,
  useAppearanceSettings,
} from "@/lib/appearance";
import { type Theme, useTheme } from "@/lib/theme";
import { useT } from "@/lib/i18n";

function OptionButton<T extends string | number>({
  value,
  activeValue,
  children,
  onSelect,
}: {
  value: T;
  activeValue: T;
  children: React.ReactNode;
  onSelect: (value: T) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(value)}
      aria-pressed={value === activeValue}
      className={`segment ${value === activeValue ? "segment-active" : ""}`}
    >
      {children}
    </button>
  );
}

function SettingRow({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-border py-4 last:border-0 md:flex-row md:items-center md:justify-between">
      <div className="min-w-0">
        <div className="text-sm font-medium text-fg">{title}</div>
        <div className="mt-1 text-xs text-muted">{description}</div>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

export default function AppearanceSettingsPage() {
  const t = useT();
  const { theme, setTheme } = useTheme();
  const { settings, updateSettings, resetSettings } = useAppearanceSettings();

  const themeOptions: { value: Theme; label: string }[] = [
    { value: "light", label: t("theme.light") },
    { value: "dark", label: t("theme.dark") },
    { value: "system", label: t("theme.system") },
  ];
  const previewDelayOptions: WorkPreviewDelayMs[] = [150, 250, 400];
  const previewSizeOptions: { value: WorkPreviewSize; label: string }[] = [
    { value: "medium", label: t("appearance.preview_size_medium") },
    { value: "large", label: t("appearance.preview_size_large") },
    { value: "fit", label: t("appearance.preview_size_fit") },
  ];
  const sensitivityOptions: { value: WorkPreviewWheelSensitivity; label: string }[] = [
    { value: "normal", label: t("appearance.wheel_normal") },
    { value: "relaxed", label: t("appearance.wheel_relaxed") },
  ];

  return (
    <PageShell>
      <div className="mb-4">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">
          {t("common.back")}
        </Link>
      </div>
      <PageHeader title={t("appearance.title")} description={t("appearance.desc")} />

      <section className="card p-5">
        <h2 className="section-title mb-2">{t("appearance.interface")}</h2>
        <SettingRow title={t("appearance.theme")} description={t("appearance.theme_desc")}>
          <div className="segmented-control">
            {themeOptions.map((option) => (
              <OptionButton key={option.value} value={option.value} activeValue={theme} onSelect={setTheme}>
                {option.label}
              </OptionButton>
            ))}
          </div>
        </SettingRow>
      </section>

      <section className="card mt-5 p-5">
        <h2 className="section-title mb-2">{t("appearance.works")}</h2>
        <SettingRow title={t("appearance.work_preview")} description={t("appearance.work_preview_desc")}>
          <button
            type="button"
            onClick={() => updateSettings({ workPreviewEnabled: !settings.workPreviewEnabled })}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
              settings.workPreviewEnabled ? "border-accent bg-accent-subtle text-accent" : "border-border text-muted hover:bg-subtle"
            }`}
            aria-pressed={settings.workPreviewEnabled}
          >
            {settings.workPreviewEnabled ? t("common.on") : t("common.off")}
          </button>
        </SettingRow>
        <SettingRow title={t("appearance.preview_delay")} description={t("appearance.preview_delay_desc")}>
          <div className="segmented-control">
            {previewDelayOptions.map((delay) => (
              <OptionButton key={delay} value={delay} activeValue={settings.workPreviewDelayMs} onSelect={(value) => updateSettings({ workPreviewDelayMs: value })}>
                {delay}ms
              </OptionButton>
            ))}
          </div>
        </SettingRow>
        <SettingRow title={t("appearance.preview_size")} description={t("appearance.preview_size_desc")}>
          <div className="segmented-control">
            {previewSizeOptions.map((option) => (
              <OptionButton key={option.value} value={option.value} activeValue={settings.workPreviewSize} onSelect={(value) => updateSettings({ workPreviewSize: value })}>
                {option.label}
              </OptionButton>
            ))}
          </div>
        </SettingRow>
        <SettingRow title={t("appearance.wheel_sensitivity")} description={t("appearance.wheel_sensitivity_desc")}>
          <div className="segmented-control">
            {sensitivityOptions.map((option) => (
              <OptionButton key={option.value} value={option.value} activeValue={settings.workPreviewWheelSensitivity} onSelect={(value) => updateSettings({ workPreviewWheelSensitivity: value })}>
                {option.label}
              </OptionButton>
            ))}
          </div>
        </SettingRow>
      </section>

      <section className="mt-5 flex items-center justify-between rounded-md border border-border bg-surface p-4">
        <div>
          <div className="text-sm font-medium text-fg">{t("appearance.reset")}</div>
          <div className="mt-1 text-xs text-muted">{t("appearance.reset_desc")}</div>
        </div>
        <button
          type="button"
          onClick={resetSettings}
          className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-muted hover:bg-subtle hover:text-fg"
        >
          {t("common.reset")}
        </button>
      </section>
    </PageShell>
  );
}
