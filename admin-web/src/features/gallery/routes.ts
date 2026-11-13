import {adminRoutes} from '@/lib/adminRoutes';
export const defaultGalleryRoutes={works:adminRoutes.works,jobs:adminRoutes.jobs,login:'/admin/login'};


/** Select a useful landing page without granting access to another module. */
export function galleryLandingRoute(user: { must_change_password: boolean; is_admin?: boolean; permissions?: readonly string[] }): string {
  if (user.must_change_password) return adminRoutes.profile;
  const permissions = user.permissions || [];
  if (user.is_admin || permissions.includes("library")) return defaultGalleryRoutes.works;
  if (permissions.includes("tasks") || permissions.includes("system")) return defaultGalleryRoutes.jobs;
  if (permissions.includes("upload")) return adminRoutes.upload;
  if (permissions.includes("subscriptions")) return adminRoutes.subscriptions;
  if (permissions.includes("curation")) return adminRoutes.curation;
  return adminRoutes.profile;
}
