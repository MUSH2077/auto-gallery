"use client";

// The WebGL enhancement layer for the showcase pointer trail, sitting on top
// of the permanent DOM renderer (ShowcaseTrailDOM, Task 4). This component
// owns the full degradation contract:
//
//   1. prefers-reduced-motion OR low-end (motionConfig.shouldAnimate() ===
//      false) -> render ShowcaseTrailDOM, and the `ogl`-loading renderer
//      factory (createShowcaseRenderer, in ./webgl) is never called, so the
//      `ogl` chunk is never fetched.
//   2. WebGL context creation fails, or `webglcontextlost` fires -> fall
//      back to ShowcaseTrailDOM silently (no error UI). One-way: WebGL can
//      fall back to DOM, never the reverse in the same session.
//   3. document.hidden -> pause the rAF loop (no rendering while hidden).
//   4. config.minimal -> also falls through to ShowcaseTrailDOM, which
//      already renders the static grid for `minimal` on its own — no second
//      minimal branch needed here.
//
// The WebGL-vs-DOM decision is made once on mount (`useWebGL`, a lazy
// useState initializer) and never re-evaluated for the same mount — flipping
// renderers under the user mid-session would be worse than either fixed
// choice. `fellBack` is the one-way escape hatch for rules 2's runtime
// failures.

import { useEffect, useRef, useState } from "react";

import type { ShowcaseItem } from "@/lib/api";
import { createTrail } from "@/lib/showcase/trail";
import { motionConfig } from "@/lib/motion";
import {
  computeLifetimeMs,
  frameIndependentAlpha,
  isPointerActive,
  MAX_DT_MS,
  type TrailConfig,
} from "@/lib/showcase/trailTiming";
import { createShowcaseRenderer, PreviewAuthExpiredError, type ShowcaseRenderer } from "@/lib/showcase/webgl";
import ShowcaseTrailDOM from "./ShowcaseTrailDOM";

// A stale signed-URL batch that keeps 401ing even after a refetch (clock
// skew, misconfiguration) must not turn into an unbounded refetch loop
// hammering the backend. Stop auto-refetching after this many consecutive
// expired batches; the streak resets the moment a batch loads cleanly.
const MAX_AUTO_REFETCH_STREAK = 2;

