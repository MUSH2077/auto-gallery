"use client";

// The permanent DOM renderer for the showcase pointer trail — the fallback
// path for reduced-motion and low-end devices (Task 4), and the baseline the
// WebGL layer (Task 5) renders on top of. Gated at the source by
// `motionConfig.shouldAnimate()`: when it is false we render a static grid
// and never touch rAF or pointer listeners at all (see
// docs/frontend-motion-audit.md — the project holds a zero-long-task
// baseline and this page must not break it).
//
// This component only ever mounts client-side after the showcase sample
// query resolves (see app/page.tsx), so there is no SSR/hydration pass to
// keep in sync — `motionConfig.shouldAnimate()` can be read directly.

import { useEffect, useRef, useState } from "react";

import type { ShowcaseItem } from "@/lib/api";
import { createTrail, type TrailController, type TrailItem } from "@/lib/showcase/trail";
import { motionConfig, motionTokens } from "@/lib/motion";
import type { ShowcaseConfig } from "@/lib/showcase/config";

type TrailConfig = Pick<ShowcaseConfig, "trailMax" | "spawnIntervalMs" | "followDamping" | "parallaxStrength">;

// Brief-specified floor: `lifetimeMs = trailMax * spawnIntervalMs`, clamped to
// at least 900ms so a small trailMax / fast spawnInterval combination never
// produces a flash-cut trail.
const MIN_LIFETIME_MS = 900;
// Fade-in duration reuses the shared "fast" token (120ms) rather than a new
// hardcoded value.
const FADE_IN_MS = motionTokens.duration.fast;
// Portion of the post-fade-in span held at full opacity/scale before the
// fade-out begins (item-age curve, not a duration/easing token).
const HOLD_FRACTION = 0.3;
const POP_SCALE_START = 0.7;
const FADE_OUT_SCALE_END = 0.85;
const STATIC_GRID_COUNT = 8;

function computeLifetimeMs(config: TrailConfig): number {
  return Math.max(MIN_LIFETIME_MS, config.trailMax * config.spawnIntervalMs);
}

/** Scale/opacity purely as a function of item age: fade+pop in, hold, fade+shrink out. */
function ageStyle(age: number, lifetimeMs: number): { opacity: number; scale: number } {
  const fadeInEnd = Math.min(FADE_IN_MS, lifetimeMs);
  const holdEnd = fadeInEnd + (lifetimeMs - fadeInEnd) * HOLD_FRACTION;
  if (age <= fadeInEnd) {
    const p = fadeInEnd > 0 ? age / fadeInEnd : 1;
    return { opacity: p, scale: POP_SCALE_START + (1 - POP_SCALE_START) * p };
  }
  if (age <= holdEnd) {
    return { opacity: 1, scale: 1 };
  }
  const p = holdEnd < lifetimeMs ? (age - holdEnd) / (lifetimeMs - holdEnd) : 1;
  return { opacity: Math.max(0, 1 - p), scale: 1 - (1 - FADE_OUT_SCALE_END) * p };
}

