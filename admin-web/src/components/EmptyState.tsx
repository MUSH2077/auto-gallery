import { ReactNode } from "react";

export default function EmptyState({ icon, title, description, action }: { icon?: string; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="text-center py-12">
      {icon && <div className="text-4xl mb-3">{icon}</div>}
      <h3 className="text-lg font-medium text-gray-700 dark:text-gray-300">{title}</h3>
      {description && <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
