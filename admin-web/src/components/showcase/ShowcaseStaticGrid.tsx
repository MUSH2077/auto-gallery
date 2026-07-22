"use client";
import type { ShowcaseItem } from "@/lib/api";

const GRID_COUNT = 8;

/** Reduced-motion / minimal fallback: a still grid of large aspect-correct images. No rAF, no pointer, no WebGL. */
export default function ShowcaseStaticGrid({ items }: { items: ShowcaseItem[] }) {
  const shown = items.slice(0, GRID_COUNT);
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-8">
      <div className="grid max-w-5xl grid-cols-2 gap-3 sm:grid-cols-4">
        {shown.map((it) => (
          <img
            key={it.work_id}
            src={it.thumb_url}
            alt=""
            aria-hidden="true"
            width={it.width ?? undefined}
            height={it.height ?? undefined}
            className="h-auto w-full rounded-lg object-cover"
            decoding="async"
          />
        ))}
      </div>
    </div>
  );
}