export default function ShowcaseTrailDOM({ items, config }: { items: ShowcaseItem[]; config: TrailConfig }) {
  // Computed once — a device/OS property, not something that should flip
  // mid-session and re-decide the render path.
  const [animate] = useState(() => motionConfig.shouldAnimate());

  const containerRef = useRef<HTMLDivElement>(null);
  const poolRef = useRef<(HTMLImageElement | null)[]>([]);
  const lastSrcRef = useRef<string[]>([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => {
    if (!animate) return; // reduced-motion / low-end: no rAF, no pointer listener, no controller.
    const container = containerRef.current;
    if (!container) return;

    const lifetimeMs = computeLifetimeMs(config);
    const controller: TrailController = createTrail({
      max: config.trailMax,
      spawnIntervalMs: config.spawnIntervalMs,
      lifetimeMs,
      imageCount: itemsRef.current.length,
    });

    const raw = { x: 0, y: 0 };
    const damped = { x: 0, y: 0 };
    let rafId = 0;
    let running = true;

    function onPointerMove(e: PointerEvent) {
      const rect = container!.getBoundingClientRect();
      raw.x = e.clientX - rect.left;
      raw.y = e.clientY - rect.top;
      controller.pointerMove(raw.x, raw.y);
    }

    function paint(now: number) {
      if (!running) return;

      // followDamping lerps a virtual "rendered pointer" toward the raw
      // pointer each frame; parallaxStrength offsets every live item by a
      // fraction of the gap between the two, so the trail layer reads as
      // sitting slightly behind the cursor instead of glued to it.
      damped.x += (raw.x - damped.x) * config.followDamping;
      damped.y += (raw.y - damped.y) * config.followDamping;
      const parallaxDX = (raw.x - damped.x) * config.parallaxStrength;
      const parallaxDY = (raw.y - damped.y) * config.parallaxStrength;

      const live = controller.tick(now);
      const pool = poolRef.current;
      const currentItems = itemsRef.current;

      for (let i = 0; i < pool.length; i++) {
        const img = pool[i];
        if (!img) continue;
        const trailItem: TrailItem | undefined = live[i];
        const source = trailItem && currentItems.length > 0 ? currentItems[trailItem.imageIndex % currentItems.length] : undefined;
        if (!trailItem || !source) {
          if (img.style.opacity !== "0") img.style.opacity = "0";
          continue;
        }

        if (lastSrcRef.current[i] !== source.thumb_url) {
          img.src = source.thumb_url;
          lastSrcRef.current[i] = source.thumb_url;
          if (source.width) img.width = source.width;
          if (source.height) img.height = source.height;
        }

        const { opacity, scale } = ageStyle(now - trailItem.bornAt, lifetimeMs);
        const x = trailItem.x + parallaxDX;
        const y = trailItem.y + parallaxDY;
        img.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${scale})`;
        img.style.opacity = String(opacity);
      }

      rafId = requestAnimationFrame(paint);
    }

    function onVisibility() {
      if (document.hidden) {
        running = false;
        cancelAnimationFrame(rafId);
      } else if (!running) {
        running = true;
        rafId = requestAnimationFrame(paint);
      }
    }

    container.addEventListener("pointermove", onPointerMove);
    document.addEventListener("visibilitychange", onVisibility);
    if (!document.hidden) rafId = requestAnimationFrame(paint);

    return () => {
      running = false;
      cancelAnimationFrame(rafId);
      container.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("visibilitychange", onVisibility);
      controller.reset();
    };
    // config fields are primitive numbers from a sanitized, range-clamped
    // source (useShowcaseConfig) — including them keeps the loop honest if
    // the user tweaks settings in another tab without over-firing on
    // `items` (tracked via itemsRef instead, so a background refetch never
    // restarts the pointer listener).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [animate, config.trailMax, config.spawnIntervalMs, config.followDamping, config.parallaxStrength]);

  if (!animate) {
    const gridItems = items.slice(0, STATIC_GRID_COUNT);
    return (
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <div className="grid grid-cols-4 gap-4 p-6">
          {gridItems.map((item) => (
            <img
              key={item.work_id}
              src={item.thumb_url}
              width={item.width ?? undefined}
              height={item.height ?? undefined}
              alt=""
              aria-hidden="true"
              decoding="async"
              className="aspect-square h-20 w-20 rounded-md object-cover opacity-80 sm:h-28 sm:w-28"
            />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="absolute inset-0 overflow-hidden">
      {Array.from({ length: config.trailMax }).map((_, i) => (
        <img
          key={i}
          ref={(el) => {
            poolRef.current[i] = el;
          }}
          alt=""
          aria-hidden="true"
          decoding="async"
          className="pointer-events-none absolute left-0 top-0 h-20 w-20 rounded-md object-cover opacity-0 sm:h-28 sm:w-28"
          style={{ willChange: "transform, opacity" }}
        />
      ))}
    </div>
  );
}
