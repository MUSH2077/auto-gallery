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
import type { ShowcaseConfig } from "@/lib/showcase/config";

type TrailConfig = Pick<
  ShowcaseConfig,
  "trailMax" | "spawnIntervalMs" | "followDamping" | "parallaxStrength" | "minimal"
>;

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

// `followDamping` was authored (and its default/range tuned) against the old
// per-callback lerp — `damped += (raw - damped) * followDamping`, invoked
// once per rAF frame with no time term — which implicitly means "close this
// fraction of the gap in one 60Hz frame's worth of time." REFERENCE_FRAME_MS
// is that assumed frame time; frameIndependentAlpha() below converts the
// per-frame fraction into a continuous rate so the *wall-clock* convergence
// speed stays the same regardless of the display's actual refresh rate.
const REFERENCE_FRAME_MS = 1000 / 60;
// Clamp applied to the rAF delta before it feeds the smoothing formula, so a
// backgrounded tab returning to the foreground (or any other multi-frame
// stall) can't be read as "one giant frame" and snap the trail across the
// screen. Defense-in-depth alongside resetting lastFrameTime on hide (see
// onVisibility below).
const MAX_DT_MS = 100;

function computeLifetimeMs(config: TrailConfig): number {
  return Math.max(MIN_LIFETIME_MS, config.trailMax * config.spawnIntervalMs);
}

/**
 * Frame-rate-independent exponential smoothing factor.
 *
 * Given `perFrameFactor` — the fraction of the remaining gap that should
 * close in one REFERENCE_FRAME_MS-long frame — returns the equivalent
 * fraction that closes in `dtMs` of actual elapsed time:
 *
 *   alpha(dt) = 1 - (1 - perFrameFactor) ^ (dt / REFERENCE_FRAME_MS)
 *
 * At dt === REFERENCE_FRAME_MS this reduces exactly to `perFrameFactor`, so
 * existing tuned defaults keep their 60Hz feel; at any other refresh rate
 * (144Hz, a throttled background tab, …) the same wall-clock convergence
 * time is preserved instead of scaling with frame count. This is the
 * formula Task 5's WebGL renderer must copy verbatim to keep both trail
 * implementations feeling identical.
 */
function frameIndependentAlpha(perFrameFactor: number, dtMs: number): number {
  return 1 - Math.pow(1 - perFrameFactor, dtMs / REFERENCE_FRAME_MS);
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
    // No pointer input has arrived yet — must not spawn trail items at the
    // (0,0) default before the user actually moves the mouse.
    let hasPointer = false;
    // Wall-clock timestamp of the previous painted frame; null means "no
    // previous frame to diff against" (first frame after mount, or the
    // first frame after resuming from document.hidden).
    let lastFrameTime: number | null = null;

    function onPointerMove(e: PointerEvent) {
      const rect = container!.getBoundingClientRect();
      raw.x = e.clientX - rect.left;
      raw.y = e.clientY - rect.top;
      if (!hasPointer) {
        // Snap instead of lerping from (0,0) on the very first move, so the
        // trail starts exactly at the cursor rather than visibly crawling
        // in from the top-left corner.
        hasPointer = true;
        damped.x = raw.x;
        damped.y = raw.y;
      }
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
      if (hasPointer) controller.pointerMove(damped.x, damped.y);

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
