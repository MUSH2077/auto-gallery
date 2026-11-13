"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Moon, Sun } from "lucide-react";

import { useI18n } from "@/lib/i18n";
import { pushPreferences } from "@/lib/preferencesSync";
import { DEFAULT_THEME, THEME_STORAGE_KEY, type Theme } from "@/lib/themePreference";

export type { Theme } from "@/lib/themePreference";

export const STORAGE_KEY = THEME_STORAGE_KEY;

interface ThemeContextType {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextType>({
  theme: DEFAULT_THEME,
  setTheme: () => {},
});

export function useTheme() {
  return useContext(ThemeContext);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(DEFAULT_THEME);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "light" || stored === "dark") {
        setThemeState(stored);
      }
    } catch {}
    setInitialized(true);
  }, []);

  const applyTheme = useCallback((nextTheme: Theme) => {
    document.documentElement.classList.toggle("dark", nextTheme === "dark");
    document.documentElement.style.colorScheme = nextTheme;
  }, []);

  useEffect(() => {
    if (initialized) applyTheme(theme);
  }, [applyTheme, theme, initialized]);

  const setTheme = useCallback((nextTheme: Theme) => {
    setThemeState(nextTheme);
    try { localStorage.setItem(STORAGE_KEY, nextTheme); } catch {}
    applyTheme(nextTheme);
    pushPreferences();
  }, [applyTheme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();
  const next = theme === "light" ? "dark" : "light";
  const labels: Record<Theme, string> = {
    light: t("theme.light"),
    dark: t("theme.dark"),
  };
  const Icon = theme === "dark" ? Moon : Sun;

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
      className="text-button gallery-account-language"
      title={label}
      aria-label={label}
    >
      {lang === "zh" ? "English" : "中文"}
    </button>
  );
}
