"use client";

import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { BusinessPage } from "@/features/gallery-ui/BusinessPage";
import { Breadcrumb } from "@/components/Breadcrumb";
import { adminBreadcrumbParents } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

export function SettingsPageShell({ children }: { children: ReactNode }) {
  return <BusinessPage className="gallery-settings-page">{children}</BusinessPage>;
}

export function SettingsPageHeader({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
}) {
  const pathname = usePathname();
  const t = useT();
  const breadcrumbParents = adminBreadcrumbParents(pathname);

  return (
    <header data-page-header className="business-heading settings-page-header">
      {breadcrumbParents.length > 0 ? (
        <Breadcrumb
          className="settings-breadcrumb"
          items={breadcrumbParents.map((item) => ({ label: t(item.labelKey), href: item.href }))}
        />
      ) : null}
      <h1>{title}</h1>
      {description ? <p>{description}</p> : null}
      {children ? <div className="business-heading-actions">{children}</div> : null}
    </header>
  );
}
