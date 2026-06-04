import { ReactNode } from "react";

export default function PageHeader({ title, description, children }: { title: string; description?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-8">
      <div>
        <h1 className="text-2xl md:text-3xl font-semibold text-[#24292f] dark:text-[#e6edf3] tracking-normal">
          {title}
        </h1>
        {description && <div className="text-sm text-[#57606a] dark:text-[#8b949e] mt-1.5">{description}</div>}
      </div>
      {children && <div className="flex gap-2 shrink-0">{children}</div>}
    </div>
  );
}
