/**
 * Runs in <head> to remove the external callback query before hydration. The
 * one-shot closure waits until hydration before removing any serialized RSC
 * markers; the page then rebuilds Next's history with a clean router URL.
 */
export const X_OAUTH_CALLBACK_BOOTSTRAP_SCRIPT = String.raw`
(() => {
  try {
    if (window.location.pathname !== "/admin/discovery") return;
    const callbackUrl = new URL(window.location.href);
    const state = callbackUrl.searchParams.get("state");
    const code = callbackUrl.searchParams.get("code");
    if (!state && !code) return;

    let payload = state && code ? { state, code } : null;
    let stateMarker = state;
    let codeMarker = code;

    Object.defineProperty(window, "__consumeAutoGalleryXOAuthCallback", {
      configurable: true,
      enumerable: false,
      value: () => {
        const result = payload;
        payload = null;
        for (const script of Array.from(document.querySelectorAll("script"))) {
          const source = script.textContent || "";
          if ((stateMarker && source.includes(stateMarker)) || (codeMarker && source.includes(codeMarker))) {
            script.remove();
          }
        }
        stateMarker = null;
        codeMarker = null;
        delete window.__consumeAutoGalleryXOAuthCallback;
        return result;
      },
    });
    window.history.replaceState(null, "", "/admin/discovery");
  } catch {
    if (window.location.pathname === "/admin/discovery") {
      window.history.replaceState(null, "", "/admin/discovery");
    }
  }
})();
`;
