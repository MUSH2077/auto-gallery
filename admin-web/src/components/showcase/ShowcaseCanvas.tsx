"use client";

// Gallery canvas: owns auto-scroll + wheel/drag, drives the WebGL renderer with
// a dt-independent scroll lerp, and the degradation contract. `hardwareOk`
// (reduced-motion/low-end) is frozen at mount; `config.minimal` is live;
// `fellBack` (runtime WebGL loss) is one-way. Reduced-motion/minimal use a
// static grid; low-end/WebGL-unavailable devices use the CSS drift band.

import { useEffect, useRef, useState } from "react";
import type { ShowcaseItem } from "@/lib/api";
import type { ShowcaseConfig } from "@/lib/showcase/config";
import { motionConfig } from "@/lib/motion";
import { frameIndependentAlpha, MAX_DT_MS } from "@/lib/showcase/smoothing";
import { createShowcaseRenderer, PreviewAuthExpiredError, type ShowcaseRenderer } from "@/lib/showcase/webgl";
import type { GalleryImage } from "@/lib/showcase/galleryLayout";
import ShowcaseGalleryDOM from "./ShowcaseGalleryDOM";
import ShowcaseStaticGrid from "./ShowcaseStaticGrid";

const MAX_AUTO_REFETCH_STREAK = 2;
const DRAG_PX_PER_UNIT = 1;
const WHEEL_SCALE = 0.6;
const SCROLL_EASE = 0.1;

export type GalleryCanvasConfig = Pick<
  ShowcaseConfig,
  "planeHeightVh" | "autoScrollSpeed" | "curveStrength" | "minimal"
>;

function toGalleryImages(items: ShowcaseItem[]): GalleryImage[] {
  return items.map((it) => ({ url: it.preview_url, width: it.width, height: it.height }));
}

export default function ShowcaseCanvas({
  items,
  config,
  onPreviewExpired,
  onHit,
}: {
  items: ShowcaseItem[];
  config: GalleryCanvasConfig;
  onPreviewExpired?: () => void;
  onHit?: (index: number) => void;
}) {
  const [hardwareOk] = useState(() => typeof window !== "undefined" && motionConfig.shouldAnimate());
  const useWebGL = hardwareOk && !config.minimal;
  const [fellBack, setFellBack] = useState(false);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<ShowcaseRenderer | null>(null);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const onPreviewExpiredRef = useRef(onPreviewExpired);
  onPreviewExpiredRef.current = onPreviewExpired;
  const onHitRef = useRef(onHit);
  onHitRef.current = onHit;
  const expiredStreakRef = useRef(0);

  function reportSetImagesOutcome(expired: boolean): void {
    if (expired) {
      expiredStreakRef.current += 1;
      if (expiredStreakRef.current <= MAX_AUTO_REFETCH_STREAK) onPreviewExpiredRef.current?.();
    } else {
      expiredStreakRef.current = 0;
    }
  }

  useEffect(() => {
    if (!useWebGL || fellBack) return;
    const canvas = canvasRef.current;
    if (!canvas) return;

    let cancelled = false;
    let renderer: ShowcaseRenderer | null = null;
    let rafId = 0;
    let running = true;
    let lastFrameTime: number | null = null;
    const scroll = { current: 0, target: 0, velocity: 0 };

    let dragging = false;
    let lastDragX = 0;
    function onPointerDown(e: PointerEvent) {
      dragging = true;
      lastDragX = e.clientX;
      canvas!.setPointerCapture(e.pointerId);
    }
    function onPointerMove(e: PointerEvent) {
      if (!dragging) return;
      scroll.target -= (e.clientX - lastDragX) * DRAG_PX_PER_UNIT;
      lastDragX = e.clientX;
    }
    function onPointerUp(e: PointerEvent) {
      dragging = false;
      try { canvas!.releasePointerCapture(e.pointerId); } catch {}
    }
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      scroll.target += (e.deltaY + e.deltaX) * WHEEL_SCALE;
    }
    function onContextLost(e: Event) { e.preventDefault(); setFellBack(true); }
    function onResize() { renderer?.resize(); }

    function paint(now: number) {
      if (!running || !renderer) return;
      const dt = lastFrameTime === null ? 0 : Math.min(now - lastFrameTime, MAX_DT_MS);
      lastFrameTime = now;
      scroll.target += config.autoScrollSpeed * (dt / (1000 / 60));
      const alpha = frameIndependentAlpha(SCROLL_EASE, dt);
      const prev = scroll.current;
      scroll.current += (scroll.target - scroll.current) * alpha;
      scroll.velocity = dt > 0 ? ((scroll.current - prev) / dt) * 1000 : 0;
      renderer.render({ current: scroll.current, velocity: scroll.velocity });
      rafId = requestAnimationFrame(paint);
    }
    function onVisibility() {
      if (document.hidden) { running = false; cancelAnimationFrame(rafId); lastFrameTime = null; }
      else if (!running && renderer) { running = true; rafId = requestAnimationFrame(paint); }
    }

    canvas.addEventListener("webglcontextlost", onContextLost);

    void (async () => {
      let created: ShowcaseRenderer;
      try {
        created = await createShowcaseRenderer(canvas!, {
          planeHeightVh: config.planeHeightVh,
          curveStrength: config.curveStrength,
          maxTextures: Math.max(24, itemsRef.current.length + 4),
        });
      } catch { if (!cancelled) setFellBack(true); return; }
      if (cancelled) { created.destroy(); return; }
      renderer = created;
      rendererRef.current = created;
      renderer.resize();
      try {
        await renderer.setImages(toGalleryImages(itemsRef.current));
        if (!cancelled) reportSetImagesOutcome(false);
      } catch (err) { if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError); }
      if (cancelled) return;
      canvas!.addEventListener("pointerdown", onPointerDown);
      canvas!.addEventListener("pointermove", onPointerMove);
      canvas!.addEventListener("pointerup", onPointerUp);
      canvas!.addEventListener("wheel", onWheel, { passive: false });
      window.addEventListener("resize", onResize);
      document.addEventListener("visibilitychange", onVisibility);
      if (!document.hidden) rafId = requestAnimationFrame(paint);
    })();

    return () => {
      cancelled = true;
      running = false;
      cancelAnimationFrame(rafId);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
      rendererRef.current = null;
      renderer?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useWebGL, fellBack, config.planeHeightVh, config.curveStrength, config.autoScrollSpeed]);

  useEffect(() => {
    if (!useWebGL || fellBack) return;
    const renderer = rendererRef.current;
    if (!renderer) return;
    let cancelled = false;
    renderer.setImages(toGalleryImages(items))
      .then(() => { if (!cancelled) reportSetImagesOutcome(false); })
      .catch((err) => { if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, useWebGL, fellBack]);

  if (!useWebGL || fellBack) {
    const reducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || config.minimal) {
      return <ShowcaseStaticGrid items={items} />;
    }
    return <ShowcaseGalleryDOM items={items} config={config} />;
  }
  return <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />;
}
