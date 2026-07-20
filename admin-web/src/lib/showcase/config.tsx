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

function readStored(): ShowcaseConfig {
  if (typeof window === "undefined") return DEFAULT_SHOWCASE_CONFIG;
  try {
    const raw = window.localStorage.getItem(SHOWCASE_STORAGE_KEY);
    return raw ? { ...DEFAULT_SHOWCASE_CONFIG, ...JSON.parse(raw) } : DEFAULT_SHOWCASE_CONFIG;
  } catch {
    return DEFAULT_SHOWCASE_CONFIG;
  }
}

/** Apply server-side preferences over localStorage (called by the hydrator). */
export function applyShowcasePreferences(value: unknown): void {
  if (typeof window === "undefined" || !value || typeof value !== "object") return;
  const merged = { ...readStored(), ...(value as Partial<ShowcaseConfig>) };
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
  // localStorage in an effect (same pattern as the appearance provider).
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
