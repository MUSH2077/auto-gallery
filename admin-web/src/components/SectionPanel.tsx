import { type ReactNode } from "react";

export default function SectionPanel({
  title,
  description,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card overflow-hidden ${className}`}>
      {(title || description || actions) && (
        <div className="flex flex-col gap-2 border-b border-[#d8dee4] px-4 py-3 dark:border-[#30363d] sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-semibold text-[#24292f] dark:text-[#e6edf3]">{title}</h2>}
            {description && <div className="mt-1 text-xs text-[#57606a] dark:text-[#8b949e]">{description}</div>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}
