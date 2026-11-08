"use client";

import { SettingsPageHeader as PageHeader, SettingsPageShell as PageShell } from "@/app/admin/settings/GallerySettingsFrame";
import { useAppearanceSettings } from "@/lib/appearance";
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
  const { resetSettings } = useAppearanceSettings();

  const themeOptions: { value: Theme; label: string }[] = [
    { value: "light", label: t("theme.light") },
    { value: "dark", label: t("theme.dark") },
  ];
  return (
    <PageShell>
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
