"use client";
import { useRef, useState, FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import { authChangePassword } from "@/lib/api";
import { BusinessPage } from "@/features/gallery-ui/BusinessPage";
import Banner from "@/components/Banner";

export default function ProfilePage() {
  const t = useT();
  const { user, refreshUser } = useAuth();
  const toast = useToast();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (submitting.current) return;
    setError("");

    if (newPassword.length < 6) {
      setError(t("auth.password_too_short"));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t("auth.password_mismatch"));
      return;
    }

    submitting.current = true;
    setLoading(true);
    try {
      await authChangePassword(currentPassword, newPassword);
      await refreshUser();
      toast.success(t("auth.change_password_success"));
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t("auth.change_password_failed"));
    } finally {
      submitting.current = false;
      setLoading(false);
    }
  }

  return (
    <BusinessPage className="gallery-profile-page" title={t("auth.profile")}>

      <div className="account-content">
        {/* User Info */}
        <div className="account-section">
          <h2 className="text-sm font-medium text-muted mb-3">{t("auth.account_info")}</h2>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-accent-subtle flex items-center justify-center text-accent font-bold text-lg">
              {(user?.display_name || user?.username || "?")[0].toUpperCase()}
            </div>
            <div>
              <div className="font-medium text-fg">{user?.display_name || user?.username}</div>
              <div className="text-sm text-muted">@{user?.username}</div>
            </div>
          </div>
        </div>

        {/* Change Password */}
        <div className="account-section">
          {user?.must_change_password && (
            <Banner tone="warning" title={t("auth.force_change_title")} className="mb-4">
              {t("auth.force_change_message")}
            </Banner>
          )}
          <h2 className="text-sm font-medium text-muted mb-4">{t("auth.change_password")}</h2>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4" aria-busy={loading} aria-describedby={error ? "profile-password-error" : undefined}>
            <div>
              <label htmlFor="current-password" className="block text-sm font-medium text-fg mb-1">
                {t("auth.current_password")}
              </label>
              <input
                type="password"
                id="current-password"
                autoComplete="current-password"
                disabled={loading}
                required
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                className="input w-full"
              />
            </div>
            <div>
              <label htmlFor="new-password" className="block text-sm font-medium text-fg mb-1">
                {t("auth.new_password")}
              </label>
              <input
                type="password"
                id="new-password"
                autoComplete="new-password"
                minLength={6}
                disabled={loading}
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="input w-full"
              />
            </div>
            <div>
              <label htmlFor="confirm-password" className="block text-sm font-medium text-fg mb-1">
                {t("auth.confirm_password")}
              </label>
              <input
                type="password"
                id="confirm-password"
                autoComplete="new-password"
                minLength={6}
                disabled={loading}
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className="input w-full"
              />
            </div>

            {error && <p id="profile-password-error" role="alert" className="text-danger">{error}</p>}
            <button
              type="submit"
              disabled={loading}
              className="btn-primary min-h-11 w-full"
            >
              {loading ? t("common.saving") : t("auth.change_password")}
            </button>
          </form>
        </div>
      </div>
    </BusinessPage>
  );
}
