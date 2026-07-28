// Motion design tokens — the single vocabulary for every duration, easing,
// distance and stagger value in the app. Components must never hardcode
// timings; they consume these tokens (JS) or the matching Tailwind theme
// values / globals.css keyframes (CSS).
//
// KEEP IN SYNC with:
//   - tailwind.config.ts  transitionDuration { fast/base/slow } + transitionTimingFunction.expo
//   - src/app/globals.css @keyframes block (fadeUp / popIn / popOut / barGrow / dialog*)

export const motionTokens = {
  /** Durations in ms. fast/base/slow mirror the Tailwind duration-* tokens. */
  duration: {
    instant: 80,
    fast: 120,
    base: 150,
    slow: 240,
    /** Page/element entrances (matches .page-transition 400ms). */
    enter: 400,
  },
  /** Cubic-bezier control points. expo mirrors Tailwind ease-expo. */
  bezier: {
    expo: [0.16, 1, 0.3, 1] as const,
    sharp: [0.4, 0, 0.2, 1] as const,
  },
  /** Translate distances in px for enter/exit movement. */
  distance: {
    xs: 4,
    sm: 8,
    md: 12,
  },
  scale: {
    subtle: 0.98,
    press: 0.97,
  },
  /** List-entrance stagger: per-item step and total cap (works grid et al). */
  stagger: {
    step: 30,
    cap: 300,
    /** Tag bubbles stay individually legible across a full 50-item page. */
    bubbleStep: 24,
    bubbleCap: 1176,
    /** Dense stagger for large target sets (SVG heatmap cells). */
    denseStep: 1.5,
  },
} as const;

/**
 * CSS var value for `.page-item` style stagger: `--delay: staggerDelay(i)`.
 * Centralizes the previous inline `Math.min(index * 30, 300)ms` constants.
 */
export function staggerDelay(index: number): string {
  return `${Math.min(index * motionTokens.stagger.step, motionTokens.stagger.cap)}ms`;
}

export function bubbleStaggerDelay(index: number): string {
  return `${Math.min(index * motionTokens.stagger.bubbleStep, motionTokens.stagger.bubbleCap)}ms`;
}
