"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ThemeProvider } from "@/lib/theme";
import { I18nProvider } from "@/lib/i18n";
import { AuthProvider } from "@/lib/auth";
import ErrorBoundary from "@/components/ErrorBoundary";
import { ToastProvider } from "@/components/Toast";

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: { queries: { staleTime: 10000, refetchOnWindowFocus: false } },
  }));
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ThemeProvider>
            <AuthProvider><ToastProvider>{children}</ToastProvider></AuthProvider>
          </ThemeProvider>
        </I18nProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
