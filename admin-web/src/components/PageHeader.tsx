import { ReactNode } from "react";

export default function PageHeader({ title, description, children }: { title: string; description?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h1 className="text-2xl font-bold dark:text-white">{title}</h1>
        {description && <div className="text-sm text-gray-500 dark:text-gray-400 mt-1">{description}</div>}
      </div>
      {children && <div className="flex gap-2">{children}</div>}
    </div>
  );
}
