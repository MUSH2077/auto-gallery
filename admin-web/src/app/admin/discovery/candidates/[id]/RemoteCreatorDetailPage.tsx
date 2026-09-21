"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearchParams } from "next/navigation";
import {
  ArrowDownToLine,
  ArrowLeft,
  Bookmark,
  BriefcaseBusiness,
  Cake,
  CircleAlert,
  ExternalLink,
  Images,
  Link as LinkIcon,
  LoaderCircle,
  MapPin,
  RotateCcw,
  UserCheck,
  UserRound,
  Users,
} from "lucide-react";

import { ErrorState, PageShell, StatusBadge, useToast } from "@/components";
import { Breadcrumb } from "@/components/Breadcrumb";
import {
  ApiError,
  api,
  queryKeys,
  type DiscoveryCandidate,
  type RemoteCreatorDetail,
  type RemoteCreatorProfile,
  type RemoteWorkFeedType,
  type RemoteWorkPage,
} from "@/lib/api";
import { adminRoutes } from "@/lib/adminRoutes";
import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { runPrivateDiscoveryRequest } from "@/lib/remoteDiscoveryPrivateCache";
import ConflictResolutionDialog, {
  type ConflictResolutionValue,
} from "../../ConflictResolutionDialog";
import { localCreatorIds, safeDiscoveryError } from "../../discoveryPresentation";
import RemoteCreatorWorks from "../../RemoteCreatorWorks";

type CreatorPage = {
  candidate?: DiscoveryCandidate;
  profile?: RemoteCreatorProfile;
  works: RemoteWorkPage;
};

function safeWorkbenchReturn(raw: string | null) {
  if (!raw || !raw.startsWith("/")) return adminRoutes.discovery;
  try {
    const parsed = new URL(raw, "https://auto-gallery.invalid");
    if (parsed.origin !== "https://auto-gallery.invalid") return adminRoutes.discovery;
    if (parsed.pathname !== adminRoutes.discovery) return adminRoutes.discovery;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return adminRoutes.discovery;
  }
}

function count(profile: RemoteCreatorProfile, ...keys: string[]) {
  for (const key of keys) {
    if (profile.work_counts[key] !== undefined) return profile.work_counts[key];
  }
  return 0;
}

function Statistic({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-24 rounded-xl border border-white/10 bg-black/20 px-3 py-2 backdrop-blur-sm">
      <div className="text-lg font-semibold tabular-nums text-white">{value.toLocaleString()}</div>
      <div className="text-xs text-white/70">{label}</div>
    </div>
  );
}

function ProfileFacts({ profile }: { profile: RemoteCreatorProfile }) {
  const t = useT();
  const facts = [
    profile.public_profile.gender
      ? { icon: UserRound, label: t("discovery.profile_gender"), value: profile.public_profile.gender }
      : null,
    profile.public_profile.region
      ? { icon: MapPin, label: t("discovery.profile_region"), value: profile.public_profile.region }
      : null,
    profile.public_profile.birth_day || profile.public_profile.birth_year
      ? {
          icon: Cake,
          label: t("discovery.profile_birthday"),
          value: [profile.public_profile.birth_year, profile.public_profile.birth_day]
            .filter(Boolean)
            .join(" / "),
        }
      : null,
    profile.public_profile.job
      ? { icon: BriefcaseBusiness, label: t("discovery.profile_job"), value: profile.public_profile.job }
      : null,
  ].filter((fact): fact is NonNullable<typeof fact> => !!fact);
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {facts.map(({ icon: Icon, label, value }) => (
        <div key={label} className="flex items-start gap-3 rounded-xl border border-border bg-surface p-4">
          <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <div className="text-xs text-muted">{label}</div>
            <div className="mt-0.5 break-words text-sm font-medium text-fg">{value}</div>
          </div>
        </div>
      ))}
      {!facts.length ? (
        <p className="col-span-full rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted">
          {t("discovery.no_public_profile")}
        </p>
      ) : null}
    </div>
  );
}

