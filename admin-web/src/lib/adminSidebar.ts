export const LEGACY_SIDEBAR_KEY = "auto-gallery-sidebar";
export const SIDEBAR_WIDE_KEY = "auto-gallery-sidebar-wide-v2";
export const SIDEBAR_MID_KEY = "auto-gallery-sidebar-mid-v2";

export const ADMIN_SIDEBAR_EXPANDED_WIDTH = 248;
export const ADMIN_SIDEBAR_COMPACT_WIDTH = 64;

export const ADMIN_SIDEBAR_BOOTSTRAP_SCRIPT = `
(() => {
  try {
    const legacy = localStorage.getItem(${JSON.stringify(LEGACY_SIDEBAR_KEY)});
    const storedWide = localStorage.getItem(${JSON.stringify(SIDEBAR_WIDE_KEY)});
    const storedMid = localStorage.getItem(${JSON.stringify(SIDEBAR_MID_KEY)});
    const isMode = (value) => value === "expanded" || value === "compact";
    let mode = "expanded";

    if (window.matchMedia("(min-width: 768px) and (max-width: 1279px)").matches) {
      mode = isMode(storedMid) ? storedMid : "compact";
    } else if (window.matchMedia("(min-width: 1280px)").matches) {
      mode = isMode(storedWide)
        ? storedWide
        : legacy === "collapsed"
          ? "compact"
          : "expanded";
    }

    document.documentElement.style.setProperty(
      "--admin-sidebar-width",
      mode === "compact"
        ? ${ADMIN_SIDEBAR_COMPACT_WIDTH} + "px"
        : ${ADMIN_SIDEBAR_EXPANDED_WIDTH} + "px",
    );
  } catch {}
})();
`;
