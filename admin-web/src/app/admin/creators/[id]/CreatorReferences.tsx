"use client";

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, UserRound } from "lucide-react";

import { creatorReferencesApi } from "@/lib/api/endpoints";
import { useT } from "@/lib/i18n";

export default function CreatorReferences({
  creatorId,
  currentDisplay,
  onSelectAlias,
}: {
  creatorId: string;
  currentDisplay?: string;
  onSelectAlias: (alias: string) => void;
}) {
  const t = useT();
  const query = useQuery({
    queryKey: ["creator-references", creatorId],
    queryFn: ({ signal }) => creatorReferencesApi.getCreatorReferences(creatorId, signal),
    staleTime: 10 * 60 * 1000,
    retry: false,
  });

  if (query.isLoading) {
    return <div className="card p-4"><div className="h-24 animate-pulse rounded-md bg-subtle" /></div>;
  }
  if (query.error) {
    return (
      <div className="card p-4">
        <h3 className="text-sm font-semibold">{t("creator_detail.references")}</h3>
        <p className="mt-2 text-xs text-muted">{t("creator_detail.references_failed")}</p>
        <button type="button" className="btn-ghost mt-3" onClick={() => query.refetch()}>{t("common.retry")}</button>
      </div>
    );
  }
  const references = query.data;
  if (!references || (!references.pixiv.length && !references.danbooru)) return null;

  return (
    <section className="card space-y-5 p-4" aria-label={t("creator_detail.references")}>
      <div>
        <h3 className="text-sm font-semibold">{t("creator_detail.references")}</h3>
        <p className="mt-1 text-xs leading-5 text-muted">{t("creator_detail.references_hint")}</p>
      </div>

      {references.pixiv.length ? (
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Pixiv</h4>
          <div className="space-y-2">
            {references.pixiv.map((identity) => (
              <article key={identity.source_creator_id} className="rounded-lg border border-border bg-subtle p-3">
                <div className="flex items-start gap-3">
                  {identity.avatar_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={identity.avatar_url} alt="" className="h-11 w-11 shrink-0 rounded-lg object-cover" />
                  ) : (
                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-surface text-muted">
                      <UserRound aria-hidden="true" className="h-5 w-5" />
                    </span>
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <button
                          type="button"
                          onClick={() => onSelectAlias(identity.display_name)}
                          title={t("creator_detail.set_display_name_as", { name: identity.display_name })}
                          className={`block max-w-full truncate text-left text-sm font-medium hover:text-accent hover:underline ${currentDisplay === identity.display_name ? "text-accent" : "text-fg"}`}
                        >
                          {identity.display_name}
                        </button>
                        {identity.username ? (
                          <button
                            type="button"
                            onClick={() => onSelectAlias(identity.username!)}
                            title={t("creator_detail.set_display_name_as", { name: identity.username })}
                            className={`mt-0.5 block max-w-full truncate text-left text-xs hover:text-accent hover:underline ${currentDisplay === identity.username ? "text-accent" : "text-muted"}`}
                          >
                            @{identity.username}
                          </button>
                        ) : (
                          <p className="mt-0.5 text-xs text-muted">{t("creator_detail.pixiv_identity", { id: identity.source_creator_id })}</p>
                        )}
                      </div>
                      <a href={identity.profile_url} target="_blank" rel="noreferrer" className="text-muted hover:text-accent" aria-label={t("creator_detail.open_pixiv_reference", { name: identity.display_name })}>
                        <ExternalLink aria-hidden="true" className="h-4 w-4" />
                      </a>
                    </div>
                    {identity.status === "fallback" ? (
                      <p className="mt-2 text-[11px] text-warning">{t("creator_detail.pixiv_reference_fallback")}</p>
                    ) : null}
                  </div>
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}

      {references.danbooru ? (
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Danbooru</h4>
          <div className="rounded-lg border border-border bg-subtle p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-fg">
                  {references.danbooru.name || `Danbooru #${references.danbooru.artist_id}`}
                </p>
                <p className="mt-0.5 text-xs text-muted">{t("creator_detail.danbooru_primary_context")}</p>
              </div>
              <a href={references.danbooru.profile_url} target="_blank" rel="noreferrer" className="text-muted hover:text-accent" aria-label={t("creator_detail.open_danbooru_reference")}>
                <ExternalLink aria-hidden="true" className="h-4 w-4" />
              </a>
            </div>
            {references.danbooru.other_names.length ? (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {references.danbooru.other_names.map((alias) => (
                  <button
                    key={alias}
                    type="button"
                    onClick={() => onSelectAlias(alias)}
                    title={t("creator_detail.set_display_name_as", { name: alias })}
                    className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${currentDisplay === alias ? "border-accent bg-accent-subtle text-accent" : "border-border bg-surface text-muted hover:text-accent"}`}
                  >
                    {alias}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}
