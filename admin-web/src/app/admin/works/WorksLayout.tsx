import type { ReactNode } from "react";

import type { WorkCardSize, WorksViewMode } from "@/lib/appearance";

const gridClasses: Record<WorkCardSize, string> = {
  small: "grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-7 xl:grid-cols-8",
  medium: "grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6",
  large: "grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5",
};

const masonryClasses: Record<WorkCardSize, string> = {
  small: "columns-2 sm:columns-3 md:columns-5 lg:columns-7 xl:columns-8",
  medium: "columns-2 sm:columns-3 md:columns-4 lg:columns-5 xl:columns-6",
  large: "columns-1 sm:columns-2 md:columns-3 lg:columns-4 xl:columns-5",
};

export function WorksLayout({
  mode,
  size,
  children,
}: {
  mode: WorksViewMode;
  size: WorkCardSize;
  children: ReactNode;
}) {
  if (mode === "list") {
    return <div data-works-layout="list" data-card-size={size} className="mb-6 space-y-2">{children}</div>;
  }
  if (mode === "masonry") {
    return (
      <div data-works-layout="masonry" data-card-size={size} className={`mb-6 gap-4 ${masonryClasses[size]} [&>[data-work-card]]:mb-4`}>
        {children}
      </div>
    );
  }
  return (
    <div data-works-layout="grid" data-card-size={size} className={`mb-6 grid gap-4 ${gridClasses[size]}`}>
      {children}
    </div>
  );
}

export function WorksLayoutSkeleton({ mode, size }: { mode: WorksViewMode; size: WorkCardSize }) {
  if (mode === "list") {
    return <div className="space-y-2">{Array.from({ length: 8 }).map((_, index) => <div key={index} className="h-16 animate-pulse rounded bg-subtle" />)}</div>;
  }
  return (
    <WorksLayout mode={mode} size={size}>
      {Array.from({ length: 10 }).map((_, index) => (
        <div key={index} className="mb-4 break-inside-avoid rounded-md bg-surface p-3 shadow-sm">
          <div className={`${mode === "masonry" && index % 3 === 0 ? "aspect-[3/4]" : "aspect-[4/3]"} mb-2 animate-pulse rounded bg-subtle`} />
          <div className="h-3 w-3/4 animate-pulse rounded bg-subtle" />
        </div>
      ))}
    </WorksLayout>
  );
}
