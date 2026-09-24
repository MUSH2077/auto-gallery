"use client";
import dynamic from "next/dynamic";
import { useEffect, useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { adminRoutes } from "@/lib/adminRoutes";
import { SOURCE_CODE_URL } from "@/lib/sourceCode";
import { clearToken, loadUser, loginAndLoadUser, saveToken, storedToken } from "@/lib/authFlow";
import loginCopy from "@/lib/locales/login.json";
import { Code2, Eye, EyeOff, Globe2, Images, Monitor, Moon, ShieldCheck, Sun } from "lucide-react";

const ThreeUiArcCanvas = dynamic(() => import("@/components/ThreeUiArcCanvas"), {
  ssr: false,
});

type Lang = "zh" | "en";
type Theme = "light" | "dark" | "system";

const COPY = loginCopy as Record<Lang, typeof loginCopy.zh>;

function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function LoginPage() {
  const router = useRouter();
  const [lang, setLang] = useState<Lang>("zh");
  const [theme, setTheme] = useState<Theme>("system");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const copy = COPY[lang];

  useEffect(() => {
    try {
      const storedLang = localStorage.getItem("auto-gallery-lang");
      if (storedLang === "zh" || storedLang === "en") setLang(storedLang);
      const storedTheme = localStorage.getItem("auto-gallery-theme");
      const initialTheme = storedTheme === "light" || storedTheme === "dark" || storedTheme === "system"
        ? storedTheme
        : "system";
      setTheme(initialTheme);
      document.documentElement.classList.toggle("dark", resolveTheme(initialTheme) === "dark");
    } catch {}

    const token = storedToken();
    if (!token) return;
    loadUser(token)
      .then((user) => {
        router.replace(user.must_change_password ? adminRoutes.profile : adminRoutes.dashboard);
      })
      .catch(() => clearToken());
  }, [router]);

  const changeLang = () => {
    const next = lang === "zh" ? "en" : "zh";
    setLang(next);
    try { localStorage.setItem("auto-gallery-lang", next); } catch {}
  };

  const changeTheme = () => {
    const themes: Theme[] = ["light", "dark", "system"];
    const next = themes[(themes.indexOf(theme) + 1) % themes.length];
    setTheme(next);
    document.documentElement.classList.toggle("dark", resolveTheme(next) === "dark");
    try { localStorage.setItem("auto-gallery-theme", next); } catch {}
  };

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const { token, user: authUser } = await loginAndLoadUser(username, password);
      saveToken(token);
      if (authUser.must_change_password) {
        router.replace(adminRoutes.profile);
      } else {
        router.replace(adminRoutes.dashboard);
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : copy.invalidCredentials);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="relative min-h-screen overflow-hidden bg-bg px-4 py-16 text-fg sm:px-6 lg:flex lg:items-center lg:px-10 lg:py-10">
      <div className="absolute right-4 top-4 z-20 flex items-center gap-2">
        <button type="button" onClick={changeLang} className="btn-icon relative" title={copy.switchLanguage} aria-label={copy.switchLanguage}>
          <Globe2 className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
          <span className="absolute -bottom-0.5 -right-0.5 rounded bg-surface px-0.5 text-[8px] font-bold leading-3 text-muted" aria-hidden>{lang === "zh" ? "EN" : "中"}</span>
        </button>
        <button type="button" onClick={changeTheme} className="btn-icon" aria-label={copy.theme[theme]} title={copy.theme[theme]}>
          {theme === "dark" ? <Moon className="h-[18px] w-[18px]" aria-hidden /> : theme === "system" ? <Monitor className="h-[18px] w-[18px]" aria-hidden /> : <Sun className="h-[18px] w-[18px]" aria-hidden />}
        </button>
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
              <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">{copy.adminPanel}</p>
              <h1 id="login-hero-title" className="mt-3 max-w-lg text-3xl font-semibold tracking-tight sm:text-4xl lg:text-5xl lg:leading-[1.08]">
                {copy.heroTitle}
              </h1>
              <p className="mt-4 max-w-lg text-sm leading-6 text-muted sm:text-base sm:leading-7">{copy.heroDesc}</p>
            </div>
          </div>
        </section>

        <section className="flex items-center px-6 py-10 sm:px-10 lg:px-12" aria-labelledby="login-form-title">
          <div className="mx-auto w-full max-w-sm">
            <div className="mb-7">
              <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-subtle text-accent">
                <ShieldCheck className="h-5 w-5" aria-hidden />
              </div>
              <h2 id="login-form-title" className="text-2xl font-semibold tracking-tight">{copy.login}</h2>
              <p className="mt-2 text-sm text-muted">{copy.formDesc}</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="mb-1.5 block text-sm font-medium" htmlFor="username">{copy.username}</label>
                <input
                  id="username"
                  type="text"
                  autoComplete="username"
                  required
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="input w-full"
                  placeholder={copy.usernamePlaceholder}
                />
              </div>

              <div>
                <label className="mb-1.5 block text-sm font-medium" htmlFor="password">{copy.password}</label>
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
                    aria-label={passwordVisible ? copy.hidePassword : copy.showPassword}
                    title={passwordVisible ? copy.hidePassword : copy.showPassword}
                    onClick={() => setPasswordVisible((visible) => !visible)}
                    className="absolute inset-y-0 right-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-r-md text-muted transition-colors hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
                  >
                    {passwordVisible
                      ? <EyeOff className="h-4 w-4" aria-hidden="true" />
                      : <Eye className="h-4 w-4" aria-hidden="true" />}
                  </button>
                </div>
              </div>

              {error && <p role="alert" className="text-sm text-danger">{error}</p>}

              <button type="submit" disabled={loading} className="btn-primary mt-2 w-full">
                {loading ? copy.loggingIn : copy.loginButton}
              </button>
            </form>

            <p className="mt-6 text-center text-xs text-muted">
              {copy.secureAccess}
            </p>
            <div className="mt-1 flex justify-center">
              <a href={SOURCE_CODE_URL} target="_blank" rel="noreferrer" aria-label={copy.sourceCode} title={copy.sourceCode} data-testid="source-code-link" className="inline-flex min-h-11 items-center gap-2 rounded-md px-3 text-xs font-medium text-muted transition-colors hover:bg-subtle hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                <Code2 className="h-[18px] w-[18px]" strokeWidth={1.8} aria-hidden />
                <span>{copy.sourceCode}</span>
              </a>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
