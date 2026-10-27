"use client";

/**
 * Debounced write-through of user preferences (theme/lang/appearance/slideshow)
 * to the server, shared by theme.tsx, i18n.tsx, and appearance.tsx.
 *
 * Deliberately does NOT import from theme.tsx / i18n.tsx / appearance.tsx —
 * those modules import `pushPreferences` from here, so importing back would
 * create a circular dependency. Theme defaults come from a dependency-free
 * module; the other storage keys below stay in sync with their providers.
 */
import { api } from "@/lib/api";
import { DEFAULT_THEME, THEME_STORAGE_KEY } from "@/lib/themePreference";

const THEME_KEY = THEME_STORAGE_KEY;
const LANG_KEY = "auto-gallery-lang";
const APPEARANCE_KEY = "auto-gallery-appearance-v1";
const SLIDESHOW_KEY = "auto-gallery-slideshow-v1";
import { csrfToken } from "@/lib/authFlow";

const DEBOUNCE_MS = 800;
let timer: ReturnType<typeof setTimeout> | null = null;

/**
 * PUT /me/preferences whole-replaces server state, and every setter writes
 * localStorage synchronously before calling pushPreferences — so by the time
 * the debounce timer fires, localStorage IS the full merged picture. Read it
 * fresh here rather than threading a merged object through every caller.
 */
function readFullPreferencesFromLocalStorage(): Record<string, unknown> | null {
  try {
    const appearanceRaw = localStorage.getItem(APPEARANCE_KEY);
    const slideshowRaw = localStorage.getItem(SLIDESHOW_KEY);
    return {
      theme: localStorage.getItem(THEME_KEY) || DEFAULT_THEME,
      lang: localStorage.getItem(LANG_KEY) || "zh",
      appearance: appearanceRaw ? JSON.parse(appearanceRaw) : {},
      slideshow: slideshowRaw ? JSON.parse(slideshowRaw) : {},
    };
  } catch {
    return null;
  }
}

/**
 * Schedule a debounced (800ms, shared/coalesced across all preference setters)
 * write-through of the full merged local preference object to the server.
 *
 * No-ops while unauthenticated (no session CSRF cookie) — this keeps
 * the login page (which also has theme/lang toggles) localStorage-only,
 * per spec, instead of firing a doomed 401 request.
 */
export function pushPreferences() {
  if (typeof window === "undefined") return;
  if (!csrfToken()) return;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    const preferences = readFullPreferencesFromLocalStorage();
    if (!preferences) return;
    api.updateMyPreferences(preferences).catch(() => {
      // Best-effort: localStorage already holds the value for this session;
      // a failed server sync just means other devices won't see it yet.
    });
  }, DEBOUNCE_MS);
}
