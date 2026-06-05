// Canonical source colors — single source of truth for all source-specific UI.
// Must be consistent across the entire admin system.
// Hex colors aligned with the gallery-dl settings page tab colors.

export const SOURCE_COLORS: Record<string, string> = {
  // Downloadable sources
  pixiv: "#0066FF",
  iwara: "#EC4899",       // pink-500, matches gallery-dl tab
  x: "#6B7280",           // gray-500, matches gallery-dl tab
  twitter: "#6B7280",     // alias for x
  danbooru: "#B45309",    // amber-700, matches gallery-dl tab
  pinterest: "#EF4444",   // red-500, matches gallery-dl tab
  lofter: "#14B8A6",      // teal-500, matches gallery-dl tab
  weibo: "#F97316",       // orange-500, matches gallery-dl tab
  bilibili: "#06B6D4",    // cyan-500, matches gallery-dl tab

  // Reference
  danbooru_reference: "#B45309",

  // Non-downloadable
  local: "#6B7280",
  manual: "#6B7280",

  // Fallback for unknown sources
  unknown: "#9CA3AF",
};

// Tailwind background + text classes per source (for badges/chips)
export const SOURCE_BADGE_COLORS: Record<string, string> = {
  pixiv: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  iwara: "bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400",
  x: "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400",
  twitter: "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400",
  danbooru: "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400",
  danbooru_reference: "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400",
  pinterest: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  lofter: "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
  weibo: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  bilibili: "bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400",
  local: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  manual: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",

  // Fallback
  pixiv_sketch: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  pixiv_stacc: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  bluesky: "bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400",
  misskey: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
  mastodon: "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400",
  youtube: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  deviantart: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  artstation: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  nicovideo: "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400",
  vimeo: "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
  xiaohongshu: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  bcy: "bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400",
  fanbox: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  skeb: "bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400",
  patreon: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  boosty: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400",
  gumroad: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  fantia: "bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400",
  instagram: "bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400",
  tumblr: "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400",
  facebook: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400",
  tiktok: "bg-gray-900 text-white dark:bg-gray-700 dark:text-white",
  threads: "bg-gray-800 text-white dark:bg-gray-700 dark:text-white",
  reddit: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
  linktree: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  carrd: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400",
  aboutme: "bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400",
  website: "bg-gray-100 text-gray-600 dark:bg-gray-900/30 dark:text-gray-400",
};

export function getSourceColor(source: string): string {
  return SOURCE_COLORS[source.toLowerCase()] || SOURCE_COLORS["unknown"];
}

export function getSourceBadgeColor(source: string): string {
  return SOURCE_BADGE_COLORS[source] || "bg-gray-100 text-gray-600 dark:bg-gray-900/30 dark:text-gray-400";
}

// Consistent chart color palette (ordered for visual distinction)
export const CHART_COLORS = ["#0066FF", "#EC4899", "#B45309", "#EF4444", "#14B8A6", "#F97316", "#06B6D4", "#6B7280", "#1DA1F2", "#E60023"];
