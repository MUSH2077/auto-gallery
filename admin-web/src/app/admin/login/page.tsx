"use client";
import { useEffect, useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import { ThemeToggle, LangToggle } from "@/lib/theme";
import SourceCodeLink from "@/components/SourceCodeLink";
import ThreeUiArcCanvas from "@/components/ThreeUiArcCanvas";
import { adminRoutes } from "@/lib/adminRoutes";
import { Eye, EyeOff, Images, ShieldCheck } from "lucide-react";

export default function LoginPage() {
  const t = useT();
  const { login, isAuthenticated, user } = useAuth();
  const router = useRouter();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!isAuthenticated) return;
    router.replace(user?.must_change_password ? adminRoutes.profile : adminRoutes.dashboard);
  }, [isAuthenticated, router, user?.must_change_password]);

  if (isAuthenticated) return null;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const authUser = await login(username, password);
      if (authUser.must_change_password) {
        router.replace(adminRoutes.profile);
      } else {
        router.replace(adminRoutes.dashboard);
      }
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : t("auth.invalid_credentials"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="relative min-h-screen overflow-hidden bg-bg px-4 py-16 text-fg sm:px-6 lg:flex lg:items-center lg:px-10 lg:py-10">
      <div className="absolute right-4 top-4 z-20 flex items-center gap-2">
        <LangToggle />
        <ThemeToggle />
      </div>

      <div className="mx-auto grid w-full max-w-6xl overflow-hidden rounded-xl border border-border bg-surface shadow-lg lg:min-h-[660px] lg:grid-cols-[minmax(0,1.18fr)_minmax(380px,.82fr)]">
        <section
          data-testid="login-hero"
          className="relative isolate min-h-60 overflow-hidden border-b border-border px-6 py-8 sm:min-h-72 sm:px-10 lg:min-h-0 lg:border-b-0 lg:border-r lg:px-12 lg:py-14"
          aria-labelledby="login-hero-title"
        >
          <ThreeUiArcCanvas className="opacity-65 dark:opacity-45" density="calm" />
          <div className="relative z-10 flex h-full max-w-xl flex-col justify-between gap-12">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-surface/90 text-accent shadow-sm">
                <Images className="h-5 w-5" aria-hidden />
              </div>
              <span className="text-sm font-semibold tracking-wide">auto-gallery</span>
            </div>
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">{t("auth.admin_panel")}</p>
              <h1 id="login-hero-title" className="mt-3 max-w-lg text-3xl font-semibold tracking-tight sm:text-4xl lg:text-5xl lg:leading-[1.08]">
                {t("auth.hero_title")}
              </h1>
              <p className="mt-4 max-w-lg text-sm leading-6 text-muted sm:text-base sm:leading-7">{t("auth.hero_desc")}</p>
            </div>
          </div>
        </section>

        <section className="flex items-center px-6 py-10 sm:px-10 lg:px-12" aria-labelledby="login-form-title">
          <div className="mx-auto w-full max-w-sm">
            <div className="mb-7">
              <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-subtle text-accent">
                <ShieldCheck className="h-5 w-5" aria-hidden />
              </div>
              <h2 id="login-form-title" className="text-2xl font-semibold tracking-tight">{t("auth.login")}</h2>
              <p className="mt-2 text-sm text-muted">{t("auth.form_desc")}</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
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
                  placeholder={t("auth.username_placeholder")}
                />
              </div>

              <div>
                <label className="mb-1.5 block text-sm font-medium" htmlFor="password">{t("auth.password")}</label>
                <div className="relative">
                  <input
                    id="password"
                    type={passwordVisible ? "text" : "password"}
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    className="input w-full pr-12"
                    placeholder="••••••••"
                  />
                  <button
                    type="button"
                    aria-controls="password"
                    aria-pressed={passwordVisible}
                    aria-label={passwordVisible ? t("auth.hide_password") : t("auth.show_password")}
                    title={passwordVisible ? t("auth.hide_password") : t("auth.show_password")}
                    onClick={() => setPasswordVisible((visible) => !visible)}
                    className="absolute inset-y-0 right-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-r-md text-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
                  >
                    {passwordVisible
                      ? <EyeOff className="h-4 w-4" aria-hidden="true" />
                      : <Eye className="h-4 w-4" aria-hidden="true" />}
                  </button>
                </div>
              </div>

              <button type="submit" disabled={loading} className="btn-primary mt-2 w-full">
                {loading ? t("auth.logging_in") : t("auth.login_button")}
              </button>
            </form>

            <p className="mt-6 text-center text-xs text-muted">
              {t("auth.secure_access", { version: "v0.1.0" })}
            </p>
            <div className="mt-1 flex justify-center">
              <SourceCodeLink />
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
