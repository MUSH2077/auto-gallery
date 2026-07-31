"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import {
  GalleryDLSettingsContent,
  type GallerySourceTab,
} from "@/app/admin/settings/gallerydl/page";
import { ProxySettingsContent } from "@/app/admin/settings/proxy/page";
import { PageHeader, PageShell, PermissionGuard, UrlTabs } from "@/components";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

type ConnectivityTab = "extractors" | "proxy";
const CONNECTIVITY_TABS: readonly ConnectivityTab[] = ["extractors", "proxy"];
const SOURCE_TABS: readonly GallerySourceTab[] = [
  "pixiv", "twitter", "iwara", "danbooru", "pinterest", "lofter", "weibo", "bilibili",
];

export default function ConnectivitySettingsPage() {
  const t = useT();
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const requestedSource = searchParams.get("source");
  const activeTab: ConnectivityTab = CONNECTIVITY_TABS.includes(requestedTab as ConnectivityTab)
    ? requestedTab as ConnectivityTab
    : "extractors";
  const activeSource: GallerySourceTab = SOURCE_TABS.includes(requestedSource as GallerySourceTab)
    ? requestedSource as GallerySourceTab
    : "pixiv";
  const paramsKey = searchParams.toString();

  useEffect(() => {
    const tabInvalid = Boolean(requestedTab && requestedTab !== activeTab);
    const sourceInvalid = Boolean(requestedSource && requestedSource !== activeSource);
    if (!tabInvalid && !sourceInvalid) return;
    const next = new URLSearchParams(paramsKey);
    next.set("tab", activeTab);
    if (activeTab === "extractors") next.set("source", activeSource);
    router.replace(`${adminRoutes.settingsConnectivity}?${next.toString()}`, { scroll: false });
  }, [activeSource, activeTab, paramsKey, requestedSource, requestedTab, router]);

  const selectSource = (source: GallerySourceTab) => {
    const next = new URLSearchParams(searchParams.toString());
    next.set("tab", "extractors");
    next.set("source", source);
    router.push(`${adminRoutes.settingsConnectivity}?${next.toString()}`, { scroll: false });
  };

  return (
    <PermissionGuard module="system">
      <PageShell>
        <PageHeader title={t("settings.connectivity")} description={t("settings.connectivity_desc")} />
        <UrlTabs
          activeId={activeTab}
          ariaLabel={t("settings.connectivity_sections")}
          tabs={[
            {
              id: "extractors",
              label: t("settings.extractors"),
              href: `${adminRoutes.settingsConnectivity}?tab=extractors&source=${activeSource}`,
            },
            { id: "proxy", label: t("proxy.title"), href: `${adminRoutes.settingsConnectivity}?tab=proxy` },
          ]}
        />
        <section hidden={activeTab !== "extractors"} role="tabpanel" aria-label={t("settings.extractors")}>
          <GalleryDLSettingsContent activeSource={activeSource} onSourceChange={selectSource} />
        </section>
        <section hidden={activeTab !== "proxy"} role="tabpanel" aria-label={t("proxy.title")}>
          <ProxySettingsContent />
        </section>
      </PageShell>
    </PermissionGuard>
  );
}
