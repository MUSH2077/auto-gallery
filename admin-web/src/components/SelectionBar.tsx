import { type ReactNode } from "react";

export default function SelectionBar({
  count,
  children,
  onClear,
  clearLabel = "Clear",
  label,
  className = "",
}: {
  count: number;
  children?: ReactNode;
  onClear?: () => void;
  clearLabel?: string;
  label?: ReactNode;
  className?: string;
}) {
  if (count <= 0) return null;
  return (
    <div className={`sticky bottom-2 top-auto z-20 mb-4 flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-border bg-surface/95 px-3 py-2 shadow-overlay backdrop-blur sm:bottom-auto sm:top-16 sm:gap-3 sm:px-4 ${className}`}>
      <span className="text-sm font-medium text-fg">{label || `${count} selected`}</span>
      {onClear && <button onClick={onClear} className="btn-ghost text-xs">{clearLabel}</button>}
      <span className="min-w-4 flex-1" />
      {children}
    </div>
  );
}
