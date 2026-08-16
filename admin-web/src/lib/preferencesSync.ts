"use client";

/**
 * Debounced write-through of user preferences (theme/lang/appearance/slideshow)
 * to the server, shared by theme.tsx, i18n.tsx, and appearance.tsx.
 *
 * Deliberately does NOT import from theme.tsx / i18n.tsx / appearance.tsx —
 * those modules import `pushPreferences` from here, so importing back would
 * create a circular dependency. The storage key constants below are
 * duplicated (not re-exported) from those modules for that reason; keep them
 * in sync if a key ever changes.
 */
import { api } from "@/lib/api";

const THEME_KEY = "auto-gallery-theme";
const LANG_KEY = "auto-gallery-lang";
const APPEARANCE_KEY = "auto-gallery-appearance-v1";
const SLIDESHOW_KEY = "auto-gallery-slideshow-v1";
const TOKEN_KEY = "ag_token";

const DEBOUNCE_MS = 800;
let timer: ReturnType<typeof setTimeout> | null = null;

/**
 * PUT /me/preferences whole-replaces server state, and every setter writes
 * localStorage synchronously before calling pushPreferences — so by the time
 * the debounce timer fires, localStorage IS the full merged picture. Read it
 * fresh here rather than threading a merged object through every caller.
 */
function readFullPreferencesFromLocalStorage(): Record<string, unknown> {
  try {
    const appearanceRaw = localStorage.getItem(APPEARANCE_KEY);
    const slideshowRaw = localStorage.getItem(SLIDESHOW_KEY);
    return {
      theme: localStorage.getItem(THEME_KEY) || "system",
      lang: localStorage.getItem(LANG_KEY) || "zh",
      appearance: appearanceRaw ? JSON.parse(appearanceRaw) : {},
      slideshow: slideshowRaw ? JSON.parse(slideshowRaw) : {},
    };
  } catch {
    return {};
  }
}

/**
 * Schedule a debounced (800ms, shared/coalesced across all preference setters)
 * write-through to the server. `partial` documents what just changed for
 * callers/readability; the payload actually sent is always the full merged
 * object (see readFullPreferencesFromLocalStorage).
 *
 * No-ops while unauthenticated (no ag_token in localStorage) — this keeps
 * the login page (which also has theme/lang toggles) localStorage-only,
 * per spec, instead of firing a doomed 401 request.
 */
export function pushPreferences(_partial?: Record<string, unknown>) {
  if (typeof window === "undefined") return;
  if (!localStorage.getItem(TOKEN_KEY)) return;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    api.updateMyPreferences(readFullPreferencesFromLocalStorage()).catch(() => {
      // Best-effort: localStorage already holds the value for this session;
      // a failed server sync just means other devices won't see it yet.
    });
  }, DEBOUNCE_MS);
}
