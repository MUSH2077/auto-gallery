"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ChevronDown } from "lucide-react";

import {
  ADMIN_CONTEXT_LINKS,
  ADMIN_LINK_MODULE,
  findAdminNavLink,
  pathnameMatches,
} from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

export default function PageContextNav() {
  const pathname = usePathname();
  const t = useT();
  const { has, isAdmin } = usePermissions();
  const current = findAdminNavLink(pathname);
  if (!current) return null;

  const links = ADMIN_CONTEXT_LINKS[current.context].filter((item) => {
    if (item.adminOnly && !isAdmin) return false;
    const module = ADMIN_LINK_MODULE[item.href];
    return !module || has(module);
  });
  if (links.length <= 1) return null;

  const active = links.find((item) => pathnameMatches(pathname, item.href)) || current;

  return (
    <div className="mt-4 border-t border-border pt-3">
      <nav aria-label={t("nav.secondary")} className="hidden min-w-0 items-center gap-1 md:flex">
        {links.map((item) => {
          const selected = pathnameMatches(pathname, item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              aria-current={selected ? "page" : undefined}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                selected
                  ? "bg-accent-subtle text-accent"
                  : "text-muted hover:bg-subtle hover:text-fg"
              }`}
            >
              {t(item.labelKey)}
            </Link>
          );
        })}
      </nav>
      <details className="group relative md:hidden">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between rounded-lg border border-border bg-surface px-3 text-sm font-medium text-fg [&::-webkit-details-marker]:hidden">
          <span className="truncate">{t(active.labelKey)}</span>
          <ChevronDown className="h-4 w-4 shrink-0 text-muted transition-transform group-open:rotate-180" strokeWidth={1.8} aria-hidden />
        </summary>
        <nav
          aria-label={t("nav.secondary")}
          className="absolute left-0 right-0 z-30 mt-1 overflow-hidden rounded-lg border border-border bg-surface p-1 shadow-overlay"
        >
          {links.map((item) => {
            const selected = pathnameMatches(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={selected ? "page" : undefined}
                className={`flex min-h-11 items-center rounded-md px-3 text-sm ${
                  selected ? "bg-accent-subtle font-medium text-accent" : "text-muted hover:bg-subtle hover:text-fg"
                }`}
              >
                {t(item.labelKey)}
              </Link>
            );
          })}
        </nav>
      </details>
    </div>
  );
}
