"use client";
import Link from "next/link";
import { useT } from "@/lib/i18n";

export type Crumb = { label: string; href?: string };

export function Breadcrumb({ items }: { items: Crumb[] }) {
  const t = useT();
  return (
    <nav aria-label={t("common.breadcrumb")} className="flex items-center gap-1.5 text-sm text-muted mb-4">
      {items.map((item, i) => (
        <span key={i} className="flex items-center gap-1.5">
          {i > 0 && <span className="text-muted" aria-hidden="true">/</span>}
          {item.href ? (
            <Link href={item.href} className="hover:text-blue-600 dark:hover:text-blue-400 hover:underline transition-colors">
              {item.label}
            </Link>
          ) : (
            <span className="text-fg dark:text-white font-medium">{item.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}
