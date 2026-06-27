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
    <div className={`sticky top-2 z-20 mb-4 flex flex-wrap items-center gap-3 rounded-md border border-border bg-white px-4 py-2 shadow-sm dark:border-border dark:bg-surface ${className}`}>
      <span className="text-sm font-medium text-fg">{label || `${count} selected`}</span>
      {onClear && <button onClick={onClear} className="btn-ghost text-xs">{clearLabel}</button>}
      <span className="min-w-4 flex-1" />
      {children}
    </div>
  );
}
