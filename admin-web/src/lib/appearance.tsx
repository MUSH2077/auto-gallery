"use client";

import { useCallback, useEffect, useState } from "react";

export type WorkGridDensity = "compact" | "comfortable";
export type WorkPreviewDelayMs = 150 | 250 | 400;
export type WorkPreviewSize = "medium" | "large" | "fit";
export type WorkPreviewWheelSensitivity = "normal" | "relaxed";

export interface AppearanceSettings {
  workPreviewEnabled: boolean;
  workPreviewDelayMs: WorkPreviewDelayMs;
  workPreviewSize: WorkPreviewSize;
  workPreviewWheelSensitivity: WorkPreviewWheelSensitivity;
  workGridDensity: WorkGridDensity;
}

export const APPEARANCE_STORAGE_KEY = "auto-gallery-appearance-v1";
const LEGACY_WORK_PREVIEW_KEY = "auto-gallery-work-preview";
const APPEARANCE_EVENT = "auto-gallery-appearance-change";

export const DEFAULT_APPEARANCE_SETTINGS: AppearanceSettings = {
  workPreviewEnabled: true,
  workPreviewDelayMs: 250,
  workPreviewSize: "large",
  workPreviewWheelSensitivity: "normal",
  workGridDensity: "compact",
};

function isPreviewDelay(value: unknown): value is WorkPreviewDelayMs {
  return value === 150 || value === 250 || value === 400;
}

function isPreviewSize(value: unknown): value is WorkPreviewSize {
  return value === "medium" || value === "large" || value === "fit";
}

function isWheelSensitivity(value: unknown): value is WorkPreviewWheelSensitivity {
  return value === "normal" || value === "relaxed";
}

function isGridDensity(value: unknown): value is WorkGridDensity {
  return value === "compact" || value === "comfortable";
}

function sanitizeAppearanceSettings(value: unknown): AppearanceSettings {
  if (!value || typeof value !== "object") return DEFAULT_APPEARANCE_SETTINGS;
  const raw = value as Partial<AppearanceSettings>;
  return {
    workPreviewEnabled: typeof raw.workPreviewEnabled === "boolean" ? raw.workPreviewEnabled : DEFAULT_APPEARANCE_SETTINGS.workPreviewEnabled,
    workPreviewDelayMs: isPreviewDelay(raw.workPreviewDelayMs) ? raw.workPreviewDelayMs : DEFAULT_APPEARANCE_SETTINGS.workPreviewDelayMs,
    workPreviewSize: isPreviewSize(raw.workPreviewSize) ? raw.workPreviewSize : DEFAULT_APPEARANCE_SETTINGS.workPreviewSize,
    workPreviewWheelSensitivity: isWheelSensitivity(raw.workPreviewWheelSensitivity)
      ? raw.workPreviewWheelSensitivity
      : DEFAULT_APPEARANCE_SETTINGS.workPreviewWheelSensitivity,
    workGridDensity: isGridDensity(raw.workGridDensity) ? raw.workGridDensity : DEFAULT_APPEARANCE_SETTINGS.workGridDensity,
  };
}

export function readAppearanceSettings(): AppearanceSettings {
  if (typeof window === "undefined") return DEFAULT_APPEARANCE_SETTINGS;
  try {
    const stored = window.localStorage.getItem(APPEARANCE_STORAGE_KEY);
    if (stored) return sanitizeAppearanceSettings(JSON.parse(stored));
    const legacyPreview = window.localStorage.getItem(LEGACY_WORK_PREVIEW_KEY);
    if (legacyPreview === "off") {
      return { ...DEFAULT_APPEARANCE_SETTINGS, workPreviewEnabled: false };
    }
  } catch {}
  return DEFAULT_APPEARANCE_SETTINGS;
}

function writeAppearanceSettings(settings: AppearanceSettings) {
  window.localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(settings));
  window.dispatchEvent(new CustomEvent<AppearanceSettings>(APPEARANCE_EVENT, { detail: settings }));
}

export function useAppearanceSettings() {
  const [settings, setSettingsState] = useState<AppearanceSettings>(() => readAppearanceSettings());

  useEffect(() => {
    const applyCurrent = () => setSettingsState(readAppearanceSettings());
    const onCustom = (event: Event) => {
      const detail = (event as CustomEvent<AppearanceSettings>).detail;
      setSettingsState(sanitizeAppearanceSettings(detail));
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === APPEARANCE_STORAGE_KEY || event.key === LEGACY_WORK_PREVIEW_KEY) applyCurrent();
    };
    applyCurrent();
    window.addEventListener(APPEARANCE_EVENT, onCustom);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(APPEARANCE_EVENT, onCustom);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const updateSettings = useCallback((patch: Partial<AppearanceSettings>) => {
    setSettingsState((current) => {
      const next = sanitizeAppearanceSettings({ ...current, ...patch });
      try { writeAppearanceSettings(next); } catch {}
      return next;
    });
  }, []);

  const resetSettings = useCallback(() => {
    setSettingsState(DEFAULT_APPEARANCE_SETTINGS);
    try { writeAppearanceSettings(DEFAULT_APPEARANCE_SETTINGS); } catch {}
  }, []);

  return { settings, updateSettings, resetSettings };
}
