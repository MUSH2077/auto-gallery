"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { DownloadDefaultsSettingsContent } from "@/app/admin/settings/download-defaults/page";
import { SyncScheduleSettingsContent } from "@/app/admin/settings/subscription-defaults/page";
import { PageHeader, PageShell, PermissionGuard, UrlTabs } from "@/components";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

type AutomationTab = "schedule" | "downloads";
const AUTOMATION_TABS: readonly AutomationTab[] = ["schedule", "downloads"];

export default function AutomationSettingsPage() {
  const t = useT();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const activeTab: AutomationTab = AUTOMATION_TABS.includes(requestedTab as AutomationTab)
    ? requestedTab as AutomationTab
    : "schedule";
  const paramsKey = searchParams.toString();
  const [dirty, setDirty] = useState({ schedule: false, downloads: false });
  const hasUnsavedChanges = dirty.schedule || dirty.downloads;
  const setScheduleDirty = useCallback((value: boolean) => {
    setDirty((current) => current.schedule === value ? current : { ...current, schedule: value });
  }, []);
  const setDownloadsDirty = useCallback((value: boolean) => {
    setDirty((current) => current.downloads === value ? current : { ...current, downloads: value });
  }, []);

  useEffect(() => {
    if (!requestedTab || requestedTab === activeTab) return;
    const next = new URLSearchParams(paramsKey);
    next.set("tab", activeTab);
    router.replace(`${adminRoutes.settingsAutomation}?${next.toString()}`, { scroll: false });
  }, [activeTab, paramsKey, requestedTab, router]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const guardLinks = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as HTMLElement | null)?.closest("a");
      if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download")) return;
      const target = new URL(anchor.href, window.location.href);
      if (target.origin !== window.location.origin || target.pathname === adminRoutes.settingsAutomation) return;
      if (!window.confirm(t("settings.unsaved_confirm"))) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("click", guardLinks, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      document.removeEventListener("click", guardLinks, true);
    };
  }, [hasUnsavedChanges, t]);

  return (
    <PermissionGuard module="system">
      <PageShell>
        <PageHeader title={t("settings.automation")} description={t("settings.automation_desc")} />
        <UrlTabs
          activeId={activeTab}
          ariaLabel={t("settings.automation_sections")}
          tabs={[
            { id: "schedule", label: t("settings.sync_schedule"), href: `${adminRoutes.settingsAutomation}?tab=schedule` },
            { id: "downloads", label: t("settings.download_behavior"), href: `${adminRoutes.settingsAutomation}?tab=downloads` },
          ]}
        />
        <section hidden={activeTab !== "schedule"} role="tabpanel" aria-label={t("settings.sync_schedule")}>
          <SyncScheduleSettingsContent onDirtyChange={setScheduleDirty} />
        </section>
        <section hidden={activeTab !== "downloads"} role="tabpanel" aria-label={t("settings.download_behavior")}>
          <DownloadDefaultsSettingsContent onDirtyChange={setDownloadsDirty} />
        </section>
      </PageShell>
    </PermissionGuard>
  );
}
