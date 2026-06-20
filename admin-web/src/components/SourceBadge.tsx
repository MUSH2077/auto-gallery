import Link from "next/link";
import { getSourceBadgeColor } from "@/lib/sourceColors";

export default function SourceBadge({ source, href }: { source: string; href?: string }) {
  const badge = (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${getSourceBadgeColor(source)}`}>
      {source}
    </span>
  );
  if (href) {
    return <Link href={href} onClick={(e) => e.stopPropagation()} className="inline-flex">{badge}</Link>;
  }
  return badge;
}
