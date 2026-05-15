const sourceColors: Record<string, string> = {
  pixiv: "bg-blue-100 text-blue-700",
  iwara: "bg-pink-100 text-pink-700",
  x: "bg-gray-900 text-white",
  danbooru_reference: "bg-yellow-100 text-yellow-700",
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