export default function RemoteCreatorDetailPage() {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const userId = user?.id || 0;
  const candidateId = params.id;
  const returnPath = safeWorkbenchReturn(searchParams.get("return_to"));
  const [tab, setTab] = useState<"works" | "profile">("works");
  const [workType, setWorkType] = useState<RemoteWorkFeedType>("illust");
  const [resolveOpen, setResolveOpen] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);

  const detail = useInfiniteQuery({
    queryKey: [
      "remote-discovery-private",
      userId,
      "discovery",
      "candidate-page",
      candidateId,
      workType,
    ] as const,
    initialPageParam: null as string | null,
    enabled: userId > 0 && !!candidateId,
    retry: false,
    queryFn: async ({ pageParam, signal }): Promise<CreatorPage> => {
      if (pageParam) {
        return {
          works: await api.getDiscoveryCandidateRemoteWorks(candidateId, {
            workType,
            cursor: pageParam,
            limit: 20,
            signal,
          }),
        };
      }
      const first: RemoteCreatorDetail = await api.getDiscoveryCandidateRemoteDetail(
        candidateId,
        { workType, limit: 20, signal },
      );
      return first;
    },
    getNextPageParam: (lastPage) => lastPage.works.next_cursor || undefined,
  });

  const firstPage = detail.data?.pages[0];
  const profile = firstPage?.profile;
  const candidate = firstPage?.candidate;
  const works = useMemo(
    () => detail.data?.pages.flatMap((page) => page.works.items) || [],
    [detail.data?.pages],
  );

  const candidateAction = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "candidate-page-action"),
    mutationFn: (action: "import" | "restore") => runPrivateDiscoveryRequest(
      userId,
      (signal) => api.batchDiscoveryCandidates({ ids: [candidateId], action }, signal),
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.discovery.all(userId) });
      await detail.refetch();
      toast.success(t("discovery.batch_succeeded"));
    },
    onError: (error) => toast.error(
      safeDiscoveryError(t, error, t("discovery.batch_failed")),
    ),
  });

  const resolve = useMutation({
    mutationKey: queryKeys.discovery.mutation(userId, "candidate-page-resolve"),
    mutationFn: (value: ConflictResolutionValue) => runPrivateDiscoveryRequest(
      userId,
      (signal) => api.resolveDiscoveryCandidate(candidateId, value, signal),
    ),
    onSuccess: async () => {
      setResolveOpen(false);
      setResolveError(null);
      await queryClient.invalidateQueries({ queryKey: queryKeys.discovery.all(userId) });
      await detail.refetch();
      toast.success(t("discovery.resolve_succeeded"));
    },
    onError: (error) => setResolveError(
      safeDiscoveryError(t, error, t("discovery.resolve_failed")),
    ),
  });

  if (detail.isLoading) {
    return (
      <PageShell className="max-w-[96rem]">
        <div className="flex min-h-[28rem] items-center justify-center gap-2 text-sm text-muted" role="status">
          <LoaderCircle aria-hidden="true" className="h-5 w-5 animate-spin" />
          {t("discovery.creator_details_loading")}
        </div>
      </PageShell>
    );
  }

  if (!detail.data || !profile || !candidate) {
    return (
      <PageShell className="max-w-[96rem]">
        <Breadcrumb items={[
          { label: t("discovery.title"), href: returnPath },
          { label: t("discovery.creator_details") },
        ]} />
        <ErrorState
          message={detail.error instanceof ApiError
            ? safeDiscoveryError(t, detail.error, t("discovery.creator_details_failed"))
            : t("discovery.creator_details_failed")}
          onRetry={() => detail.refetch()}
        />
      </PageShell>
    );
  }

  const name = profile.display_name || candidate.display_name || candidate.source_creator_id;
  const avatar = profile.avatar_url || candidate.avatar_url;
  const localIds = localCreatorIds(candidate);
  const localCreatorId = localIds.length === 1 ? localIds[0] : null;

  return (
    <PageShell className="max-w-[96rem]">
      <Breadcrumb items={[
        { label: t("discovery.title"), href: returnPath },
        { label: name },
      ]} />
      <Link href={returnPath} className="btn-ghost mb-3 inline-flex">
        <ArrowLeft aria-hidden="true" className="h-4 w-4" />
        {t("discovery.back_to_workbench")}
      </Link>

      <header className="relative overflow-hidden rounded-2xl border border-border bg-gradient-to-br from-accent/30 via-subtle to-accent-subtle shadow-sm">
        <div className="absolute inset-0">
          {profile.header_image_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={profile.header_image_url}
              alt=""
              className="h-full w-full object-cover"
            />
          ) : null}
          <div className="absolute inset-0 bg-gradient-to-t from-black/90 via-black/35 to-black/10" />
        </div>
        <div className="relative flex min-h-80 flex-col justify-end px-5 pb-6 pt-24 sm:px-8">
          <div className="flex flex-col gap-5 sm:flex-row sm:items-end">
            {avatar ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={avatar}
                alt={t("discovery.avatar_alt", { name })}
                className="h-28 w-28 shrink-0 rounded-2xl border-4 border-white/85 bg-subtle object-cover shadow-xl"
              />
            ) : (
              <span className="flex h-28 w-28 shrink-0 items-center justify-center rounded-2xl border-4 border-white/85 bg-subtle text-muted shadow-xl">
                <UserRound aria-hidden="true" className="h-12 w-12" />
              </span>
            )}
            <div className="min-w-0 flex-1 text-white">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="break-words text-3xl font-bold tracking-tight sm:text-4xl">{name}</h1>
                <StatusBadge
                  status={profile.is_followed ? "up" : "unknown"}
                  label={t(profile.is_followed
                    ? "discovery.pixiv_following"
                    : "discovery.pixiv_not_following")}
                />
              </div>
              <p className="mt-1 text-sm text-white/75">
                {profile.username ? `@${profile.username} · ` : ""}
                {t("discovery.pixiv_id", { id: profile.source_creator_id })}
              </p>
              <div className="mt-4 flex flex-wrap gap-2">
                <Statistic label={t("discovery.work_type_illust")} value={count(profile, "illust", "illusts")} />
                <Statistic label={t("discovery.work_type_manga")} value={count(profile, "manga")} />
                <Statistic label={t("discovery.work_type_novel")} value={count(profile, "novel", "novels")} />
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap gap-2">
              <a href={profile.profile_url} target="_blank" rel="noreferrer" className="btn-ghost border-white/25 bg-black/30 text-white hover:bg-black/45">
                {t("discovery.open_pixiv_profile")}
                <ExternalLink aria-hidden="true" className="h-4 w-4" />
              </a>
              {localCreatorId ? (
                <Link href={adminRoutes.creator(localCreatorId)} className="btn-ghost border-white/25 bg-black/30 text-white hover:bg-black/45">
                  {t("discovery.open_local_creator")}
                </Link>
              ) : null}
              {candidate.state === "pending" ? (
                <button type="button" className="btn-primary" disabled={candidateAction.isPending} onClick={() => candidateAction.mutate("import")}>
                  <ArrowDownToLine aria-hidden="true" className="h-4 w-4" />
                  {t("discovery.import_creator")}
                </button>
              ) : null}
              {candidate.state === "conflict" ? (
                <button type="button" className="btn-primary" onClick={() => setResolveOpen(true)}>
                  <CircleAlert aria-hidden="true" className="h-4 w-4" />
                  {t("discovery.resolve_conflict")}
                </button>
              ) : null}
              {candidate.state === "dismissed" ? (
                <button type="button" className="btn-primary" disabled={candidateAction.isPending} onClick={() => candidateAction.mutate("restore")}>
                  <RotateCcw aria-hidden="true" className="h-4 w-4" />
                  {t("discovery.restore")}
                </button>
              ) : null}
            </div>
          </div>
        </div>
      </header>

      <div className="mt-5 grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-fg">
            <Users aria-hidden="true" className="h-4 w-4 text-accent" />
            {t("discovery.pixiv_following_count")}
          </div>
          <div className="mt-2 text-2xl font-semibold tabular-nums text-fg">
            {(profile.social_counts.following || 0).toLocaleString()}
          </div>
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-fg">
            <UserCheck aria-hidden="true" className="h-4 w-4 text-accent" />
            {t("discovery.pixiv_mypixiv_count")}
          </div>
          <div className="mt-2 text-2xl font-semibold tabular-nums text-fg">
            {(profile.social_counts.mypixiv || 0).toLocaleString()}
          </div>
        </div>
        <div className="rounded-xl border border-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm font-medium text-fg">
            <Bookmark aria-hidden="true" className="h-4 w-4 text-accent" />
            {t("discovery.pixiv_public_bookmarks")}
          </div>
          <div className="mt-2 text-2xl font-semibold tabular-nums text-fg">
            {(profile.social_counts.public_bookmarks || 0).toLocaleString()}
          </div>
        </div>
      </div>

      <div className="mt-6 border-b border-border" role="tablist" aria-label={t("discovery.creator_detail_tabs")}>
        <button type="button" role="tab" aria-selected={tab === "works"} className={`border-b-2 px-4 py-3 text-sm font-medium ${tab === "works" ? "border-accent text-accent" : "border-transparent text-muted hover:text-fg"}`} onClick={() => setTab("works")}>
          <Images aria-hidden="true" className="mr-2 inline h-4 w-4" />
          {t("discovery.tab_works")}
        </button>
        <button type="button" role="tab" aria-selected={tab === "profile"} className={`border-b-2 px-4 py-3 text-sm font-medium ${tab === "profile" ? "border-accent text-accent" : "border-transparent text-muted hover:text-fg"}`} onClick={() => setTab("profile")}>
          <UserRound aria-hidden="true" className="mr-2 inline h-4 w-4" />
          {t("discovery.tab_profile")}
        </button>
      </div>

      {tab === "works" ? (
        <section className="mt-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="inline-flex rounded-lg border border-border bg-surface p-1" aria-label={t("discovery.work_feed_type")}>
              {(["illust", "manga"] as const).map((type) => (
                <button
                  key={type}
                  type="button"
                  className={`rounded-md px-4 py-2 text-sm font-medium ${workType === type ? "bg-accent text-on-accent shadow-sm" : "text-muted hover:bg-subtle hover:text-fg"}`}
                  aria-pressed={workType === type}
                  onClick={() => setWorkType(type)}
                >
                  {t(`discovery.work_type_${type}`)}
                </button>
              ))}
            </div>
            <span className="text-xs tabular-nums text-muted">
              {t("discovery.loaded_works", { count: works.length })}
            </span>
          </div>
          <RemoteCreatorWorks candidate={candidate} userId={userId} works={works} />
          {detail.hasNextPage ? (
            <button type="button" className="btn-ghost mt-5 w-full justify-center" disabled={detail.isFetchingNextPage} onClick={() => detail.fetchNextPage()}>
              {detail.isFetchingNextPage ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
              {t(detail.isFetchingNextPage ? "discovery.loading_more_works" : "discovery.load_more_works")}
            </button>
          ) : null}
          {detail.error && detail.data ? (
            <div className="mt-4 rounded-xl border border-danger/30 bg-danger-subtle p-4 text-sm text-danger">
              <p>{t("discovery.works_page_failed")}</p>
              <button type="button" className="btn-ghost mt-2" onClick={() => detail.fetchNextPage()}>{t("common.retry")}</button>
            </div>
          ) : null}
        </section>
      ) : (
        <section className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(18rem,1fr)]">
          <div className="space-y-5">
            <div className="rounded-xl border border-border bg-surface p-5">
              <h2 className="text-base font-semibold text-fg">{t("discovery.profile_bio")}</h2>
              <p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-muted">
                {profile.comment || t("discovery.no_profile_bio")}
              </p>
            </div>
            <ProfileFacts profile={profile} />
          </div>
          <aside className="rounded-xl border border-border bg-surface p-5">
            <h2 className="text-base font-semibold text-fg">{t("discovery.profile_links")}</h2>
            <div className="mt-3 space-y-2">
              {profile.links.map((link) => (
                <a key={`${link.kind}-${link.url}`} href={link.url} target="_blank" rel="noreferrer" className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-3 text-sm text-fg hover:border-accent/50 hover:text-accent">
                  <span className="flex min-w-0 items-center gap-2">
                    <LinkIcon aria-hidden="true" className="h-4 w-4 shrink-0" />
                    <span className="truncate">{t(`discovery.profile_link_${link.kind}`)}</span>
                  </span>
                  <ExternalLink aria-hidden="true" className="h-4 w-4 shrink-0" />
                </a>
              ))}
              {!profile.links.length ? (
                <p className="py-6 text-center text-sm text-muted">{t("discovery.no_profile_links")}</p>
              ) : null}
            </div>
          </aside>
        </section>
      )}

      <ConflictResolutionDialog
        candidate={candidate}
        open={resolveOpen}
        pending={resolve.isPending}
        error={resolveError}
        onClose={() => {
          setResolveOpen(false);
          setResolveError(null);
        }}
        onResolve={(value) => resolve.mutate(value)}
      />
    </PageShell>
  );
}
