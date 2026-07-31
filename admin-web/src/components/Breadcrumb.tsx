"use client";
import Link from "next/link";
import { useT } from "@/lib/i18n";

export type Crumb = { label: string; href?: string };

export function Breadcrumb({ items, className = "mb-4" }: { items: Crumb[]; className?: string }) {
  const t = useT();
  return (
    <nav
      aria-label={t("common.breadcrumb")}
      className={`flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-muted ${className}`}
    >
      {items.map((item, i) => (
        <span key={`${item.href || "current"}-${item.label}-${i}`} className="flex min-w-0 items-center gap-1.5">
          {i > 0 && <span className="text-muted" aria-hidden="true">/</span>}
          {item.href ? (
            <Link
              href={item.href}
              className="max-w-48 truncate rounded-sm transition-colors hover:text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
            >
              {item.label}
            </Link>
          ) : (
            <span aria-current="page" className="max-w-64 truncate font-medium text-fg">{item.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
