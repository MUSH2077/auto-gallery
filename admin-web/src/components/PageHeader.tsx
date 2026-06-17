import { ReactNode } from "react";

export default function PageHeader({ title, description, children }: { title: string; description?: ReactNode; children?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-3 border-b border-[#d8dee4] pb-4 dark:border-[#30363d] sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="truncate text-2xl font-semibold tracking-normal text-[#24292f] dark:text-[#e6edf3]">
          {title}
        </h1>
        {description && <div className="text-sm text-[#57606a] dark:text-[#8b949e] mt-1.5">{description}</div>}
      </div>
      {children && <div className="flex flex-wrap gap-2 shrink-0">{children}</div>}
    </div>
  );
}
