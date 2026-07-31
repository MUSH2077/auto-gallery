"use client";

import { type ReactNode } from "react";
import { usePathname } from "next/navigation";

import { Breadcrumb } from "@/components/Breadcrumb";
import PageContextNav from "@/components/PageContextNav";
import { findAdminNavEntry } from "@/lib/adminNavigation";
import { adminBreadcrumbParents } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

export default function PageHeader({
  title,
  description,
  meta,
  primaryAction,
  secondaryActions,
  children,
}: {
  title: string;
  description?: ReactNode;
  meta?: ReactNode;
  primaryAction?: ReactNode;
  secondaryActions?: ReactNode;
  children?: ReactNode;
}) {
  const pathname = usePathname();
  const t = useT();
  const navEntry = findAdminNavEntry(pathname);
  const breadcrumbParents = adminBreadcrumbParents(pathname);

  const actions = primaryAction || secondaryActions || children;

  return (
    <header data-page-header className="mb-6 min-w-0 border-b border-border pb-4">
      <div className="flex min-w-0 flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0 lg:min-w-[16rem] lg:flex-1">
          {breadcrumbParents.length > 0 ? (
            <Breadcrumb
              className="mb-1.5 text-xs font-medium"
              items={[
                ...breadcrumbParents.map((item) => ({ label: t(item.labelKey), href: item.href })),
                { label: title },
              ]}
            />
          ) : navEntry && navEntry.groupKey !== navEntry.labelKey ? (
            <div className="mb-1.5 flex min-w-0 items-center gap-1.5 text-xs font-medium text-muted">
              <span className="truncate">{t(navEntry.groupKey)}</span>
              <span aria-hidden>/</span>
              <span className="truncate">{t(navEntry.labelKey)}</span>
            </div>
          ) : null}
          <h1 className="max-w-full text-2xl font-semibold leading-tight tracking-[-0.015em] text-fg [overflow-wrap:normal]">
            {title}
          </h1>
          {description && <div className="mt-1.5 max-w-[70ch] text-sm leading-5 text-muted">{description}</div>}
          {meta && <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">{meta}</div>}
        </div>
        {actions && (
          <div className="flex w-full min-w-0 flex-wrap items-center gap-2 lg:max-w-[65%] lg:flex-1 lg:justify-end [&>*]:max-w-full">
            {secondaryActions}
            {primaryAction}
            {!primaryAction && !secondaryActions ? children : null}
          </div>
        )}
      </div>
      <PageContextNav />
    </header>
  );
}
