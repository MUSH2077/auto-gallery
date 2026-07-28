// The ONLY module allowed to touch "animejs". Business components go through
// these helpers so every JS animation shares the same tokens and the same
// reduced-motion / low-end gate, and a future library swap stays local.
//
// animejs is loaded via dynamic import: usePresence/tokens consumers (Modal,
// Toast, popovers — bundled into nearly every route through the components
// barrel) must not pay its bundle cost. The library chunk is only fetched the
// first time an actual JS animation runs, and never under reduced motion.
// Client-side only: call from effects/handlers in "use client" components.

import type { TargetsParam } from "animejs";

import { motionConfig } from "./config";
import { motionTokens } from "./tokens";

type AnimeModule = typeof import("animejs");

let animeModule: Promise<AnimeModule> | null = null;

function loadAnime(): Promise<AnimeModule> {
  animeModule ??= import("animejs");
  return animeModule;
}

/**
 * Staggered fade-in for large, dense target sets (SVG heatmap cells).
 * Opacity-only by design — hundreds of targets must not touch transform
 * or layout. No-op under reduced motion / low-end devices.
 */
export function enterHeatmap(targets: TargetsParam): void {
  if (!motionConfig.shouldAnimate()) return;
  void loadAnime().then(({ animate, stagger, utils }) => {
    utils.set(targets, { opacity: 0 });
    animate(targets, {
      opacity: 1,
      duration: motionTokens.duration.slow,
      delay: stagger(motionTokens.stagger.denseStep),
      ease: "linear",
      onComplete: () => utils.set(targets, { opacity: 1 }),
    });
  });
}

/**
 * Animate a numeric readout from one value to the next (dashboard metrics).
 * State feedback → `essential`, so it survives the low-end gate; reduced
 * motion still writes the final value instantly.
 */
export function countUp(
  el: HTMLElement,
  from: number,
  to: number,
  options: { duration?: number; format?: (v: number) => string } = {},
): void {
  const format = options.format ?? ((v: number) => String(Math.round(v)));
  if (!motionConfig.shouldAnimate({ essential: true })) {
    el.textContent = format(to);
    return;
  }
  void loadAnime().then(({ animate, cubicBezier }) => {
    const proxy = { v: from };
    animate(proxy, {
      v: to,
      duration: options.duration ?? motionTokens.duration.enter,
      ease: cubicBezier(...motionTokens.bezier.expo),
      onUpdate: () => {
        el.textContent = format(proxy.v);
      },
      onComplete: () => {
        el.textContent = format(to);
      },
    });
  });
}
