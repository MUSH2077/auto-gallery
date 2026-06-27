"use client";
import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";

type Theme = "light" | "dark" | "system";
type ResolvedTheme = "light" | "dark";
export type Palette = "github" | "nord" | "rose";

const STORAGE_KEY = "auto-gallery-theme";
const PALETTE_KEY = "auto-gallery-palette";
export const PALETTES: Palette[] = ["github", "nord", "rose"];
export const PALETTE_LABELS: Record<Palette, string> = { github: "GitHub", nord: "Nord", rose: "Rosé" };

interface ThemeContextType {
  theme: Theme;
  resolved: ResolvedTheme;
  setTheme: (t: Theme) => void;
  palette: Palette;
  setPalette: (p: Palette) => void;
}

const ThemeContext = createContext<ThemeContextType>({
  theme: "system",
  resolved: "light",
  setTheme: () => {},
  palette: "github",
  setPalette: () => {},
});

export function useTheme() {
  return useContext(ThemeContext);
}

function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === "system") {
    if (typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }
  return theme;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("system");
  const [resolved, setResolved] = useState<ResolvedTheme>("light");
  const [palette, setPaletteState] = useState<Palette>("github");

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark" || stored === "system") {
        setThemeState(stored);
      }
    } catch {}
  }, []);

  const applyTheme = useCallback((t: Theme) => {
    const r = resolveTheme(t);
    setResolved(r);
    document.documentElement.classList.toggle("dark", r === "dark");
  }, []);

  useEffect(() => {
    applyTheme(theme);
  }, [theme, applyTheme]);

  // Listen for system theme changes
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      if (theme === "system") applyTheme("system");
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme, applyTheme]);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try { localStorage.setItem(STORAGE_KEY, t); } catch {}
    applyTheme(t);
  }, [applyTheme]);

  // ── Palette (data-theme) ──
  useEffect(() => {
    try {
      const stored = localStorage.getItem(PALETTE_KEY) as Palette | null;
      if (stored && PALETTES.includes(stored)) setPaletteState(stored);
    } catch {}
  }, []);
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", palette);
  }, [palette]);
  const setPalette = useCallback((p: Palette) => {
    setPaletteState(p);
    try { localStorage.setItem(PALETTE_KEY, p); } catch {}
    document.documentElement.setAttribute("data-theme", p);
  }, []);

  return <ThemeContext.Provider value={{ theme, resolved, setTheme, palette, setPalette }}>{children}</ThemeContext.Provider>;
}

import { useI18n } from "@/lib/i18n";

// SVG icon components
function SunIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5" />
      <line x1="12" y1="1" x2="12" y2="3" /><line x1="12" y1="21" x2="12" y2="23" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" /><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="1" y1="12" x2="3" y2="12" /><line x1="21" y1="12" x2="23" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" /><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  );
}

function MoonIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function MonitorIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  );
}

function GlobeIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();

  const cycle: Theme[] = ["light", "dark", "system"];
  const next = cycle[(cycle.indexOf(theme) + 1) % cycle.length];

  const labels: Record<Theme, string> = {
    light: t("theme.light", "Light"),
    dark: t("theme.dark", "Dark"),
    system: t("theme.system", "System"),
  };

  const icon = theme === "dark" ? <MoonIcon className="w-5 h-5" /> : theme === "system" ? <MonitorIcon className="w-5 h-5" /> : <SunIcon className="w-5 h-5" />;

  return (
    <button
      onClick={() => setTheme(next)}
      className="p-1.5 rounded hover:bg-white/10 transition-colors text-white/80 hover:text-white"
      title={`${labels[theme]} — click for ${labels[next]}`}
    >
      {icon}
    </button>
  );
}

function PaletteIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="13.5" cy="6.5" r=".75" fill="currentColor" stroke="none" />
      <circle cx="17.5" cy="10.5" r=".75" fill="currentColor" stroke="none" />
      <circle cx="8.5" cy="7.5" r=".75" fill="currentColor" stroke="none" />
      <circle cx="6.5" cy="12.5" r=".75" fill="currentColor" stroke="none" />
      <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.93 0 1.65-.75 1.65-1.69 0-.44-.18-.83-.44-1.12-.29-.29-.44-.65-.44-1.13a1.64 1.64 0 0 1 1.67-1.67h2C19.5 16.4 22 13.9 22 10.85 22 6 17.5 2 12 2z" />
    </svg>
  );
}

export function PaletteToggle() {
  const { palette, setPalette } = useTheme();
  const next = PALETTES[(PALETTES.indexOf(palette) + 1) % PALETTES.length];
  return (
    <button
      onClick={() => setPalette(next)}
      className="p-1.5 rounded hover:bg-white/10 transition-colors text-white/80 hover:text-white flex items-center gap-1"
      title={`Theme: ${PALETTE_LABELS[palette]} — click for ${PALETTE_LABELS[next]}`}
    >
      <PaletteIcon className="w-5 h-5" />
      <span className="hidden sm:inline text-xs font-medium">{PALETTE_LABELS[palette]}</span>
    </button>
  );
}

export function LangToggle() {
  const { lang, setLang } = useI18n();

  return (
    <button
      onClick={() => setLang(lang === "zh" ? "en" : "zh")}
      className="p-1.5 rounded hover:bg-white/10 transition-colors text-white/80 hover:text-white flex items-center gap-1"
      title={lang === "zh" ? "Switch to English" : "切换到中文"}
    >
      <GlobeIcon className="w-5 h-5" />
      <span className="text-xs font-medium">{lang === "zh" ? "EN" : "中"}</span>
    </button>
  );
}
