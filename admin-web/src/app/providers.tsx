"use client";
import { useEffect, useRef } from "react";
import { ThemeProvider, useTheme, type Theme } from "@/lib/theme";
import { I18nProvider, useI18n, type Lang } from "@/lib/i18n";
import { AuthProvider, useAuth } from "@/lib/auth";
import { useAppearanceSettings, type AppearanceSettings } from "@/lib/appearance";
import { SlideshowConfigProvider, applySlideshowPreferences } from "@/lib/slideshow/config";
import ErrorBoundary from "@/components/ErrorBoundary";
import { ToastProvider } from "@/components/Toast";
import { NotificationProvider } from "@/components/NotificationCenter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { useJobWebSocket } from "@/lib/useWebSocket";

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
 * AuthProvider supplies the restored user payload, so this does not need a
 * second `/me` query at layout level.
 */
function PreferencesHydrator() {
  const { user } = useAuth();
  const { setTheme } = useTheme();
  const { setLang } = useI18n();
  const { updateSettings } = useAppearanceSettings();
  const appliedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!user) return;
    // Apply once per distinct payload — keyed on user id + the preferences
    // blob itself, so a stable cached object (re-render without refetch)
    // doesn't reapply, but a genuinely new /me payload does.
    const marker = `${user.id}:${JSON.stringify(user.preferences)}`;
    if (appliedFor.current === marker) return;
    appliedFor.current = marker;

    const prefs = user.preferences || {};
    if (isTheme(prefs.theme)) setTheme(prefs.theme);
    if (isLang(prefs.lang)) setLang(prefs.lang);
    if (prefs.appearance && typeof prefs.appearance === "object") {
      updateSettings(prefs.appearance as Partial<AppearanceSettings>);
    }
    if (prefs.slideshow) applySlideshowPreferences(prefs.slideshow);
  }, [user, setTheme, setLang, updateSettings]);

  return null;
}

function TaskEventBridge() {
  const { isAuthenticated } = useAuth();
  useJobWebSocket({ enabled: isAuthenticated });
  return null;
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { staleTime: 30000, gcTime: 300000, refetchOnWindowFocus: false, refetchIntervalInBackground: false, retry: 1 }, mutations: { retry: 0 } },
  }));
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ThemeProvider>
            <AuthProvider>
              <SlideshowConfigProvider>
                <PreferencesHydrator />
                <TaskEventBridge />
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
