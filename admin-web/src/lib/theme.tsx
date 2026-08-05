"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Globe2, Monitor, Moon, Sun } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { pushPreferences } from "@/lib/preferencesSync";

export type Theme = "light" | "dark" | "system";
type ResolvedTheme = "light" | "dark";

export const STORAGE_KEY = "auto-gallery-theme";
export const LEGACY_PALETTE_KEY = "auto-gallery-palette";

interface ThemeContextType {
  theme: Theme;
  resolved: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextType>({
  theme: "system",
  resolved: "light",
  setTheme: () => {},
});

export function useTheme() {
  return useContext(ThemeContext);
}

function resolveTheme(theme: Theme): ResolvedTheme {
  if (theme === "system") {
    return typeof window !== "undefined"
      && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  return theme;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>("system");
  const [resolved, setResolved] = useState<ResolvedTheme>("light");

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark" || stored === "system") {
        setThemeState(stored);
      }
      // Palette selection was removed in the neutral console redesign.
      localStorage.removeItem(LEGACY_PALETTE_KEY);
      document.documentElement.removeAttribute("data-theme");
    } catch {}
  }, []);

  const applyTheme = useCallback((nextTheme: Theme) => {
    const nextResolved = resolveTheme(nextTheme);
    setResolved(nextResolved);
    document.documentElement.classList.toggle("dark", nextResolved === "dark");
  }, []);

  useEffect(() => {
    applyTheme(theme);
  }, [applyTheme, theme]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = () => {
      if (theme === "system") applyTheme("system");
    };
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [applyTheme, theme]);

  const setTheme = useCallback((nextTheme: Theme) => {
    setThemeState(nextTheme);
    try { localStorage.setItem(STORAGE_KEY, nextTheme); } catch {}
    applyTheme(nextTheme);
    pushPreferences({ theme: nextTheme });
  }, [applyTheme]);

  return (
    <ThemeContext.Provider value={{ theme, resolved, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();
  const cycle: Theme[] = ["light", "dark", "system"];
  const next = cycle[(cycle.indexOf(theme) + 1) % cycle.length];
  const labels: Record<Theme, string> = {
    light: t("theme.light"),
    dark: t("theme.dark"),
    system: t("theme.system"),
  };
  const Icon = theme === "dark" ? Moon : theme === "system" ? Monitor : Sun;

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      className="btn-icon"
      aria-label={`${labels[theme]} — ${labels[next]}`}
      title={`${labels[theme]} — ${labels[next]}`}
    >
      <Icon className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
    </button>
  );
}

export function LangToggle() {
  const { lang, setLang } = useI18n();
  const label = lang === "zh" ? "Switch to English" : "切换到中文";

  return (
    <button
      type="button"
      onClick={() => setLang(lang === "zh" ? "en" : "zh")}
      className="btn-icon relative"
      title={label}
      aria-label={label}
    >
      <Globe2 className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
      <span className="absolute -bottom-0.5 -right-0.5 rounded bg-surface px-0.5 text-[8px] font-bold leading-3 text-muted" aria-hidden>
        {lang === "zh" ? "EN" : "中"}
      </span>
    </button>
  );
}
