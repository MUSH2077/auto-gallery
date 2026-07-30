"use client";

import type { ReactNode } from "react";

import { useT } from "@/lib/i18n";

import type { ChartTableModel } from "./types";

function ChartDataTable({ model }: { model: ChartTableModel }) {
  return (
    <div
      className="mt-3 w-full max-w-full rounded-md border border-border"
      role="region"
      aria-label={model.caption}
      style={{ contain: "inline-size" }}
    >
      <table className="hidden w-full table-fixed border-collapse text-sm sm:table">
        <caption className="sr-only">{model.caption}</caption>
        <thead className="bg-subtle text-xs text-muted">
          <tr>
            {model.columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`px-3 py-2 font-semibold ${column.align === "right" ? "text-right" : "text-left"}`}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {model.rows.map((row) => (
            <tr key={row.id}>
              {model.columns.map((column) => (
                <td
                  key={column.key}
                  className={`break-words px-3 py-2 ${column.align === "right" ? "text-right tabular-nums" : "text-left"}`}
                >
                  {row.cells[column.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="divide-y divide-border sm:hidden">
        {model.rows.map((row) => (
          <dl key={row.id} className="space-y-2 px-3 py-3">
            {model.columns.map((column) => (
              <div key={column.key} className="grid grid-cols-[minmax(5rem,0.42fr)_minmax(0,1fr)] gap-3">
                <dt className="text-xs font-semibold text-muted">{column.label}</dt>
                <dd className={`min-w-0 break-words text-sm text-fg ${column.align === "right" ? "tabular-nums" : ""}`}>
                  {row.cells[column.key]}
                </dd>
              </div>
            ))}
          </dl>
        ))}
      </div>
    </div>
  );
}

export default function ChartFrame({
  title,
  insight,
  description,
  footer,
  actions,
  children,
  table,
  className = "",
  testId,
}: {
  title: string;
  insight?: string;
  description?: string;
  footer?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  table?: ChartTableModel;
  className?: string;
  testId?: string;
}) {
  const t = useT();
  return (
    <section
      className={`card min-w-0 max-w-full overflow-hidden ${className}`}
      data-chart-frame
      data-testid={testId}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-4 sm:px-5">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold text-fg">{title}</h2>
          {insight ? (
            <p className="mt-1 text-sm font-medium text-fg" aria-live="polite">{insight}</p>
          ) : null}
          {description ? <p className="mt-1 text-xs leading-5 text-muted">{description}</p> : null}
        </div>
        {actions ? <div className="flex min-h-11 shrink-0 items-center gap-2">{actions}</div> : null}
      </header>

      <div className="px-4 py-4 sm:px-5">{children}</div>

      {footer || table ? (
        <footer className="min-w-0 max-w-full overflow-hidden border-t border-border px-4 py-3 sm:px-5">
          {footer ? <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">{footer}</div> : null}
          {table ? (
            <details className="group mt-2 min-w-0 max-w-full overflow-hidden">
              <summary className="inline-flex min-h-11 cursor-pointer list-none items-center rounded-md px-2 text-sm font-medium text-accent hover:bg-accent-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus [&::-webkit-details-marker]:hidden">
                <span aria-hidden className="mr-1.5 transition-transform group-open:rotate-90">›</span>
                {t("charts.view_data")}
              </summary>
              <ChartDataTable model={table} />
            </details>
          ) : null}
        </footer>
      ) : null}
    </section>
  );
}
