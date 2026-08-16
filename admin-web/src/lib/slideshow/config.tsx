"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { pushPreferences } from "@/lib/preferencesSync";

export const SLIDESHOW_STORAGE_KEY = "auto-gallery-slideshow-v1";
const LEGACY_SHOWCASE_STORAGE_KEY = "auto-gallery-showcase-v1";

export interface SlideshowConfig {
  slideDwellMs: number;
  slideTransition: "crossfade" | "kenburns";
  slideLoop: boolean;
  slideShowMeta: boolean;
}

export const DEFAULT_SLIDESHOW_CONFIG: SlideshowConfig = {
  slideDwellMs: 5000,
  slideTransition: "kenburns",
  slideLoop: true,
  slideShowMeta: true,
};

export function sanitizeSlideshowConfig(value: unknown): SlideshowConfig {
  if (!value || typeof value !== "object") return DEFAULT_SLIDESHOW_CONFIG;
  const raw = value as Partial<SlideshowConfig>;
  const dwell = typeof raw.slideDwellMs === "number" && Number.isFinite(raw.slideDwellMs)
    ? Math.min(15000, Math.max(2000, raw.slideDwellMs))
    : DEFAULT_SLIDESHOW_CONFIG.slideDwellMs;
  return {
    slideDwellMs: dwell,
    slideTransition: raw.slideTransition === "crossfade" || raw.slideTransition === "kenburns"
      ? raw.slideTransition
      : DEFAULT_SLIDESHOW_CONFIG.slideTransition,
    slideLoop: typeof raw.slideLoop === "boolean" ? raw.slideLoop : DEFAULT_SLIDESHOW_CONFIG.slideLoop,
    slideShowMeta: typeof raw.slideShowMeta === "boolean" ? raw.slideShowMeta : DEFAULT_SLIDESHOW_CONFIG.slideShowMeta,
  };
}

function readStored(): SlideshowConfig {
  if (typeof window === "undefined") return DEFAULT_SLIDESHOW_CONFIG;
  try {
    const current = localStorage.getItem(SLIDESHOW_STORAGE_KEY);
    if (current) {
      localStorage.removeItem(LEGACY_SHOWCASE_STORAGE_KEY);
      return sanitizeSlideshowConfig(JSON.parse(current));
    }
    const legacy = localStorage.getItem(LEGACY_SHOWCASE_STORAGE_KEY);
    if (!legacy) return DEFAULT_SLIDESHOW_CONFIG;
    const migrated = sanitizeSlideshowConfig(JSON.parse(legacy));
    localStorage.setItem(SLIDESHOW_STORAGE_KEY, JSON.stringify(migrated));
    localStorage.removeItem(LEGACY_SHOWCASE_STORAGE_KEY);
    return migrated;
  } catch {
    try { localStorage.removeItem(LEGACY_SHOWCASE_STORAGE_KEY); } catch {}
    return DEFAULT_SLIDESHOW_CONFIG;
  }
}

export function applySlideshowPreferences(value: unknown): void {
  if (typeof window === "undefined" || !value || typeof value !== "object") return;
  const merged = sanitizeSlideshowConfig({ ...readStored(), ...(value as Partial<SlideshowConfig>) });
  try {
    localStorage.setItem(SLIDESHOW_STORAGE_KEY, JSON.stringify(merged));
    localStorage.removeItem(LEGACY_SHOWCASE_STORAGE_KEY);
  } catch {}
  window.dispatchEvent(new CustomEvent("ag:slideshow-config"));
}

interface SlideshowContextValue {
  config: SlideshowConfig;
  update: (patch: Partial<SlideshowConfig>) => void;
}

const SlideshowConfigContext = createContext<SlideshowContextValue>({
  config: DEFAULT_SLIDESHOW_CONFIG,
  update: () => {},
});

export function useSlideshowConfig(): SlideshowContextValue {
  return useContext(SlideshowConfigContext);
}

export function SlideshowConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<SlideshowConfig>(DEFAULT_SLIDESHOW_CONFIG);

  useEffect(() => {
    setConfig(readStored());
    const sync = () => setConfig(readStored());
    window.addEventListener("ag:slideshow-config", sync);
    return () => window.removeEventListener("ag:slideshow-config", sync);
  }, []);

  const update = useCallback((patch: Partial<SlideshowConfig>) => {
    setConfig((previous) => {
      const next = sanitizeSlideshowConfig({ ...previous, ...patch });
      try { localStorage.setItem(SLIDESHOW_STORAGE_KEY, JSON.stringify(next)); } catch {}
      pushPreferences({ slideshow: next });
      return next;
    });
  }, []);

  return (
    <SlideshowConfigContext.Provider value={{ config, update }}>
      {children}
    </SlideshowConfigContext.Provider>
  );
}
