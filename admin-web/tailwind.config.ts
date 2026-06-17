import type { Config } from "tailwindcss";
const config: Config = {
  content: ["./src/**/*.{js,ts,jsx,tsx,mdx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        ag: {
          bg: "var(--ag-bg)",
          surface: "var(--ag-surface)",
          subtle: "var(--ag-subtle)",
          border: "var(--ag-border)",
          muted: "var(--ag-muted)",
          text: "var(--ag-text)",
          accent: "var(--ag-accent)",
          success: "var(--ag-success)",
          warning: "var(--ag-warning)",
          danger: "var(--ag-danger)",
          focus: "var(--ag-focus)",
        },
      },
    },
  },
  plugins: [],
};
export default config;
