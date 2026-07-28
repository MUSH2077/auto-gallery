import { getSourceBadgeColor } from "@/lib/sourceColors";

export default function SourceBadge({ source }: { source: string }) {
  return (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${getSourceBadgeColor(source)}`}>
      {source}
    </span>
  );
}
