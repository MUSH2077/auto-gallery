/** Restore only an application page after sign-in; password changes take priority. */
export function loginDestination(raw: string | null, mustChangePassword = false): string {
  if (mustChangePassword) return "/admin/profile";
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || /[\\\u0000-\u001f\u007f]/.test(raw)) return "/admin";
  try {
    const target = new URL(raw, "https://auto-gallery.invalid");
    const path = decodeURIComponent(target.pathname);
    if (target.origin !== "https://auto-gallery.invalid" || /[\\\u0000-\u001f\u007f]/.test(path)) return "/admin";
    if (path === "/admin/login" || path.startsWith("/admin/login/")) return "/admin";
    if (path !== "/" && path !== "/admin" && !path.startsWith("/admin/")) return "/admin";
    return target.pathname + target.search + target.hash;
  } catch {
    return "/admin";
  }
}
