// Pure infinite-strip layout for the showcase gallery — no WebGL, no DOM, so
// the hit-test and wrap math are unit-testable in isolation (a hit-test bug
// is otherwise invisible until a user clicks the wrong image).

export interface GalleryImage {
  url: string;
  width: number | null;
  height: number | null;
}

export interface PlaneLayout {
  index: number;
  basePosX: number;
  planeW: number;
  planeH: number;
}

/** Lay images out left-to-right at a fixed height, widths from real aspect (clamped), centered on their slot. */
export function computeStrip(
  images: GalleryImage[],
  opts: { planeH: number; gap: number; aspectClamp: [number, number] },
): { planes: PlaneLayout[]; totalWidth: number } {
  const [minAspect, maxAspect] = opts.aspectClamp;
  const planes: PlaneLayout[] = [];
  let cursor = 0;
  images.forEach((img, index) => {
    const aspect =
      img.width && img.height && img.height > 0
        ? Math.min(maxAspect, Math.max(minAspect, img.width / img.height))
        : 1;
    const planeW = opts.planeH * aspect;
    planes.push({ index, basePosX: cursor + planeW / 2, planeW, planeH: opts.planeH });
    cursor += planeW + opts.gap;
  });
  return { planes, totalWidth: cursor };
}

/** Wrap a base position by the scroll offset into a centered [-total/2, total/2) range for seamless looping. */
export function screenX(basePosX: number, scroll: number, totalWidth: number): number {
  if (totalWidth <= 0) return basePosX - scroll;
  let x = (((basePosX - scroll) % totalWidth) + totalWidth) % totalWidth;
  if (x > totalWidth / 2) x -= totalWidth;
  return x;
}

/** Index of the plane whose wrapped screen span contains `px`, or null. `viewportCenterX` maps world 0 to screen. */
export function hitTestStrip(
  planes: PlaneLayout[],
  scroll: number,
  totalWidth: number,
  viewportCenterX: number,
  px: number,
): number | null {
  for (const p of planes) {
    const centerScreen = viewportCenterX + screenX(p.basePosX, scroll, totalWidth);
    if (px >= centerScreen - p.planeW / 2 && px <= centerScreen + p.planeW / 2) return p.index;
  }
  return null;
}
