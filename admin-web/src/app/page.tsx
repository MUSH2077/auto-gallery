"use client";

import { useQuery } from "@tanstack/react-query";

import { api, queryKeys } from "@/lib/api";
import { useShowcaseConfig } from "@/lib/showcase/config";
import { ErrorState } from "@/components";
import ShowcaseHero from "@/components/showcase/ShowcaseHero";
import ShowcaseTrailDOM from "@/components/showcase/ShowcaseTrailDOM";
import ShowcaseEmpty from "@/components/showcase/ShowcaseEmpty";

export default function Home() {
  const { config } = useShowcaseConfig();

  const params = {
    count: 24,
    scope: config.scope,
    source: config.source,
    tag: config.tag,
    include_nsfw: config.includeNsfw,
  };

  const sample = useQuery({
    queryKey: queryKeys.showcase.sample(params),
    queryFn: () => api.showcaseSample(params),
    staleTime: 5 * 60_000,
  });

  if (sample.isLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-subtle px-4 dark:bg-canvas">
        <div className="grid grid-cols-3 gap-4 sm:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="h-20 w-20 animate-pulse rounded-md bg-surface sm:h-28 sm:w-28" />
          ))}
        </div>
      </main>
    );
  }

  if (sample.error) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-subtle px-4 dark:bg-canvas">
        <div className="w-full max-w-md">
          <ErrorState message={(sample.error as Error).message} onRetry={() => sample.refetch()} />
        </div>
      </main>
    );
  }

  const items = sample.data?.items ?? [];

  if (items.length === 0) {
    return <ShowcaseEmpty />;
  }

  return (
    <main className="relative min-h-screen overflow-hidden bg-subtle dark:bg-canvas">
      <ShowcaseTrailDOM items={items} config={config} />
      <ShowcaseHero config={config} itemCount={items.length} />
    </main>
  );
}
