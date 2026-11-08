export type Theme = "light" | "dark";
export const DEFAULT_THEME: Theme = "light";
export const THEME_STORAGE_KEY = "auto-gallery-theme";

// Apply a saved choice before the first paint; an unset theme stays light.
export const THEME_BOOTSTRAP_SCRIPT = `
(() => {
  let theme = ${JSON.stringify(DEFAULT_THEME)};
  try {
    const stored = localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});
    if (stored === "light" || stored === "dark") theme = stored;
  } catch {}
  const dark = theme === "dark";
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
})();
`;
