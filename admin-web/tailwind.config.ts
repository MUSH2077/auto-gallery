import type { Config } from "tailwindcss";

// Design tokens for the themeable GitHub-grade system. Colors are backed by the
// --ag-* CSS vars (RGB triplets in globals.css) via rgb(... / <alpha-value>) so
// opacity modifiers work (e.g. ring-accent/40) and swapping the palette
// (data-theme on <html>) reskins everything. See .claude/plans/frontend-style-optimization.md.
const t = (v: string) => `rgb(var(${v}) / <alpha-value>)`;

const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        canvas: t("--ag-bg"),
        surface: t("--ag-surface"),
        subtle: t("--ag-subtle"),
        border: t("--ag-border"),
        muted: t("--ag-muted"),
        fg: t("--ag-text"),
        accent: t("--ag-accent"),
        success: t("--ag-success"),
        warning: t("--ag-warning"),
        danger: t("--ag-danger"),
        primary: t("--ag-primary"),
        "primary-hover": t("--ag-primary-hover"),
        "on-primary": t("--ag-on-primary"),
        input: t("--ag-input"),
        placeholder: t("--ag-placeholder"),
        nav: t("--ag-nav"),
        "nav-fg": t("--ag-nav-fg"),
        "accent-subtle": t("--ag-accent-subtle"),
        "danger-subtle": t("--ag-danger-subtle"),
        "warning-subtle": t("--ag-warning-subtle"),
        "success-subtle": t("--ag-success-subtle"),
      },
      boxShadow: {
        overlay:
          "0 1px 2px rgba(31,35,40,0.08), 0 8px 24px rgba(31,35,40,0.12)",
        "overlay-dark":
          "0 1px 2px rgba(1,4,9,0.6), 0 12px 32px rgba(1,4,9,0.5)",
      },
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
