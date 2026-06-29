"use client";
import { useState, FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { useToast } from "@/components/Toast";
import { authChangePassword } from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import Banner from "@/components/Banner";

export default function ProfilePage() {
  const t = useT();
  const { user, updateAccessToken } = useAuth();
  const toast = useToast();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (newPassword.length < 6) {
      toast.error(t("auth.password_too_short"));
      return;
    }
    if (newPassword !== confirmPassword) {
      toast.error(t("auth.password_mismatch"));
      return;
    }

    setLoading(true);
    try {
      const refreshed = await authChangePassword(currentPassword, newPassword);
      await updateAccessToken(refreshed.access_token);
      toast.success(t("auth.change_password_success"));
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : t("auth.change_password_failed"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="p-4 sm:p-6 max-w-xl mx-auto">
      <PageHeader title={t("auth.profile")} />

      {/* User Info */}
      <div className="card p-5 mb-6">
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
      <div className="card p-5">
        {user?.must_change_password && (
          <Banner tone="warning" title={t("auth.force_change_title")} className="mb-4">
            {t("auth.force_change_message")}
          </Banner>
        )}
        <h2 className="text-sm font-medium text-muted mb-4">{t("auth.change_password")}</h2>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div>
            <label className="block text-sm font-medium text-fg mb-1">
              {t("auth.current_password")}
            </label>
            <input
              type="password"
              autoComplete="current-password"
              required
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              className="input w-full"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-fg mb-1">
              {t("auth.new_password")}
            </label>
            <input
              type="password"
              autoComplete="new-password"
              required
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="input w-full"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-fg mb-1">
              {t("auth.confirm_password")}
            </label>
            <input
              type="password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="input w-full"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 px-4 bg-accent hover:bg-accent/90 disabled:bg-accent text-white font-medium rounded-lg transition-colors text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            {loading ? t("common.saving") : t("auth.change_password")}
          </button>
        </form>
      </div>
    </div>
  );
}
