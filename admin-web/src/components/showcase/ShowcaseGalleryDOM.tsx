"use client";

import { useEffect, useState } from "react";

import type { ShowcaseItem } from "@/lib/api";
import type { GalleryCanvasConfig } from "./ShowcaseCanvas";

/**
 * Low-end / WebGL-unavailable fallback: a CSS translateX drift band.
 * Transform-only, GPU-composited, no WebGL and no rAF. Each copy owns its
 * trailing gap so translating the two-copy track by 50% loops without a seam.
 */
export default function ShowcaseGalleryDOM({
  items,
  config,
}: {
  items: ShowcaseItem[];
  config: GalleryCanvasConfig;
}) {
  const [paused, setPaused] = useState(false);
  const durationS = Math.max(20, 120 / Math.max(0.2, config.autoScrollSpeed));

  useEffect(() => {
    function syncVisibility() {
      setPaused(document.hidden);
    }

    syncVisibility();
    document.addEventListener("visibilitychange", syncVisibility);
    return () => document.removeEventListener("visibilitychange", syncVisibility);
  }, []);

  return (
    <div className="pointer-events-none absolute inset-0 flex items-center overflow-hidden">
      <div
        className="gallery-drift flex w-max flex-nowrap will-change-transform"
        style={{
          ["--drift-duration" as string]: `${durationS}s`,
          height: `${config.planeHeightVh}vh`,
          animationPlayState: paused ? "paused" : "running",
        }}
      >
        {[0, 1].map((copy) => (
          <div
            key={copy}
            aria-hidden="true"
            className="flex h-full shrink-0 flex-nowrap gap-6 pr-6"
          >
            {items.map((item, index) => (
              <img
                key={`${item.work_id}-${item.asset_id}-${copy}-${index}`}
                src={item.thumb_url}
                alt=""
                width={item.width ?? undefined}
                height={item.height ?? undefined}
                className="h-full w-auto max-w-[70vw] rounded-lg object-cover"
                decoding="async"
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
