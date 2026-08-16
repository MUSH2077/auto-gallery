"use client";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ThemeProvider, useTheme, type Theme } from "@/lib/theme";
import { I18nProvider, useI18n, type Lang } from "@/lib/i18n";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useAppearanceSettings, type AppearanceSettings } from "@/lib/appearance";
import { SlideshowConfigProvider, applySlideshowPreferences } from "@/lib/slideshow/config";
import { api, queryKeys } from "@/lib/api";
import ErrorBoundary from "@/components/ErrorBoundary";
import { ToastProvider } from "@/components/Toast";
import { NotificationProvider } from "@/components/NotificationCenter";

function isTheme(v: unknown): v is Theme {
  return v === "light" || v === "dark" || v === "system";
}

function isLang(v: unknown): v is Lang {
  return v === "zh" || v === "en";
}

/**
 * On successful /me load (login or session restore), apply the server's
 * saved preferences over whatever is currently in localStorage. Renders
 * nothing — it only needs to sit below AuthProvider/ThemeProvider/I18nProvider
 * to read their context and setters.
 *
 * `enabled: isAuthenticated` keeps this a no-op on the login page (no token
 * yet, so no /me request at all) and fires automatically the moment
 * AuthProvider's `user` state flips true after login.
 */
function PreferencesHydrator() {
  const { isAuthenticated } = useAuth();
  const { setTheme } = useTheme();
  const { setLang } = useI18n();
  const { updateSettings } = useAppearanceSettings();
  const appliedFor = useRef<string | null>(null);

  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe, enabled: isAuthenticated });

  useEffect(() => {
    if (!me.data) return;
    // Apply once per distinct payload — keyed on user id + the preferences
    // blob itself, so a stable cached object (re-render without refetch)
    // doesn't reapply, but a genuinely new /me payload does.
    const marker = `${me.data.id}:${JSON.stringify(me.data.preferences)}`;
    if (appliedFor.current === marker) return;
    appliedFor.current = marker;

    const prefs = me.data.preferences || {};
    if (isTheme(prefs.theme)) setTheme(prefs.theme);
    if (isLang(prefs.lang)) setLang(prefs.lang);
    if (prefs.appearance && typeof prefs.appearance === "object") {
      updateSettings(prefs.appearance as Partial<AppearanceSettings>);
    }
    if (prefs.slideshow) applySlideshowPreferences(prefs.slideshow);
  }, [me.data, setTheme, setLang, updateSettings]);

  return null;
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { staleTime: 30000, gcTime: 300000, refetchOnWindowFocus: false, retry: 1 }, mutations: { retry: 0 } },
  }));
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ThemeProvider>
            <AuthProvider>
              <SlideshowConfigProvider>
                <PreferencesHydrator />
                <NotificationProvider>
                  <ToastProvider>{children}</ToastProvider>
                </NotificationProvider>
              </SlideshowConfigProvider>
            </AuthProvider>
          </ThemeProvider>
        </I18nProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
