"use client";

import { useQuery } from "@tanstack/react-query";

import { api, queryKeys } from "@/lib/api";
import { useShowcaseConfig } from "@/lib/showcase/config";
import { useSlideshow } from "@/lib/useSlideshow";
import { ErrorState } from "@/components";
import type { SlideItem } from "@/components/SlideshowPlayer";
import ShowcaseHero from "@/components/showcase/ShowcaseHero";
import ShowcaseCanvas from "@/components/showcase/ShowcaseCanvas";
import ShowcaseEmpty from "@/components/showcase/ShowcaseEmpty";

export default function Home() {
  const { config } = useShowcaseConfig();
  const slideshow = useSlideshow();

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
      <main className="relative min-h-screen overflow-hidden bg-subtle dark:bg-canvas">
        <div className="absolute inset-0 flex items-center justify-center px-4">
          <div className="grid grid-cols-3 gap-4 sm:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="h-20 w-20 animate-pulse rounded-md bg-surface sm:h-28 sm:w-28" />
            ))}
          </div>
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
  const slideItems: SlideItem[] = items.map((item) => ({
    assetId: item.asset_id,
    workId: item.work_id,
    title: item.title,
    creatorName: item.creator_name,
  }));

  if (items.length === 0) {
    const filterActive = config.scope !== "all" || config.source != null || config.tag != null;
    return <ShowcaseEmpty variant={filterActive ? "filtered" : "library"} />;
  }

  return (
    <main className="relative min-h-screen overflow-hidden bg-subtle dark:bg-canvas">
      <ShowcaseCanvas
        items={items}
        config={config}
        onPreviewExpired={() => sample.refetch()}
        onHit={(index) => slideshow.open(slideItems, index)}
      />
      <ShowcaseHero config={config} itemCount={items.length} />
      {slideshow.node}
    </main>
  );
}
