"use client";
import { Component, type ReactNode } from "react";

interface Props { children: ReactNode; fallback?: ReactNode }
interface State { hasError: boolean; error: Error | null }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-slate-900">
          <div className="bg-white dark:bg-slate-800 rounded-lg shadow-xl max-w-lg w-full mx-4 p-8 text-center">
            <div className="text-4xl mb-4">⚠</div>
            <h2 className="text-xl font-semibold mb-2 dark:text-white">Something went wrong</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              An unexpected error occurred. Try refreshing the page.
            </p>
            <pre className="text-xs text-left text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/30 rounded p-3 mb-4 overflow-auto max-h-32">
              {this.state.error?.message || "Unknown error"}
            </pre>
            <button
              onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload(); }}
              className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800"
            >
              Reload Page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
