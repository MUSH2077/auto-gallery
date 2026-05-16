const sourceColors: Record<string, string> = {
  // Downloadable sources
  pixiv: "bg-blue-100 text-blue-700",
  iwara: "bg-pink-100 text-pink-700",
  // Pixiv sub-types
  pixiv_sketch: "bg-sky-100 text-sky-700",
  pixiv_stacc: "bg-indigo-100 text-indigo-700",
  // Social
  x: "bg-gray-900 text-white",
  bluesky: "bg-sky-500 text-white",
  misskey: "bg-purple-100 text-purple-700",
  mastodon: "bg-indigo-100 text-indigo-700",
  // Art platforms
  danbooru: "bg-yellow-100 text-yellow-700",
  danbooru_reference: "bg-yellow-100 text-yellow-700",
  deviantart: "bg-green-200 text-green-800",
  artstation: "bg-blue-200 text-blue-800",
  // Video
  youtube: "bg-red-100 text-red-700",
  bilibili: "bg-pink-100 text-pink-700",
  nicovideo: "bg-gray-100 text-gray-700",
  vimeo: "bg-teal-100 text-teal-700",
  // Chinese platforms
  weibo: "bg-red-100 text-red-700",
  xiaohongshu: "bg-red-200 text-red-800",
  bcy: "bg-pink-200 text-pink-800",
  lofter: "bg-green-100 text-green-700",
  // Funding/Shop
  fanbox: "bg-orange-100 text-orange-700",
  skeb: "bg-cyan-100 text-cyan-700",
  patreon: "bg-orange-200 text-orange-800",
  boosty: "bg-amber-100 text-amber-700",
  gumroad: "bg-blue-100 text-blue-700",
  fantia: "bg-rose-100 text-rose-700",
  // Social media
  instagram: "bg-pink-100 text-pink-700",
  tumblr: "bg-indigo-100 text-indigo-700",
  facebook: "bg-blue-200 text-blue-800",
  tiktok: "bg-gray-900 text-white",
  threads: "bg-gray-800 text-white",
  reddit: "bg-orange-200 text-orange-800",
  // Link aggregators
  linktree: "bg-green-100 text-green-700",
  carrd: "bg-purple-100 text-purple-700",
  aboutme: "bg-teal-100 text-teal-700",
  // Generic
  website: "bg-gray-100 text-gray-600",
  local: "bg-green-100 text-green-700",
  manual: "bg-purple-100 text-purple-700",
};

export default function SourceBadge({ source }: { source: string }) {
  return (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${sourceColors[source] || "bg-gray-100 text-gray-600"}`}>
      {source}
    </span>
  );
}
