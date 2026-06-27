import { ReactNode } from "react";

export default function EmptyState({ icon, title, description, action }: { icon?: string; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-border bg-white py-10 text-center dark:border-border dark:bg-surface">
      {icon && <div className="mb-3 text-3xl">{icon}</div>}
      <h3 className="text-base font-semibold text-fg">{title}</h3>
      {description && <p className="mx-auto mt-1 max-w-md text-sm text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
