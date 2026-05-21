"use client";
import { Component, type ReactNode } from "react";
import { I18nProvider, useT } from "@/lib/i18n";

interface Props { children: ReactNode; fallback?: ReactNode }
interface State { hasError: boolean; error: Error | null }

function ErrorFallback({ error }: { error: Error | null }) {
  const t = useT();
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-slate-900">
      <div className="bg-white dark:bg-slate-800 rounded-lg shadow-xl max-w-lg w-full mx-4 p-8 text-center">
        <div className="text-4xl mb-4">⚠</div>
        <h2 className="text-xl font-semibold mb-2 dark:text-white">{t("common.something_wrong")}</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          {t("common.unexpected_error")}
        </p>
        <pre className="text-xs text-left text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/30 rounded p-3 mb-4 overflow-auto max-h-32">
          {error?.message || t("common.unknown_error")}
        </pre>
        <button
          onClick={() => window.location.reload()}
          className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800"
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
