"use client";
import { Component, type ReactNode } from "react";
import { I18nProvider, useT } from "@/lib/i18n";

interface Props { children: ReactNode; fallback?: ReactNode }
interface State { hasError: boolean; error: Error | null }

function ErrorFallback({ error }: { error: Error | null }) {
  const t = useT();
  return (
    <div className="flex min-h-screen items-center justify-center bg-[#f6f8fa] dark:bg-ag-bg">
      <div className="card mx-4 w-full max-w-lg p-8 text-center">
        <div className="text-4xl mb-4">⚠</div>
        <h2 className="mb-2 text-xl font-semibold text-[#24292f] dark:text-ag-text">{t("common.something_wrong")}</h2>
        <p className="mb-4 text-sm text-[#57606a] dark:text-[#8b949e]">
          {t("common.unexpected_error")}
        </p>
        <pre className="mb-4 max-h-32 overflow-auto rounded-md border border-[#cf222e]/30 bg-[#ffebe9] p-3 text-left text-xs text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">
          {error?.message || t("common.unknown_error")}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="btn-primary"
        >
          {t("common.reload_page")}
        </button>
      </div>
    </div>
  );
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <I18nProvider>
          <ErrorFallback error={this.state.error} />
        </I18nProvider>
      );
    }
    return this.props.children;
  }
}