export default function ShowcaseCanvas({
  items,
  config,
  onPreviewExpired,
}: {
  items: ShowcaseItem[];
  config: TrailConfig;
  onPreviewExpired?: () => void;
}) {
  // Decided once on mount — a device/OS property plus the user's `minimal`
  // preference at mount time, not something that should flip mid-session.
  const [useWebGL] = useState(
    () => typeof window !== "undefined" && motionConfig.shouldAnimate() && !config.minimal,
  );
  const [fellBack, setFellBack] = useState(false);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<ShowcaseRenderer | null>(null);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const onPreviewExpiredRef = useRef(onPreviewExpired);
  onPreviewExpiredRef.current = onPreviewExpired;
  const expiredStreakRef = useRef(0);

  function reportSetImagesOutcome(expired: boolean): void {
    if (expired) {
      expiredStreakRef.current += 1;
      if (expiredStreakRef.current <= MAX_AUTO_REFETCH_STREAK) onPreviewExpiredRef.current?.();
    } else {
      expiredStreakRef.current = 0;
    }
  }

  // Renderer lifecycle: create (or degrade), wire up the rAF loop and
  // listeners, tear down on unmount/config change. Never re-entered when
  // `items` changes alone (see the sync effect below) — mirrors
  // ShowcaseTrailDOM's own itemsRef pattern so a background refetch never
  // restarts the pointer listener or the trail controller.
  useEffect(() => {
    if (!useWebGL || fellBack) return; // Never call the renderer factory outside this path — the ogl chunk must not be fetched.
    const canvas = canvasRef.current;
    if (!canvas) return;

    let cancelled = false;
    let renderer: ShowcaseRenderer | null = null;
    let rafId = 0;
    let running = true;
    let lastFrameTime: number | null = null;
    let hasPointer = false;
    let lastRealMoveTime = -Infinity;
    const raw = { x: 0, y: 0 };
    const damped = { x: 0, y: 0 };

    const lifetimeMs = computeLifetimeMs(config);
    const controller = createTrail({
      max: config.trailMax,
      spawnIntervalMs: config.spawnIntervalMs,
      lifetimeMs,
      imageCount: itemsRef.current.length,
    });

    function onPointerMove(e: PointerEvent) {
      const rect = canvas!.getBoundingClientRect();
      raw.x = e.clientX - rect.left;
      raw.y = e.clientY - rect.top;
      lastRealMoveTime = performance.now();
      if (!hasPointer) {
        hasPointer = true;
        damped.x = raw.x;
        damped.y = raw.y;
      }
    }

    function onPointerLeave() {
      hasPointer = false;
    }

    function onContextLost(e: Event) {
      // Runtime WebGL failure after a successful start: fall back silently,
      // one-way (never re-attempt WebGL for this mount).
      e.preventDefault();
      setFellBack(true);
    }

    function onResize() {
      renderer?.resize();
    }

    function paint(now: number) {
      if (!running || !renderer) return;

      const dt = lastFrameTime === null ? 0 : Math.min(now - lastFrameTime, MAX_DT_MS);
      lastFrameTime = now;

      const alpha = frameIndependentAlpha(config.followDamping, dt);
      damped.x += (raw.x - damped.x) * alpha;
      damped.y += (raw.y - damped.y) * alpha;

      if (hasPointer && isPointerActive(now, lastRealMoveTime, raw, damped)) {
        controller.pointerMove(damped.x, damped.y);
      }

      const live = controller.tick(now);
      renderer.render(live, { x: damped.x, y: damped.y });

      rafId = requestAnimationFrame(paint);
    }

    function onVisibility() {
      if (document.hidden) {
        running = false;
        cancelAnimationFrame(rafId);
        lastFrameTime = null;
      } else if (!running && renderer) {
        running = true;
        rafId = requestAnimationFrame(paint);
      }
    }

    // Registered before the first frame, per the contract — a context loss
    // at any point (including during setup) must degrade silently.
    canvas.addEventListener("webglcontextlost", onContextLost);

    void (async () => {
      let created: ShowcaseRenderer;
      try {
        created = await createShowcaseRenderer(canvas!, {
          maxTextures: config.trailMax * 2,
          parallaxStrength: config.parallaxStrength,
        });
      } catch {
        if (!cancelled) setFellBack(true);
        return;
      }
      if (cancelled) {
        created.destroy();
        return;
      }
      renderer = created;
      rendererRef.current = created;
      renderer.resize();

      try {
        await renderer.setImages(itemsRef.current.map((it) => it.preview_url));
        if (!cancelled) reportSetImagesOutcome(false);
      } catch (err) {
        if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError);
      }
      if (cancelled) return;

      canvas!.addEventListener("pointermove", onPointerMove);
      canvas!.addEventListener("pointerleave", onPointerLeave);
      window.addEventListener("resize", onResize);
      document.addEventListener("visibilitychange", onVisibility);
      if (!document.hidden) rafId = requestAnimationFrame(paint);
    })();

    return () => {
      cancelled = true;
      running = false;
      cancelAnimationFrame(rafId);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
      controller.reset();
      rendererRef.current = null;
      renderer?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useWebGL, fellBack, config.trailMax, config.spawnIntervalMs, config.followDamping, config.parallaxStrength]);

  // Texture sync: push a fresh URL list into the already-running renderer
  // whenever `items` changes (initial load is handled by the effect above;
  // this covers a later background refetch — including the one triggered by
  // a 401 above — without tearing down rAF/listeners). No-ops until the
  // renderer exists.
  useEffect(() => {
    if (!useWebGL || fellBack) return;
    const renderer = rendererRef.current;
    if (!renderer) return;
    let cancelled = false;
    renderer
      .setImages(items.map((it) => it.preview_url))
      .then(() => {
        if (!cancelled) reportSetImagesOutcome(false);
      })
      .catch((err) => {
        if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, useWebGL, fellBack]);

  if (!useWebGL || fellBack) {
    return <ShowcaseTrailDOM items={items} config={config} />;
  }

  return <canvas ref={canvasRef} aria-hidden="true" className="absolute inset-0 h-full w-full" />;
}
