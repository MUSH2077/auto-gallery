"use client";

// The permanent DOM renderer for the showcase pointer trail — the fallback
// path for reduced-motion and low-end devices (Task 4), and the baseline the
// WebGL layer (Task 5) renders on top of. Gated at the source by
// `motionConfig.shouldAnimate()` OR the user's `minimal` preference (Task 3's
// "极简模式" toggle — see showcase_settings.minimal_hint, which promises the
// motion parameters "no longer take effect" once it's on): when either is
// true we render a static grid and never touch rAF or pointer listeners at
// all (see docs/frontend-motion-audit.md — the project holds a zero-long-task
// baseline and this page must not break it).
//
// This component only ever mounts client-side after the showcase sample
// query resolves (see app/page.tsx), so there is no SSR/hydration pass to
// keep in sync — `motionConfig.shouldAnimate()` can be read directly.

import { useEffect, useRef, useState } from "react";

import type { ShowcaseItem } from "@/lib/api";
import { createTrail, type TrailController, type TrailItem } from "@/lib/showcase/trail";
import { motionConfig, motionTokens } from "@/lib/motion";
import {
  computeLifetimeMs,
  frameIndependentAlpha,
  isPointerActive,
  MAX_DT_MS,
  type TrailConfig,
} from "@/lib/showcase/trailTiming";

// Fade-in duration reuses the shared "fast" token (120ms) rather than a new
// hardcoded value.
const FADE_IN_MS = motionTokens.duration.fast;
// Portion of the post-fade-in span held at full opacity/scale before the
// fade-out begins (item-age curve, not a duration/easing token).
const HOLD_FRACTION = 0.3;
const POP_SCALE_START = 0.7;
const FADE_OUT_SCALE_END = 0.85;
const STATIC_GRID_COUNT = 8;

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
  // `config.minimal` is a live user preference (can change via the settings
  // page in another tab, propagated through useShowcaseConfig), so unlike
  // `animate` it is recomputed every render rather than frozen at mount.
  // Both conditions are combined into one boolean so the rAF-gating effect
  // and the static/animated render branch can never disagree about which
  // path is active.
  const showStatic = !animate || config.minimal;

  const containerRef = useRef<HTMLDivElement>(null);
  const poolRef = useRef<(HTMLImageElement | null)[]>([]);
  const lastSrcRef = useRef<string[]>([]);
  const itemsRef = useRef(items);
  itemsRef.current = items;

  useEffect(() => {
    if (showStatic) return; // reduced-motion / low-end / minimal: no rAF, no pointer listener, no controller.
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
    // No pointer input is currently active — must not spawn trail items at
    // the (0,0) default before the user actually moves the mouse, and must
    // stop again once the pointer leaves the container/window (see
    // onPointerLeave below). Distinct from the motion-recency/convergence
    // gate in paint(): this is "is there a pointer here at all", that gate
    // is "is it still moving".
    let hasPointer = false;
    // Wall-clock timestamp of the most recent REAL `pointermove` event (not
    // a painted frame) — feeds the MOVE_RECENCY_WINDOW_MS check in paint().
    let lastRealMoveTime = -Infinity;
    // Wall-clock timestamp of the previous painted frame; null means "no
    // previous frame to diff against" (first frame after mount, or the
    // first frame after resuming from document.hidden).
    let lastFrameTime: number | null = null;

    function onPointerMove(e: PointerEvent) {
      const rect = container!.getBoundingClientRect();
      raw.x = e.clientX - rect.left;
      raw.y = e.clientY - rect.top;
      lastRealMoveTime = performance.now();
      if (!hasPointer) {
        // Snap instead of lerping from (0,0) on the very first move (or the
        // first move after re-entering following a pointerleave), so the
        // trail starts exactly at the cursor rather than visibly crawling
        // in from the previous position or the top-left corner.
        hasPointer = true;
        damped.x = raw.x;
        damped.y = raw.y;
      }
    }

    function onPointerLeave() {
      // Pointer left the container (including leaving the browser window
      // entirely — pointerleave fires on the last-hovered element in that
      // case too): stop spawning immediately rather than riding out the
      // smoothing tail, since there is no longer a real cursor position to
      // trail toward. Re-entry re-triggers the snap-to-cursor path above.
      hasPointer = false;
    }

    function paint(now: number) {
      if (!running) return;

      const dt = lastFrameTime === null ? 0 : Math.min(now - lastFrameTime, MAX_DT_MS);
      lastFrameTime = now;

      // followDamping smooths the pointer position used FOR SPAWNING, not
      // the position of items already spawned: `damped` chases `raw` with
      // frame-rate-independent exponential smoothing (see
      // frameIndependentAlpha above), and it is `damped` — not `raw` — that
      // gets fed to the trail controller below. That makes images spawn
      // along a slightly-lagging, smoothed version of the pointer's path,
      // matching the makemepulse-style reference this feature is modeled
      // on. Once a trailItem is created its (x, y) never move again — only
      // its age-driven opacity/scale change (see ageStyle) — until it's
      // culled by the controller's lifetime check.
      const alpha = frameIndependentAlpha(config.followDamping, dt);
      damped.x += (raw.x - damped.x) * alpha;
      damped.y += (raw.y - damped.y) * alpha;
      if (hasPointer) {
        // Forward only while genuinely in motion (see isPointerActive in
        // trailTiming.ts) — otherwise a pointer that has stopped (or left,
        // via onPointerLeave clearing hasPointer) would spawn a fresh item
        // at the same frozen coordinate every spawnIntervalMs forever,
        // instead of quietly fading to nothing.
        if (isPointerActive(now, lastRealMoveTime, raw, damped)) controller.pointerMove(damped.x, damped.y);
      }

      // parallaxStrength stays a small *shared* depth offset — the gap
      // between the raw cursor and the smoothed spawn point, scaled down —
      // applied uniformly to every live item's rendered position each
      // frame, so the whole trail layer reads as sitting slightly behind
      // the cursor. It is a pure function of two current-frame positions
      // (no integration of its own), so it is already frame-rate
      // independent without any additional dt term.
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
        // Drop the frame-time anchor so the dt computed on resume reads as
        // "no previous frame" (dt=0) instead of "however long the tab was
        // backgrounded" — belt-and-suspenders alongside the MAX_DT_MS clamp.
        lastFrameTime = null;
      } else if (!running) {
        running = true;
        rafId = requestAnimationFrame(paint);
      }
    }

    container.addEventListener("pointermove", onPointerMove);
    container.addEventListener("pointerleave", onPointerLeave);
    document.addEventListener("visibilitychange", onVisibility);
    if (!document.hidden) rafId = requestAnimationFrame(paint);

    return () => {
      running = false;
      cancelAnimationFrame(rafId);
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerleave", onPointerLeave);
      document.removeEventListener("visibilitychange", onVisibility);
      controller.reset();
    };
    // config fields are primitive numbers from a sanitized, range-clamped
    // source (useShowcaseConfig) — including them keeps the loop honest if
    // the user tweaks settings in another tab without over-firing on
    // `items` (tracked via itemsRef instead, so a background refetch never
    // restarts the pointer listener). `showStatic` (not just `animate`)
    // drives re-evaluation so toggling `config.minimal` off mid-session
    // starts the loop, and toggling it on tears it down, exactly like the
    // reduced-motion/low-end path already did.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showStatic, config.trailMax, config.spawnIntervalMs, config.followDamping, config.parallaxStrength]);

  if (showStatic) {
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
