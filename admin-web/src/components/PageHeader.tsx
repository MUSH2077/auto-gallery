import { ReactNode } from "react";

export default function PageHeader({ title, description, children }: { title: string; description?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-8">
      <div>
        <h1 className="text-2xl md:text-3xl font-bold text-stone-900 dark:text-stone-100 tracking-tight"
          style={{ fontFamily: "'Playfair Display', Georgia, serif" }}>
          {title}
        </h1>
        {description && <div className="text-sm text-stone-500 dark:text-stone-400 mt-1.5">{description}</div>}
      </div>
      {children && <div className="flex gap-2 shrink-0">{children}</div>}
    </div>
  );
}
