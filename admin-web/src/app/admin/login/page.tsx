"use client";
import { useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";

export default function LoginPage() {
  const t = useT();
  const { login, isAuthenticated, user } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (isAuthenticated) {
    router.replace(user?.must_change_password ? "/admin/settings/profile" : "/admin");
    return null;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      router.replace("/admin");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t("auth.invalid_credentials"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-[#f6f8fa] px-4 py-10 text-[#24292f] dark:bg-[#0d1117] dark:text-[#e6edf3]">
      <div className="absolute right-4 top-4 flex items-center gap-2">
        <LangToggle />
        <ThemeToggle />
      </div>

      <section className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-md border border-[#d8dee4] bg-white text-lg font-semibold tracking-tight shadow-sm dark:border-[#30363d] dark:bg-[#161b22]">
            AG
          </div>
          <h1 className="text-2xl font-semibold tracking-normal">auto-gallery</h1>
          <p className="mt-1 text-sm text-[#57606a] dark:text-[#8b949e]">{t("auth.admin_panel")}</p>
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

            {error && (
              <div className="rounded-md border border-[#cf222e]/30 bg-[#ffebe9] px-3 py-2 text-sm text-[#cf222e] dark:border-[#f85149]/30 dark:bg-[#f8514926] dark:text-[#f85149]">
                {error}
              </div>
            )}

            <button type="submit" disabled={loading} className="btn-primary w-full">
              {loading ? t("auth.logging_in") : t("auth.login_button")}
            </button>
          </div>
        </form>

        <p className="mt-5 text-center text-xs text-[#57606a] dark:text-[#8b949e]">
          v0.1.0 · secure admin access
        </p>
      </section>
    </main>
  );
}
