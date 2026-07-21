// Pure trail state machine — no DOM, no React. The DOM renderer (Task 4) and
// the WebGL renderer (Task 5) both drive an identical `createTrail()`
// instance so spawn/cull timing never diverges between the two paths.

export interface TrailItem {
  id: number;
  x: number;
  y: number;
  bornAt: number;
  imageIndex: number;
}

export interface TrailController {
  pointerMove(x: number, y: number): void; // throttled internally by spawnIntervalMs
  tick(now: number): TrailItem[]; // returns live items, culls expired
  reset(): void;
}

export function createTrail(opts: {
  max: number;
  spawnIntervalMs: number;
  lifetimeMs: number;
  imageCount: number;
}): TrailController {
  let items: TrailItem[] = [];
  let lastSpawn = 0;
  let nextId = 1;
  let cursor = 0;

  return {
    pointerMove(x, y) {
      if (opts.imageCount <= 0) return;
      const now = performance.now();
      if (now - lastSpawn < opts.spawnIntervalMs) return;
      lastSpawn = now;
      items.push({ id: nextId++, x, y, bornAt: now, imageIndex: cursor % opts.imageCount });
      cursor++;
      if (items.length > opts.max) items = items.slice(items.length - opts.max);
    },
    tick(now) {
      items = items.filter((it) => now - it.bornAt < opts.lifetimeMs);
      return items;
    },
    reset() {
      items = [];
      lastSpawn = 0;
    },
  };
}
