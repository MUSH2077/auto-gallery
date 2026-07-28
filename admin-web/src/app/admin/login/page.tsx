"use client";
import { useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useAuth, type AuthUser } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import { ThemeToggle, LangToggle } from "@/lib/theme";
import { SHOWCASE_STORAGE_KEY, DEFAULT_SHOWCASE_CONFIG } from "@/lib/showcase/config";
import SourceCodeLink from "@/components/SourceCodeLink";

/**
 * Decide the post-login destination. The server-side preference
 * (`preferences.showcase.landing`, from `/me`) is authoritative — it's what
 * `PreferencesHydrator` eventually mirrors into localStorage, but that mirror
 * runs asynchronously and never on a fresh browser/device before this runs.
 * Falls back to localStorage (covers e.g. an already-hydrated session where
 * the in-memory `user` hasn't been refreshed), then to the showcase default.
 */
function resolveLanding(preferences: AuthUser["preferences"]): "/" | "/admin" {
  const serverShowcase =
    preferences && typeof preferences === "object"
      ? (preferences as Record<string, unknown>).showcase
      : undefined;
  const serverLanding =
    serverShowcase && typeof serverShowcase === "object"
      ? (serverShowcase as Record<string, unknown>).landing
      : undefined;
  if (serverLanding === "dashboard") return "/admin";
  if (serverLanding === "showcase") return "/";

  try {
    const raw = localStorage.getItem(SHOWCASE_STORAGE_KEY);
    const local = raw ? JSON.parse(raw) : null;
    if (local && local.landing === "dashboard") return "/admin";
    if (local && local.landing === "showcase") return "/";
  } catch {
    // malformed localStorage — fall through to default
  }

  return DEFAULT_SHOWCASE_CONFIG.landing === "dashboard" ? "/admin" : "/";
}

export default function LoginPage() {
  const t = useT();
  const { login, isAuthenticated, user } = useAuth();
  const router = useRouter();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  if (isAuthenticated) {
    router.replace(user?.must_change_password ? "/admin/settings/profile" : resolveLanding(user?.preferences));
    return null;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const authUser = await login(username, password);
      if (authUser.must_change_password) {
        router.replace("/admin/settings/profile");
      } else {
        router.replace(resolveLanding(authUser.preferences));
      }
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : t("auth.invalid_credentials"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-subtle px-4 py-10 text-fg dark:bg-canvas dark:text-fg">
      <div className="absolute right-4 top-4 flex items-center gap-2">
        <LangToggle />
        <ThemeToggle />
      </div>

      <section className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-md border border-border bg-white text-lg font-semibold tracking-tight shadow-sm dark:border-border dark:bg-surface">
            AG
          </div>
          <h1 className="text-2xl font-semibold tracking-normal">auto-gallery</h1>
          <p className="mt-1 text-sm text-muted">{t("auth.admin_panel")}</p>
        </div>

        <form onSubmit={handleSubmit} className="card p-5">
          <div className="space-y-4">
            <div>
              <label className="mb-1.5 block text-sm font-medium" htmlFor="username">{t("auth.username")}</label>
              <input
                id="username"
                type="text"
                autoComplete="username"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="input w-full"
                placeholder="admin"
              />
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium" htmlFor="password">{t("auth.password")}</label>
              <input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input w-full"
                placeholder="••••••••"
              />
            </div>

            <button type="submit" disabled={loading} className="btn-primary w-full">
              {loading ? t("auth.logging_in") : t("auth.login_button")}
            </button>
          </div>
        </form>

        <p className="mt-5 text-center text-xs text-muted">
          v0.1.0 · secure admin access
        </p>
        <div className="mt-1 flex justify-center">
          <SourceCodeLink />
        </div>
      </section>
    </main>
  );
}
