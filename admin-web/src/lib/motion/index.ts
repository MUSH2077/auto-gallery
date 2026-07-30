// Public API of the motion system. Business code imports from "@/lib/motion"
// only — never from "animejs" directly (enforced by convention; anime.ts is
// the single wrapper).

export { motionTokens, staggerDelay, bubbleStaggerDelay } from "./tokens";
export { motionConfig } from "./config";
export { enterHeatmap, countUp } from "./anime";
export { usePresence, useEnterOnce, useStaggeredEntrance, useViewportReveal } from "./hooks";
export type { StaggeredEntranceProps } from "./hooks";
