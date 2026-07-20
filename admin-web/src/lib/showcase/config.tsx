"use client";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { pushPreferences } from "@/lib/preferencesSync";

export const SHOWCASE_STORAGE_KEY = "auto-gallery-showcase-v1";

export interface ShowcaseConfig {
  // content source
  scope: "all" | "favorites";
  source: string | null;
  tag: string | null;
  includeNsfw: boolean;
  // motion
  trailMax: number;
  spawnIntervalMs: number;
  followDamping: number;
  parallaxStrength: number;
  minimal: boolean;
  // slideshow
  slideDwellMs: number;
  slideTransition: "crossfade" | "kenburns";
  slideLoop: boolean;
  slideShowMeta: boolean;
  // homepage behavior
  landing: "showcase" | "dashboard";
  headline: string;
  showStats: boolean;
}

export const DEFAULT_SHOWCASE_CONFIG: ShowcaseConfig = {
  scope: "all",
  source: null,
  tag: null,
  includeNsfw: false,
  trailMax: 18,
  spawnIntervalMs: 90,
  followDamping: 0.12,
  parallaxStrength: 0.4,
  minimal: false,
  slideDwellMs: 5000,
  slideTransition: "kenburns",
  slideLoop: true,
  slideShowMeta: true,
  landing: "showcase",
  headline: "",
  showStats: true,
};

function isScope(value: unknown): value is ShowcaseConfig["scope"] {
  return value === "all" || value === "favorites";
}

function isSlideTransition(value: unknown): value is ShowcaseConfig["slideTransition"] {
  return value === "crossfade" || value === "kenburns";
}

function isLanding(value: unknown): value is ShowcaseConfig["landing"] {
  return value === "showcase" || value === "dashboard";
}

function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}

function sanitizeNullableString(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value === "string") return value === "" ? null : value;
  return null;
}

/**
 * Sanitize an untrusted value (parsed localStorage JSON, or a server-supplied
 * preferences payload) into a well-formed ShowcaseConfig. Any field that is
 * missing, of the wrong type, or out of range falls back to the corresponding
 * DEFAULT_SHOWCASE_CONFIG value — this never throws. Numeric ranges must match
 * the bounds offered by the settings UI (see Task 3) exactly.
 */
export function sanitizeShowcaseConfig(value: unknown): ShowcaseConfig {
  if (!value || typeof value !== "object") return DEFAULT_SHOWCASE_CONFIG;
  const raw = value as Partial<ShowcaseConfig>;
  return {
    scope: isScope(raw.scope) ? raw.scope : DEFAULT_SHOWCASE_CONFIG.scope,
    source: sanitizeNullableString(raw.source),
    tag: sanitizeNullableString(raw.tag),
    includeNsfw: typeof raw.includeNsfw === "boolean" ? raw.includeNsfw : DEFAULT_SHOWCASE_CONFIG.includeNsfw,
    trailMax: clampNumber(raw.trailMax, 4, 40, DEFAULT_SHOWCASE_CONFIG.trailMax),
    spawnIntervalMs: clampNumber(raw.spawnIntervalMs, 40, 400, DEFAULT_SHOWCASE_CONFIG.spawnIntervalMs),
    followDamping: clampNumber(raw.followDamping, 0.02, 0.5, DEFAULT_SHOWCASE_CONFIG.followDamping),
    parallaxStrength: clampNumber(raw.parallaxStrength, 0, 1, DEFAULT_SHOWCASE_CONFIG.parallaxStrength),
    minimal: typeof raw.minimal === "boolean" ? raw.minimal : DEFAULT_SHOWCASE_CONFIG.minimal,
    slideDwellMs: clampNumber(raw.slideDwellMs, 2000, 15000, DEFAULT_SHOWCASE_CONFIG.slideDwellMs),
    slideTransition: isSlideTransition(raw.slideTransition) ? raw.slideTransition : DEFAULT_SHOWCASE_CONFIG.slideTransition,
    slideLoop: typeof raw.slideLoop === "boolean" ? raw.slideLoop : DEFAULT_SHOWCASE_CONFIG.slideLoop,
    slideShowMeta: typeof raw.slideShowMeta === "boolean" ? raw.slideShowMeta : DEFAULT_SHOWCASE_CONFIG.slideShowMeta,
    landing: isLanding(raw.landing) ? raw.landing : DEFAULT_SHOWCASE_CONFIG.landing,
    headline: typeof raw.headline === "string" ? raw.headline : DEFAULT_SHOWCASE_CONFIG.headline,
    showStats: typeof raw.showStats === "boolean" ? raw.showStats : DEFAULT_SHOWCASE_CONFIG.showStats,
  };
}

function readStored(): ShowcaseConfig {
  if (typeof window === "undefined") return DEFAULT_SHOWCASE_CONFIG;
  try {
    const raw = window.localStorage.getItem(SHOWCASE_STORAGE_KEY);
    return raw ? sanitizeShowcaseConfig(JSON.parse(raw)) : DEFAULT_SHOWCASE_CONFIG;
  } catch {
    return DEFAULT_SHOWCASE_CONFIG;
  }
}

/** Apply server-side preferences over localStorage (called by the hydrator). */
export function applyShowcasePreferences(value: unknown): void {
  if (typeof window === "undefined" || !value || typeof value !== "object") return;
  const merged = sanitizeShowcaseConfig({ ...readStored(), ...(value as Partial<ShowcaseConfig>) });
  try {
    window.localStorage.setItem(SHOWCASE_STORAGE_KEY, JSON.stringify(merged));
  } catch {}
  window.dispatchEvent(new CustomEvent("ag:showcase-config"));
}

interface Ctx {
  config: ShowcaseConfig;
  update: (patch: Partial<ShowcaseConfig>) => void;
}

const ShowcaseConfigContext = createContext<Ctx>({
  config: DEFAULT_SHOWCASE_CONFIG,
  update: () => {},
});

export function useShowcaseConfig(): Ctx {
  return useContext(ShowcaseConfigContext);
}

export function ShowcaseConfigProvider({ children }: { children: ReactNode }) {
  // Start from defaults so server and first client render match, then adopt
  // localStorage in a useEffect (deferred, not a useState lazy initializer)
  // so the initial client render never diverges from the server-rendered
  // defaults. readStored() runs the value through sanitizeShowcaseConfig,
  // so a corrupted or hand-edited localStorage entry can't reach state.
  const [config, setConfig] = useState<ShowcaseConfig>(DEFAULT_SHOWCASE_CONFIG);

  useEffect(() => {
    setConfig(readStored());
    const onExternal = () => setConfig(readStored());
    window.addEventListener("ag:showcase-config", onExternal);
    return () => window.removeEventListener("ag:showcase-config", onExternal);
  }, []);

  const update = useCallback((patch: Partial<ShowcaseConfig>) => {
    setConfig((prev) => {
      const next = { ...prev, ...patch };
      try {
        localStorage.setItem(SHOWCASE_STORAGE_KEY, JSON.stringify(next));
      } catch {}
      pushPreferences({ showcase: next });
      return next;
    });
  }, []);

  return (
    <ShowcaseConfigContext.Provider value={{ config, update }}>
      {children}
    </ShowcaseConfigContext.Provider>
  );
}
