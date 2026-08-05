"use client";

import { useT } from "@/lib/i18n";

export default function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  className = "",
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  className?: string;
}) {
  const t = useT();
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (total <= pageSize) return null;

  return (
    <nav
      className={`mt-6 flex items-center justify-between gap-3 ${className}`}
      aria-label={t("common.pagination")}
    >
      <button
        type="button"
        className="btn-ghost min-h-11 px-4 disabled:opacity-40"
        disabled={page <= 1}
        onClick={() => onPageChange(Math.max(1, page - 1))}
      >
        {t("common.prev")}
      </button>
      <span className="text-sm tabular-nums text-muted" aria-live="polite">
        {t("common.page_of", { page, total: totalPages })}
      </span>
      <button
        type="button"
        className="btn-ghost min-h-11 px-4 disabled:opacity-40"
        disabled={page >= totalPages}
        onClick={() => onPageChange(Math.min(totalPages, page + 1))}
      >
        {t("common.next")}
      </button>
    </nav>
  );
}
