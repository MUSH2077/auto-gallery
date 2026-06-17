import { ReactNode } from "react";

export default function EmptyState({ icon, title, description, action }: { icon?: string; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-ag-border bg-white py-10 text-center dark:border-ag-border dark:bg-ag-surface">
      {icon && <div className="mb-3 text-3xl">{icon}</div>}
      <h3 className="text-base font-semibold text-[#24292f] dark:text-ag-text">{title}</h3>
      {description && <p className="mx-auto mt-1 max-w-md text-sm text-[#57606a] dark:text-[#8b949e]">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
