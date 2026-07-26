"use client";

import { type ReactNode } from "react";
import { usePathname } from "next/navigation";

import { findAdminNavEntry } from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";

export default function PageHeader({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children?: ReactNode;
}) {
  const pathname = usePathname();
  const t = useT();
  const navEntry = findAdminNavEntry(pathname);

  return (
    <header className="mb-6 flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {navEntry && navEntry.groupKey !== navEntry.labelKey && (
          <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted" aria-hidden>
            <span>{t(navEntry.groupKey)}</span>
            <svg viewBox="0 0 16 16" className="h-3 w-3" fill="currentColor">
              <path d="M6.22 3.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 1 1-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 0 1 0-1.06Z" />
            </svg>
            <span>{t(navEntry.labelKey)}</span>
          </div>
        )}
        <h1 className="text-balance text-2xl font-semibold tracking-normal text-fg">
          {title}
        </h1>
        {description && <div className="mt-1.5 max-w-3xl text-sm text-muted">{description}</div>}
      </div>
      {children && <div className="flex shrink-0 flex-wrap gap-2">{children}</div>}
    </header>
  );
}
