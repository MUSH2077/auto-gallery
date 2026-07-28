import { type ReactNode } from "react";

export default function AdaptiveToolbar({
  leading,
  filters,
  meta,
  actions,
  className = "",
}: {
  leading?: ReactNode;
  filters?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`toolbar min-w-0 ${className}`}>
      {leading && <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">{leading}</div>}
      {filters && <div className="flex min-w-0 flex-wrap items-center gap-2">{filters}</div>}
      {meta && <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-muted">{meta}</div>}
      {actions && <div className="flex min-w-0 flex-wrap items-center gap-2 sm:ml-auto">{actions}</div>}
    </div>
  );
}
