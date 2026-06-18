import { type ReactNode } from "react";

export default function FilterBar({
  children,
  meta,
  className = "",
}: {
  children: ReactNode;
  meta?: ReactNode;
  className?: string;
}) {
  return (
    <div className={`toolbar mb-4 ${className}`}>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">{children}</div>
      {meta && <div className="flex shrink-0 flex-wrap items-center gap-2 text-xs text-[#57606a] dark:text-[#8b949e]">{meta}</div>}
    </div>
  );
}
