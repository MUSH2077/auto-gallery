"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { pushPreferences } from "@/lib/preferencesSync";

export type Lang = "zh" | "en";
type I18nVars = Record<string, string | number | boolean | null | undefined>;
export type TFunction = (key: string, fallbackOrVars?: string | I18nVars, vars?: I18nVars) => string;

export const STORAGE_KEY = "auto-gallery-lang";

const catalogLoaders: Record<Lang, () => Promise<Record<string, string>>> = {
  zh: () => import("./locales/zh.json").then((module) => module.default),
  en: () => import("./locales/en.json").then((module) => module.default),
};

interface I18nContextType {
  lang: Lang;
  t: TFunction;
  setLang: (lang: Lang) => void;
}

const I18nContext = createContext<I18nContextType>({
  lang: "zh",
  t: (key) => key,
  setLang: () => {},
});

const reportedMissingKeys = new Set<string>();

function interpolate(template: string, vars?: I18nVars): string {
  if (!vars) return template;
  return template.replace(/\{([^{}]+)\}/g, (match, name: string) => {
    const value = vars[name];
    return value === undefined || value === null ? match : String(value);
  });
}

export function useI18n() {
  return useContext(I18nContext);
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("zh");
  const [dictionary, setDictionary] = useState<Record<string, string> | null>(null);
  const loadGeneration = useRef(0);

  const load = useCallback(async (nextLang: Lang) => {
    const generation = ++loadGeneration.current;
    const nextDictionary = await catalogLoaders[nextLang]();
    if (generation !== loadGeneration.current) return;
    setLangState(nextLang);
    setDictionary(nextDictionary);
  }, []);

  useEffect(() => {
    let initial: Lang = "zh";
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "en" || stored === "zh") initial = stored;
    } catch {}
    void load(initial);
  }, [load]);

  const setLang = useCallback((nextLang: Lang) => {
    try { localStorage.setItem(STORAGE_KEY, nextLang); } catch {}
    pushPreferences({ lang: nextLang });
    void load(nextLang);
  }, [load]);

  const t = useCallback<TFunction>((key, fallbackOrVars, vars) => {
    const fallback = typeof fallbackOrVars === "string" ? fallbackOrVars : undefined;
    const interpolationVars = typeof fallbackOrVars === "object" ? fallbackOrVars : vars;
    let value = dictionary?.[key];
    if (!value) {
      const reportKey = `${lang}:${key}`;
      if (dictionary && process.env.NODE_ENV !== "production" && !reportedMissingKeys.has(reportKey)) {
        reportedMissingKeys.add(reportKey);
        console.error(`[i18n] Missing ${lang} translation: ${key}`);
      }
      const safeFallback = fallback && (lang === "zh" || !/[\u3400-\u9fff]/u.test(fallback))
        ? fallback
        : undefined;
      value = safeFallback || key;
    }
    return interpolate(value, interpolationVars);
  }, [dictionary, lang]);

  if (!dictionary) {
    return (
      <div className="min-h-screen bg-subtle" role="status" aria-busy="true" />
    );
  }

  return (
    <I18nContext.Provider value={{ lang, t, setLang }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useT() {
  return useI18n().t;
}
