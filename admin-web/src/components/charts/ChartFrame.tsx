"use client";

import type { ReactNode } from "react";

export default function ChartFrame({
  title,
  insight,
  description,
  footer,
  actions,
  children,
  className = "",
  testId,
}: {
  title: string;
  insight?: string;
  description?: string;
  footer?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
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

      {footer ? (
        <footer className="min-w-0 max-w-full overflow-hidden border-t border-border px-4 py-3 sm:px-5">
          <div className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">{footer}</div>
        </footer>
      ) : null}
    </section>
  );
}
