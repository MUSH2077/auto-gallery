"use client";

import Link from "next/link";
import { Cable, Clock3, Users } from "lucide-react";

import { PageHeader, PageShell, PermissionGuard } from "@/components";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

export default function SettingsPage() {
  const t = useT();
  const { isAdmin } = usePermissions();
  const cards = [
    {
      href: `${adminRoutes.settingsAutomation}?tab=schedule`,
      title: t("settings.automation"),
      description: t("settings.automation_desc"),
      icon: Clock3,
      visible: true,
    },
    {
      href: `${adminRoutes.settingsConnectivity}?tab=extractors`,
      title: t("settings.connectivity"),
      description: t("settings.connectivity_desc"),
      icon: Cable,
      visible: true,
    },
    {
      href: adminRoutes.users,
      title: t("users.title"),
      description: t("settings.users_desc"),
      icon: Users,
      visible: isAdmin,
    },
  ].filter((card) => card.visible);

  return (
    <PermissionGuard module="system">
      <PageShell>
        <PageHeader title={t("settings.title")} description={t("settings.desc_default")} />
        <div data-page-primary-content className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {cards.map((card) => {
            const Icon = card.icon;
            return (
              <Link key={card.href} href={card.href} className="card-interactive block min-h-40 p-6">
                <span className="mb-5 inline-flex h-10 w-10 items-center justify-center rounded-md bg-accent-subtle text-accent">
                  <Icon className="h-5 w-5" strokeWidth={1.8} aria-hidden="true" />
                </span>
                <h2 className="text-base font-semibold text-fg">{card.title}</h2>
                <p className="mt-2 text-sm leading-5 text-muted">{card.description}</p>
              </Link>
            );
          })}
        </div>
      </PageShell>
    </PermissionGuard>
  );
}
