"use client";
import { useState, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { ThemeToggle, LangToggle } from "@/lib/theme";

export default function LoginPage() {
  const t = useT();
  const { login, isAuthenticated } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  if (isAuthenticated) { router.replace("/admin"); return null; }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault(); setError(""); setLoading(true);
    try { await login(username, password); router.replace("/admin"); }
    catch (err: unknown) { setError(err instanceof Error ? err.message : t("auth.invalid_credentials")); }
    finally { setLoading(false); }
  }

  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-[#050508] px-4 relative overflow-hidden"
      style={{ fontFamily: "'JetBrains Mono', monospace" }}>
      {/* CRT overlay + scanline */}
      <div className="crt-overlay" />
      <div className="scanline-bar" />

      {/* Background wireframe geometry */}
      <svg className="absolute inset-0 w-full h-full opacity-[0.04] pointer-events-none" viewBox="0 0 1200 800">
        <defs>
          <linearGradient id="wireGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#00f0ff" />
            <stop offset="100%" stopColor="#ff006e" />
          </linearGradient>
        </defs>
        {Array.from({ length: 20 }).map((_, i) => (
          <line key={`h${i}`} x1={i * 60} y1={0} x2={i * 60 + 800} y2={800} stroke="url(#wireGrad)" strokeWidth="0.5" />
        ))}
        {Array.from({ length: 20 }).map((_, i) => (
          <line key={`v${i}`} x1={i * 60 + 200} y1={0} x2={i * 60 - 200} y2={800} stroke="url(#wireGrad)" strokeWidth="0.5" />
        ))}
      </svg>

      {/* Top-right controls */}
      <div className="absolute top-4 right-4 flex items-center gap-2 z-10">
        <LangToggle />
        <ThemeToggle />
      </div>

      <div className="w-full max-w-md relative z-10">
        {/* Brand */}
        <div className="text-center mb-10">
          <h1 className="text-4xl font-bold tracking-[0.2em] neon-cyan" style={{ fontFamily: "'JetBrains Mono', monospace" }}>
            AUTO<span className="neon-pink">_</span>GALLERY
          </h1>
          <p className="text-[#00f0ff]/40 text-xs mt-3 tracking-[0.3em] uppercase">
            <span className="cursor-blink">▌</span> {t("auth.admin_panel")}
          </p>
        </div>

        {/* Terminal window */}
        <div className="bg-[#0a0a10]/90 backdrop-blur border border-[#00f0ff]/20 rounded-lg overflow-hidden shadow-[0_0_60px_rgba(0,240,255,0.05)]">
          {/* Title bar */}
          <div className="flex items-center gap-2 px-4 py-2.5 bg-[#0d0d18] border-b border-[#00f0ff]/10">
            <div className="flex gap-1.5">
              <div className="w-3 h-3 rounded-full bg-[#ff006e]/60" />
              <div className="w-3 h-3 rounded-full bg-[#ff006e]/30" />
              <div className="w-3 h-3 rounded-full bg-[#ff006e]/10" />
            </div>
            <span className="text-[10px] text-[#00f0ff]/30 ml-2 tracking-wider">terminal — ssh admin@auto-gallery</span>
          </div>

          <form onSubmit={handleSubmit} className="p-6 space-y-5">
            {/* Username */}
            <div>
              <label className="flex items-center gap-2 text-xs text-[#00f0ff]/60 mb-2 tracking-wider uppercase">
                <span className="text-[#ff006e]">$</span> {t("auth.username")}
              </label>
              <input type="text" autoComplete="username" required value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full px-3 py-2.5 bg-[#050508] border border-[#00f0ff]/15 rounded text-[#00f0ff] placeholder-[#00f0ff]/15
                  focus:outline-none neon-border-cyan text-sm transition-all duration-200"
                style={{ fontFamily: "'JetBrains Mono', monospace" }}
                placeholder="admin" />
            </div>

            {/* Password */}
            <div>
              <label className="flex items-center gap-2 text-xs text-[#00f0ff]/60 mb-2 tracking-wider uppercase">
                <span className="text-[#ff006e]">$</span> {t("auth.password")}
              </label>
              <input type="password" autoComplete="current-password" required value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2.5 bg-[#050508] border border-[#00f0ff]/15 rounded text-[#00f0ff] placeholder-[#00f0ff]/15
                  focus:outline-none neon-border-cyan text-sm transition-all duration-200"
                style={{ fontFamily: "'JetBrains Mono', monospace" }}
                placeholder="••••••••" />
            </div>

            {/* Error */}
            {error && (
              <div className="border border-[#ff006e]/20 bg-[#ff006e]/5 rounded px-3 py-2 text-xs text-[#ff006e] flex items-center gap-2">
                <span>⚠</span> {error}
              </div>
            )}

            {/* Submit */}
            <button type="submit" disabled={loading}
              className="w-full py-2.5 border border-[#00f0ff]/40 bg-[#00f0ff]/5 text-[#00f0ff] rounded
                hover:bg-[#00f0ff]/10 hover:border-[#00f0ff]/60 hover:shadow-[0_0_20px_rgba(0,240,255,0.15)]
                disabled:opacity-30 disabled:cursor-not-allowed
                transition-all duration-200 text-sm tracking-wider uppercase font-medium"
              style={{ fontFamily: "'JetBrains Mono', monospace" }}>
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <span className="w-2 h-2 bg-[#00f0ff] rounded-full animate-ping" />
                  {t("auth.logging_in")}
                </span>
              ) : (
                <span className="flex items-center justify-center gap-2">
                  <span className="text-[#ff006e]">▶</span> {t("auth.login_button")}
                </span>
              )}
            </button>
          </form>
        </div>

        {/* Footer */}
        <p className="text-center mt-6 text-[10px] text-[#00f0ff]/20 tracking-widest uppercase">
          <span className="text-[#ff006e]/40">v</span>0.1.0 — secure terminal access
        </p>
      </div>
    </div>
  );
}
