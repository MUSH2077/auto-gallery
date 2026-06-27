"use client";
import { useState, FormEvent } from "react";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { authChangePassword } from "@/lib/api";
import PageHeader from "@/components/PageHeader";

export default function ProfilePage() {
  const t = useT();
  const { user, updateAccessToken } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSuccess(false);

    if (newPassword.length < 6) {
      setError(t("auth.password_too_short"));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t("auth.password_mismatch"));
      return;
    }

    setLoading(true);
    try {
      const refreshed = await authChangePassword(currentPassword, newPassword);
      await updateAccessToken(refreshed.access_token);
      setSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t("auth.change_password_failed"));
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
          <div className="w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center text-blue-700 dark:text-blue-300 font-bold text-lg">
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
          <div className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-900/20 dark:text-amber-300">
            <div className="font-medium">{t("auth.force_change_title")}</div>
            <div>{t("auth.force_change_message")}</div>
          </div>
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

          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
              {error}
            </div>
          )}
          {success && (
            <div className="text-sm text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg px-3 py-2">
              {t("auth.change_password_success")}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 px-4 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white font-medium rounded-lg transition-colors text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {loading ? t("common.saving") : t("auth.change_password")}
          </button>
        </form>
      </div>
    </div>
  );
}
