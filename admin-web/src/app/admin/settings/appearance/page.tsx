"use client";

import Link from "next/link";
import { PageHeader } from "@/components";
import {
  type WorkPreviewDelayMs,
  type WorkPreviewSize,
  type WorkPreviewWheelSensitivity,
  useAppearanceSettings,
} from "@/lib/appearance";
import { PALETTES, PALETTE_LABELS, PALETTE_SWATCH, type Palette, type Theme, useTheme } from "@/lib/theme";
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
  const { theme, setTheme, palette, setPalette } = useTheme();
  const { settings, updateSettings, resetSettings } = useAppearanceSettings();

  const themeOptions: { value: Theme; label: string }[] = [
    { value: "light", label: t("theme.light", "Light") },
    { value: "dark", label: t("theme.dark", "Dark") },
    { value: "system", label: t("theme.system", "System") },
  ];
  const previewDelayOptions: WorkPreviewDelayMs[] = [150, 250, 400];
  const previewSizeOptions: { value: WorkPreviewSize; label: string }[] = [
    { value: "medium", label: t("appearance.preview_size_medium", "Medium") },
    { value: "large", label: t("appearance.preview_size_large", "Large") },
    { value: "fit", label: t("appearance.preview_size_fit", "Fit") },
  ];
  const sensitivityOptions: { value: WorkPreviewWheelSensitivity; label: string }[] = [
    { value: "normal", label: t("appearance.wheel_normal", "Normal") },
    { value: "relaxed", label: t("appearance.wheel_relaxed", "Relaxed") },
  ];

  return (
    <main className="mx-auto max-w-4xl p-6 page-transition">
      <div className="mb-4">
        <Link href="/admin/settings" className="text-sm text-accent hover:underline">
          {t("common.back", "Back")}
        </Link>
      </div>
      <PageHeader title={t("appearance.title", "Appearance")} description={t("appearance.desc", "Display and gallery interaction preferences.")} />

      <section className="card p-5">
        <h2 className="section-title mb-2">{t("appearance.interface", "Interface")}</h2>
        <SettingRow title={t("appearance.theme", "Theme")} description={t("appearance.theme_desc", "Choose light, dark, or system mode.")}>
          <div className="segmented-control">
            {themeOptions.map((option) => (
              <OptionButton key={option.value} value={option.value} activeValue={theme} onSelect={setTheme}>
                {option.label}
              </OptionButton>
            ))}
          </div>
        </SettingRow>
        <SettingRow title={t("appearance.palette", "Palette")} description={t("appearance.palette_desc", "Select the accent palette for the admin UI.")}>
          <div className="flex flex-wrap justify-end gap-1.5">
            {PALETTES.map((p: Palette) => (
              <button
                key={p}
                type="button"
                onClick={() => setPalette(p)}
                aria-pressed={p === palette}
                className={`flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs transition-colors ${
                  p === palette ? "border-accent bg-accent-subtle text-accent" : "border-border text-muted hover:bg-subtle hover:text-fg"
                }`}
              >
                <span className="h-3.5 w-3.5 rounded-full border border-border" style={{ background: PALETTE_SWATCH[p] }} aria-hidden />
                {PALETTE_LABELS[p]}
              </button>
            ))}
          </div>
        </SettingRow>
      </section>

      <section className="card mt-5 p-5">
        <h2 className="section-title mb-2">{t("appearance.works", "Works")}</h2>
        <SettingRow title={t("appearance.work_preview", "Enlarged preview")} description={t("appearance.work_preview_desc", "Show original-image hover previews on the works grid.")}>
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
        <SettingRow title={t("appearance.preview_delay", "Preview delay")} description={t("appearance.preview_delay_desc", "Delay before the enlarged preview opens.")}>
          <div className="segmented-control">
            {previewDelayOptions.map((delay) => (
              <OptionButton key={delay} value={delay} activeValue={settings.workPreviewDelayMs} onSelect={(value) => updateSettings({ workPreviewDelayMs: value })}>
                {delay}ms
              </OptionButton>
            ))}
          </div>
        </SettingRow>
        <SettingRow title={t("appearance.preview_size", "Preview size")} description={t("appearance.preview_size_desc", "Maximum size for the original-image preview.")}>
          <div className="segmented-control">
            {previewSizeOptions.map((option) => (
              <OptionButton key={option.value} value={option.value} activeValue={settings.workPreviewSize} onSelect={(value) => updateSettings({ workPreviewSize: value })}>
                {option.label}
              </OptionButton>
            ))}
          </div>
        </SettingRow>
        <SettingRow title={t("appearance.wheel_sensitivity", "Wheel paging")} description={t("appearance.wheel_sensitivity_desc", "How much wheel movement is needed to switch pages.")}>
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
          <div className="text-sm font-medium text-fg">{t("appearance.reset", "Reset appearance")}</div>
          <div className="mt-1 text-xs text-muted">{t("appearance.reset_desc", "Restore gallery interaction preferences to defaults.")}</div>
        </div>
        <button
          type="button"
          onClick={resetSettings}
          className="rounded-md border border-border px-3 py-1.5 text-sm font-medium text-muted hover:bg-subtle hover:text-fg"
        >
          {t("common.reset", "Reset")}
        </button>
      </section>
    </main>
  );
}
