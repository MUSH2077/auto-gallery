// Shared timing/gating math for the showcase pointer trail — used by BOTH the
// permanent DOM renderer (ShowcaseTrailDOM, Task 4) and the WebGL enhancement
// layer (ShowcaseCanvas + webgl.ts, Task 5) so the two renderers feel
// identical and never drift into two subtly-different formulas. Extracted
// from ShowcaseTrailDOM's original inline implementation when Task 5 needed
// the exact same math — see that file for the full rationale behind each
// constant.
//
// Pure functions only: no DOM, no React, no rAF. Both renderers own their own
// rAF loop and call into this module for the parts that must match exactly.

import type { ShowcaseConfig } from "./config";

export type TrailConfig = Pick<
  ShowcaseConfig,
  "trailMax" | "spawnIntervalMs" | "followDamping" | "parallaxStrength" | "minimal"
>;

/** Brief-specified floor so a small trailMax / fast spawnInterval combination never produces a flash-cut trail. */
export const MIN_LIFETIME_MS = 900;

export function computeLifetimeMs(config: Pick<TrailConfig, "trailMax" | "spawnIntervalMs">): number {
  return Math.max(MIN_LIFETIME_MS, config.trailMax * config.spawnIntervalMs);
}

// `followDamping` was authored (and its default/range tuned) against a
// per-callback lerp — `damped += (raw - damped) * followDamping`, invoked
// once per rAF frame with no time term — which implicitly means "close this
// fraction of the gap in one 60Hz frame's worth of time." REFERENCE_FRAME_MS
// is that assumed frame time; frameIndependentAlpha() below converts the
// per-frame fraction into a continuous rate so the *wall-clock* convergence
// speed stays the same regardless of the display's actual refresh rate.
export const REFERENCE_FRAME_MS = 1000 / 60;

// Clamp applied to the rAF delta before it feeds the smoothing formula, so a
// backgrounded tab returning to the foreground (or any other multi-frame
// stall) can't be read as "one giant frame" and snap the trail across the
// screen. Defense-in-depth alongside resetting lastFrameTime on hide.
export const MAX_DT_MS = 100;

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
 * time is preserved instead of scaling with frame count. This is the one
 * formula both the DOM and WebGL renderers use — do not reimplement it.
 */
export function frameIndependentAlpha(perFrameFactor: number, dtMs: number): number {
  return 1 - Math.pow(1 - perFrameFactor, dtMs / REFERENCE_FRAME_MS);
}

// Real browser `pointermove` events stop firing the instant the pointer
// stops moving (or leaves the container/window) — a rAF loop does not, it
// keeps running every frame regardless. Forwarding to
// `controller.pointerMove` must therefore be gated on genuine motion, not
// merely "a pointer has been seen at some point": keep forwarding while
// either (a) a real `pointermove` event arrived within this window — so a
// still-moving-but-nearly-caught-up pointer (large followDamping, or a very
// slow/sub-pixel drag) never gets cut off — or (b) `damped` has not yet
// converged to `raw` within CONVERGENCE_EPSILON_PX, which lets the trail
// finish its short smoothing-lag deceleration tail after the last real
// event before going quiet. Once neither holds, spawning stops.
//
// This is the exact gate that fixed the Task 4 idle-spawn bug (a still
// cursor spawning a fresh trail item every spawnIntervalMs forever at a
// frozen coordinate). Both renderers must use this one gate, not two
// differently-tuned ones.
export const MOVE_RECENCY_WINDOW_MS = 200;

// Below this many px of lag between `raw` and `damped`, the smoothing tail
// is considered finished. Small relative to any perceptible pointer speed
// (which moves `raw` many px/frame), so it only trips once motion has
// genuinely stopped and `damped` has caught up.
export const CONVERGENCE_EPSILON_PX = 0.5;

/** Whether the pointer is still "genuinely moving" and should keep spawning trail items — see the constants above. */
export function isPointerActive(
  now: number,
  lastRealMoveTime: number,
  raw: { x: number; y: number },
  damped: { x: number; y: number },
): boolean {
  const recentRealMove = now - lastRealMoveTime < MOVE_RECENCY_WINDOW_MS;
  const stillConverging = Math.hypot(raw.x - damped.x, raw.y - damped.y) > CONVERGENCE_EPSILON_PX;
  return recentRealMove || stillConverging;
}
