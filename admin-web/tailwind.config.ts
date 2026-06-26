import type { Config } from "tailwindcss";

// Design tokens for the GitHub-grade system. Everything here is ADDITIVE — it
// introduces new utilities without overriding Tailwind defaults, so existing
// hardcoded hex/classes keep working. See .claude/plans/frontend-style-optimization.md.
const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  darkMode: "class",
  theme: {
    extend: {
      // Semantic colors backed by the --ag-* CSS vars (which already flip in
      // dark mode), so e.g. `bg-surface` works in both themes without `dark:`.
      colors: {
        canvas: "var(--ag-bg)",
        surface: "var(--ag-surface)",
        subtle: "var(--ag-subtle)",
        border: "var(--ag-border)",
        muted: "var(--ag-muted)",
        fg: "var(--ag-text)",
        accent: "var(--ag-accent)",
        success: "var(--ag-success)",
        warning: "var(--ag-warning)",
        danger: "var(--ag-danger)",
      },
      // Layered, transparent overlay shadow for dropdowns / dialogs / popovers.
      boxShadow: {
        overlay:
          "0 1px 2px rgba(31,35,40,0.08), 0 8px 24px rgba(31,35,40,0.12)",
        "overlay-dark":
          "0 1px 2px rgba(1,4,9,0.6), 0 12px 32px rgba(1,4,9,0.5)",
      },
      // The expo ease already used by fadeUp; standard = ease-out.
      transitionTimingFunction: {
        expo: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      transitionDuration: {
        fast: "120ms",
        base: "150ms",
        slow: "240ms",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      // Fluid display sizes (apply in P2/P4; additive, defaults untouched).
      fontSize: {
        "fluid-lg": ["clamp(1.05rem, 0.98rem + 0.4vw, 1.25rem)", { lineHeight: "1.4" }],
        "fluid-xl": ["clamp(1.25rem, 1.05rem + 1vw, 1.6rem)", { lineHeight: "1.25" }],
        "fluid-2xl": ["clamp(1.5rem, 1.2rem + 1.5vw, 2rem)", { lineHeight: "1.2" }],
      },
    },
  },
  plugins: [],
};
export default config;
