// Runtime gates for the motion system. prefers-reduced-motion is the highest
// priority (see docs/frontend-motion-audit.md §4.5): when it is set, JS-driven
// animations must not start at all — the globals.css kill-switch is only the
// fallback for plain CSS transitions.

export const motionConfig = {
  prefersReduced(): boolean {
    return (
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  },

  /** Coarse low-end heuristic — skip non-essential animation work entirely. */
  isLowEnd(): boolean {
    return (
      typeof navigator !== "undefined" &&
      (navigator.hardwareConcurrency ?? 8) <= 4
    );
  },

  /**
   * Master gate every JS animation must pass before starting.
   * `essential` marks animations that carry state information (e.g. progress
   * feedback) and are kept on low-end devices — reduced-motion still wins.
   */
  shouldAnimate({ essential = false }: { essential?: boolean } = {}): boolean {
    if (this.prefersReduced()) return false;
    if (!essential && this.isLowEnd()) return false;
    return true;
  },
};
