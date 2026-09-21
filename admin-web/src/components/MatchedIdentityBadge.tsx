"use client";

import type { MatchedCreatorIdentity } from "@/lib/api";
import { useT } from "@/lib/i18n";

const SOURCE_LABELS: Record<string, string> = {
  pixiv: "Pixiv",
  danbooru: "Danbooru",
  x: "X",
};

export default function MatchedIdentityBadge({
  identity,
}: {
  identity?: MatchedCreatorIdentity | null;
}) {
  const t = useT();
  if (!identity) return null;

  const source = SOURCE_LABELS[identity.source] || identity.source;
  const kind = t(`search.identity_kind_${identity.kind}`);
  const rawValue = identity.kind === "account" && !identity.value.startsWith("@")
    ? `@${identity.value}`
    : identity.value;
  const message = t("search.identity_match", {
    source,
    kind,
    value: rawValue,
  });

  return (
    <span
      className="inline-flex max-w-full items-center gap-1 rounded-full border border-accent/25 bg-accent-subtle px-2 py-0.5 text-[10px] text-accent"
      title={message}
    >
      <span className="truncate">{message}</span>
      {!identity.is_current ? (
        <span className="shrink-0 rounded-full bg-surface/70 px-1 text-[9px] text-muted">
          {t("search.identity_historical")}
        </span>
      ) : null}
    </span>
  );
}
