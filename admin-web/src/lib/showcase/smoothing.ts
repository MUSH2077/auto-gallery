// Shared frame-rate-independent smoothing for the showcase — used by the
// gallery canvas's scroll lerp. One formula, no reimplementation.
export const REFERENCE_FRAME_MS = 1000 / 60;
export const MAX_DT_MS = 100;

/** alpha(dt) = 1 - (1 - perFrameFactor)^(dt / REFERENCE_FRAME_MS); at dt=REFERENCE_FRAME_MS reduces to perFrameFactor. */
export function frameIndependentAlpha(perFrameFactor: number, dtMs: number): number {
  return 1 - Math.pow(1 - perFrameFactor, dtMs / REFERENCE_FRAME_MS);
}
