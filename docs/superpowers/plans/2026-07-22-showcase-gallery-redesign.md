# Showcase Gallery Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the showcase homepage's mouse-follow WebGL trail with a makemepulse-style horizontal auto-scrolling perspective gallery — large aspect-correct planes, velocity-driven curve, infinite loop, click-to-open-slideshow — fixing the four live defects (upside-down, tiny/squished, ghosting, wobble) in the process.

**Architecture:** Reuse the existing sample endpoint, ogl lazy-load, degradation-contract skeleton, preference plumbing, and slideshow. Rewrite only the visual layer: `webgl.ts` becomes a gallery renderer (aspect-cover fragment shader, `imageOrientation:"flipY"` bitmaps, velocity-driven Z-bend vertex shader, infinite-strip layout, screen-space hit-test); `ShowcaseCanvas` drives auto-scroll + wheel/drag with dt-independent lerp; the DOM fallback becomes a CSS drift band + static large-image grid.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async; Next.js 14 App Router + TypeScript; TanStack Query; `ogl` (existing, lazy); existing `src/lib/motion` + `src/lib/showcase` primitives.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-22-showcase-gallery-redesign-design.md` — normative for behavior, config shape, degradation, and acceptance.
- **Task ordering differs from spec §12 for dependency-correctness:** config gallery-fields are ADDED early (Task 2, additively — old trail fields kept) so the renderer/canvas compile against them; the old trail fields are REMOVED and the settings UI migrated late (Task 6, after all consumers use the new fields). Every task boundary must leave `tsc`/`build` green.
- Motion discipline (from `docs/frontend-motion-audit.md`, non-negotiable): animate only `transform`/`opacity` in DOM; `motionConfig.shouldAnimate()` gates hardware at the source (when false, no rAF loop, no `import("ogl")`); rAF pauses on `document.hidden`; cancel rAF and remove every listener on unmount, including while hidden.
- Degradation contract (four rules, all mandatory): `prefers-reduced-motion` OR `config.minimal` → static large-image grid, **ogl never fetched**; low-end (`hardwareConcurrency <= 4`) OR WebGL create-fail OR `webglcontextlost` → `ShowcaseGalleryDOM` (CSS drift), silent, one-way WebGL→DOM; `document.hidden` → pause rAF. The WebGL-vs-DOM decision is made once at mount (`hardwareOk` frozen) except `config.minimal` which is live, and `fellBack` which is one-way.
- `ogl` reachable ONLY through the dynamic `import("ogl")` inside `webgl.ts`, so it stays in its own content-hashed lazy chunk. Verify after building with a distinctive token (`OES_texture_float` / `attributeOrder`) — grepping the bare string "ogl" false-positives on `GOOGLE_FONT_PROVIDER` in the shared chunk.
- Frame-rate independence: reuse `frameIndependentAlpha` from `src/lib/showcase/smoothing.ts` (renamed from `trailTiming.ts` in Task 3) with its `MAX_DT_MS` clamp for the scroll lerp — one formula, no reimplementation.
- Every user-visible string through `t()` with the key present in BOTH the `zh` and `en` dicts in `admin-web/src/lib/i18n.tsx`; prefer `t("key")` with no fallback so a missing key cannot hide (this class of bug shipped once before). zh is primary — natural Simplified Chinese, idiomatic English.
- Config: control min/max/step must equal the sanitizer clamp exactly and `(max-min)/step` must be an integer, so a slider notch can never emit a value the sanitizer rewrites.
- Backend tests: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest <path> -q`. Known unrelated flake: `tests/test_disk_import.py::test_reconcile_downloads_to_db_registers_and_enqueues_idempotently`.
- Frontend verify: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build`. "First Load JS shared by all" must not grow (currently 87.2–87.3 kB).
- **Environment:** admin-web is served on host port **13000** again (the user restored the mount and freed the port). Use `http://127.0.0.1:13000` for manual/browser verification. If a deploy binds the wrong port, the compose default is 13000 (`ADMIN_WEB_PORT` is unset/commented in `.env`).
- Deploy: `docker compose build --build-arg CACHEBUST="$(date +%s)" <svc> && docker compose up -d --force-recreate <svc>`.
- Commit messages end with exactly:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```

---

## File Structure

**Backend**
| File | Change |
|---|---|
| `backend/app/schemas/showcase.py` | add `asset_id: str` to `ShowcaseItem` |
| `backend/app/api/showcase.py` | populate `asset_id=aid` when building items |
| `backend/tests/test_showcase_api.py` | assert `asset_id` present and equals the thumbnail asset id |

**Frontend**
| File | Change |
|---|---|
| `admin-web/src/lib/api/types.ts` | add `asset_id: string` to TS `ShowcaseItem` |
| `admin-web/src/lib/showcase/config.tsx` | add gallery fields (Task 2), remove trail fields (Task 6) |
| `admin-web/src/lib/showcase/smoothing.ts` (rename from `trailTiming.ts`) | keep `frameIndependentAlpha`/`REFERENCE_FRAME_MS`/`MAX_DT_MS`; drop trail-specific exports |
| `admin-web/src/lib/showcase/trail.ts` | DELETE (`createTrail` obsolete) |
| `admin-web/src/lib/showcase/webgl.ts` | REWRITE: gallery renderer |
| `admin-web/src/lib/showcase/galleryLayout.ts` (new) | pure infinite-strip layout math — testable without WebGL |
| `admin-web/src/components/showcase/ShowcaseCanvas.tsx` | REWRITE: auto-scroll + wheel/drag + degradation |
| `admin-web/src/components/showcase/ShowcaseTrailDOM.tsx` | DELETE |
| `admin-web/src/components/showcase/ShowcaseGalleryDOM.tsx` (new) | CSS `translateX` drift band (low-end/WebGL-fail) |
| `admin-web/src/components/showcase/ShowcaseStaticGrid.tsx` (new) | static large aspect-correct grid (reduced-motion/minimal) |
| `admin-web/src/app/page.tsx` | pass gallery config + slideshow node |
| `admin-web/src/app/admin/settings/showcase/page.tsx` | migrate 动效 controls (Task 6) |
| `admin-web/src/lib/i18n.tsx` | migrate 动效 keys (Task 6) |

---

### Task 1: Backend `asset_id` on the sample item

**Files:**
- Modify: `backend/app/schemas/showcase.py`
- Modify: `backend/app/api/showcase.py` (item build, ~line 106)
- Modify: `backend/tests/test_showcase_api.py`
- Modify: `admin-web/src/lib/api/types.ts:970-979`

**Interfaces:**
- Produces: `ShowcaseItem.asset_id: str` — the thumbnail asset's id, used by the frontend to build `SlideItem.assetId` for click-to-slideshow.

- [ ] **Step 1: Extend the failing test**

In `backend/tests/test_showcase_api.py`, inside `test_sample_hides_nsfw_from_restricted_user_and_signs_preview_urls`, after the existing `thumb_url` assertion add:

```python
            assert item["asset_id"] == str(asset.id)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_showcase_api.py -q`
Expected: FAIL — `KeyError: 'asset_id'`.

- [ ] **Step 3: Add the schema field**

In `backend/app/schemas/showcase.py`, add to `ShowcaseItem` after `source`:

```python
    asset_id: str
```

- [ ] **Step 4: Populate it in the endpoint**

In `backend/app/api/showcase.py`, in the `items.append(ShowcaseItem(...))` call (~line 106), add `asset_id=aid`:

```python
        items.append(ShowcaseItem(
            work_id=str(w.id),
            title=w.title,
            creator_name=getattr(w, "creator_name", None),
            source=getattr(w, "source", None),
            asset_id=aid,
            thumb_url=f"/media/thumb/{aid}",
            preview_url=signed_media_url(aid, "preview"),
            width=width,
            height=height,
        ))
```

- [ ] **Step 5: Run it to verify it passes**

Run: `docker compose run --rm -T -v "<repo-root>/backend:/app" backend python -m pytest tests/test_showcase_api.py -q`
Expected: PASS.

- [ ] **Step 6: Mirror the TS type**

In `admin-web/src/lib/api/types.ts`, add to `ShowcaseItem` after `source` (line ~974):

```ts
  asset_id: string;
```

- [ ] **Step 7: Verify, deploy, commit**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit` → clean.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" backend
docker compose up -d --force-recreate backend worker-download worker-import worker-operations scheduler
git add backend/app/schemas/showcase.py backend/app/api/showcase.py backend/tests/test_showcase_api.py admin-web/src/lib/api/types.ts
git commit -m "feat(showcase): expose asset_id on sample items for slideshow linking

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Add gallery config fields (additive)

Add the new motion fields WITHOUT removing the trail fields yet, so the still-present trail consumers keep compiling. Task 6 removes the trail fields and migrates the settings UI once all consumers use the new fields.

**Files:**
- Modify: `admin-web/src/lib/showcase/config.tsx`

**Interfaces:**
- Produces: `ShowcaseConfig.planeHeightVh: number` (30–70), `.autoScrollSpeed: number` (0.2–3.0), `.curveStrength: number` (0–1); defaults 48 / 1.0 / 0.5. Consumed by the gallery renderer and canvas (Task 3).

- [ ] **Step 1: Add fields to the type**

In `admin-web/src/lib/showcase/config.tsx`, in `interface ShowcaseConfig`, extend the `// motion` block:

```ts
  // motion (trail — removed in Task 6)
  trailMax: number;
  spawnIntervalMs: number;
  followDamping: number;
  parallaxStrength: number;
  // motion (gallery)
  planeHeightVh: number;
  autoScrollSpeed: number;
  curveStrength: number;
  minimal: boolean;
```

- [ ] **Step 2: Add defaults**

In `DEFAULT_SHOWCASE_CONFIG`, add after `parallaxStrength: 0.4,`:

```ts
  planeHeightVh: 48,
  autoScrollSpeed: 1.0,
  curveStrength: 0.5,
```

- [ ] **Step 3: Add sanitizer clamps**

In `sanitizeShowcaseConfig`'s returned object, add after the `parallaxStrength` line:

```ts
    planeHeightVh: clampNumber(raw.planeHeightVh, 30, 70, DEFAULT_SHOWCASE_CONFIG.planeHeightVh),
    autoScrollSpeed: clampNumber(raw.autoScrollSpeed, 0.2, 3.0, DEFAULT_SHOWCASE_CONFIG.autoScrollSpeed),
    curveStrength: clampNumber(raw.curveStrength, 0, 1, DEFAULT_SHOWCASE_CONFIG.curveStrength),
```

(Ranges verified against the `(max-min)/step` integer rule for Task 6's controls: 30–70/5 = 8, 0.2–3.0/0.1 = 28, 0–1/0.05 = 20.)

- [ ] **Step 4: Verify and commit**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build` → clean, shared JS unchanged.

```bash
git add admin-web/src/lib/showcase/config.tsx
git commit -m "feat(showcase): add gallery motion config fields (additive)

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Gallery renderer + canvas (the visual swap)

The atomic visual-layer swap — the build cannot be green mid-swap, so the renderer, its layout module, the canvas rewrite, and the fallback deletions land together, broken into bite-sized steps. The fallback for this task is a **static large-image grid**; the CSS drift band comes in Task 4.

**Files:**
- Rename: `admin-web/src/lib/showcase/trailTiming.ts` → `admin-web/src/lib/showcase/smoothing.ts`
- Delete: `admin-web/src/lib/showcase/trail.ts`, `admin-web/src/components/showcase/ShowcaseTrailDOM.tsx`
- Create: `admin-web/src/lib/showcase/galleryLayout.ts`, `admin-web/src/components/showcase/ShowcaseStaticGrid.tsx`
- Rewrite: `admin-web/src/lib/showcase/webgl.ts`, `admin-web/src/components/showcase/ShowcaseCanvas.tsx`
- Modify: `admin-web/src/app/page.tsx`

**Interfaces:**
- Consumes: `ShowcaseItem` (with `asset_id`, `width`, `height`, `preview_url`); `frameIndependentAlpha`, `MAX_DT_MS` from `./smoothing`; `motionConfig.shouldAnimate()`; `PreviewAuthExpiredError`.
- Produces:
  ```ts
  // galleryLayout.ts
  export interface GalleryImage { url: string; width: number | null; height: number | null }
  export interface PlaneLayout { index: number; basePosX: number; planeW: number; planeH: number }
  export function computeStrip(images: GalleryImage[], opts: { planeH: number; gap: number; aspectClamp: [number, number] }): { planes: PlaneLayout[]; totalWidth: number }
  export function screenX(basePosX: number, scroll: number, totalWidth: number): number
  export function hitTestStrip(planes: PlaneLayout[], scroll: number, totalWidth: number, viewportCenterX: number, px: number): number | null

  // webgl.ts
  export interface ShowcaseRenderer {
    setImages(images: GalleryImage[]): Promise<void>;
    render(scroll: { current: number; velocity: number }): void;
    resize(): void;
    hitTest(clientX: number, clientY: number): number | null;
    destroy(): void;
  }
  export function createShowcaseRenderer(canvas: HTMLCanvasElement, opts: { planeHeightVh: number; curveStrength: number; maxTextures: number }): Promise<ShowcaseRenderer>
  export class PreviewAuthExpiredError extends Error {}
  ```

- [ ] **Step 1: Rename the smoothing module, drop trail-specific exports**

`git mv admin-web/src/lib/showcase/trailTiming.ts admin-web/src/lib/showcase/smoothing.ts`. Reduce it to the generic primitives only — delete `TrailConfig`, `MIN_LIFETIME_MS`, `computeLifetimeMs`, `MOVE_RECENCY_WINDOW_MS`, `CONVERGENCE_EPSILON_PX`, `isPointerActive`. Keep exactly:

```ts
// Shared frame-rate-independent smoothing for the showcase — used by the
// gallery canvas's scroll lerp. One formula, no reimplementation.
export const REFERENCE_FRAME_MS = 1000 / 60;
export const MAX_DT_MS = 100;

/** alpha(dt) = 1 - (1 - perFrameFactor)^(dt / REFERENCE_FRAME_MS); at dt=REFERENCE_FRAME_MS reduces to perFrameFactor. */
export function frameIndependentAlpha(perFrameFactor: number, dtMs: number): number {
  return 1 - Math.pow(1 - perFrameFactor, dtMs / REFERENCE_FRAME_MS);
}
```

- [ ] **Step 2: Write the layout unit test**

Create `admin-web/src/lib/showcase/galleryLayout.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { computeStrip, screenX, hitTestStrip } from "./galleryLayout";

const imgs = [
  { url: "a", width: 1000, height: 1000 },
  { url: "b", width: 2000, height: 1000 },
  { url: "c", width: null, height: null },
];

describe("computeStrip", () => {
  it("sizes plane width from real aspect at fixed height, sums total width with gaps", () => {
    const { planes, totalWidth } = computeStrip(imgs, { planeH: 400, gap: 40, aspectClamp: [0.4, 2.5] });
    expect(planes).toHaveLength(3);
    expect(planes[0].planeH).toBe(400);
    expect(planes[0].planeW).toBeCloseTo(400, 5);
    expect(planes[1].planeW).toBeCloseTo(800, 5);
    const sum = planes.reduce((s, p) => s + p.planeW, 0) + 40 * 3;
    expect(totalWidth).toBeCloseTo(sum, 5);
    expect(planes[1].basePosX).toBeGreaterThan(planes[0].basePosX);
  });

  it("clamps extreme aspect ratios", () => {
    const { planes } = computeStrip([{ url: "x", width: 5000, height: 500 }], { planeH: 400, gap: 0, aspectClamp: [0.4, 2.5] });
    expect(planes[0].planeW).toBeCloseTo(1000, 5);
  });
});

describe("screenX wrap", () => {
  it("wraps a base position by scroll into a centered range", () => {
    const total = 1000;
    expect(screenX(100, 0, total)).toBeCloseTo(screenX(100, total, total), 5);
  });
});

describe("hitTestStrip", () => {
  it("returns the index of the plane under a pixel, or null in a gap", () => {
    const { planes, totalWidth } = computeStrip(imgs, { planeH: 400, gap: 40, aspectClamp: [0.4, 2.5] });
    const cx0 = screenX(planes[0].basePosX, 0, totalWidth) + 720;
    expect(hitTestStrip(planes, 0, totalWidth, 720, cx0)).toBe(0);
    expect(hitTestStrip(planes, 0, totalWidth, 720, -99999)).toBeNull();
  });
});
```

Note: if `admin-web` has no vitest runner configured, convert this to a Node assertion script under the scratchpad and run with `node`, and record the deviation — the layout math MUST be tested in isolation because a hit-test bug is invisible until a user clicks the wrong image.

- [ ] **Step 3: Run the layout test — verify it fails**

Run: `cd <repo-root>/admin-web && npx vitest run src/lib/showcase/galleryLayout.test.ts` (or the Node fallback).
Expected: FAIL — module not found / functions undefined.

- [ ] **Step 4: Implement the layout math**

Create `admin-web/src/lib/showcase/galleryLayout.ts`:

```ts
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
```

- [ ] **Step 5: Run the layout test — verify it passes**

Run: `cd <repo-root>/admin-web && npx vitest run src/lib/showcase/galleryLayout.test.ts` (or Node fallback).
Expected: PASS.

- [ ] **Step 6: Rewrite the WebGL renderer**

Rewrite `admin-web/src/lib/showcase/webgl.ts` as the gallery renderer. Full replacement:

```ts
// The ONLY module allowed to reference "ogl". `ogl` is pulled in via a dynamic
// import() so it lands only in its own lazy chunk and is never fetched under
// reduced-motion / low-end / minimal. Client-only (invoked from a mount effect).

import { computeStrip, screenX, hitTestStrip, type GalleryImage, type PlaneLayout } from "./galleryLayout";

export interface ShowcaseRenderer {
  setImages(images: GalleryImage[]): Promise<void>;
  render(scroll: { current: number; velocity: number }): void;
  resize(): void;
  hitTest(clientX: number, clientY: number): number | null;
  destroy(): void;
}

/** Thrown by setImages when any preview URL 401s — the whole signed batch has expired together; caller refetches the batch. */
export class PreviewAuthExpiredError extends Error {
  constructor(url: string) {
    super(`showcase preview url expired (401): ${url}`);
    this.name = "PreviewAuthExpiredError";
  }
}

const GAP_PX = 40;
const ASPECT_CLAMP: [number, number] = [0.4, 2.5];
const VELOCITY_CLAMP = 4000;

// Aspect-cover UV correction (object-fit: cover) + velocity-driven cylindrical
// Z-bend. No chromatic aberration, no idle sine warp — those were the ghosting
// and wobble in the old trail shader.
const VERTEX = /* glsl */ `
  attribute vec2 uv;
  attribute vec3 position;
  uniform mat4 modelViewMatrix;
  uniform mat4 projectionMatrix;
  uniform float uStrength;
  uniform vec2 uViewportSizes;
  varying vec2 vUv;
  void main() {
    vUv = uv;
    vec3 p = position;
    p.z += sin(p.y / uViewportSizes.y * 3.14159265 + 3.14159265 / 2.0) * -uStrength;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }
`;

const FRAGMENT = /* glsl */ `
  precision highp float;
  uniform sampler2D tMap;
  uniform float uOpacity;
  uniform vec2 uPlaneSizes;
  uniform vec2 uImageSizes;
  varying vec2 vUv;
  void main() {
    vec2 ratio = vec2(
      min((uPlaneSizes.x / uPlaneSizes.y) / (uImageSizes.x / uImageSizes.y), 1.0),
      min((uPlaneSizes.y / uPlaneSizes.x) / (uImageSizes.y / uImageSizes.x), 1.0)
    );
    vec2 uv = vec2(vUv.x * ratio.x + (1.0 - ratio.x) * 0.5, vUv.y * ratio.y + (1.0 - ratio.y) * 0.5);
    gl_FragColor = texture2D(tMap, uv) * uOpacity;
  }
`;

function measureContainerSize(canvas: HTMLCanvasElement): { width: number; height: number } {
  const rect = canvas.parentElement?.getBoundingClientRect();
  const width = rect && rect.width > 0 ? rect.width : window.innerWidth;
  const height = rect && rect.height > 0 ? rect.height : window.innerHeight;
  return { width: Math.max(1, Math.round(width)), height: Math.max(1, Math.round(height)) };
}

async function fetchBitmap(url: string): Promise<ImageBitmap> {
  const res = await fetch(url);
  if (res.status === 401) throw new PreviewAuthExpiredError(url);
  if (!res.ok) throw new Error(`preview fetch failed: ${res.status}`);
  const blob = await res.blob();
  // imageOrientation:"flipY" gives a correctly-oriented bitmap for WebGL's
  // bottom-left origin — ogl's Texture flipY (UNPACK_FLIP_Y_WEBGL) is a no-op
  // for ImageBitmap sources, which is exactly why the old trail was upside-down.
  return createImageBitmap(blob, { imageOrientation: "flipY" });
}

export async function createShowcaseRenderer(
  canvas: HTMLCanvasElement,
  opts: { planeHeightVh: number; curveStrength: number; maxTextures: number },
): Promise<ShowcaseRenderer> {
  const { Renderer, Camera, Transform, Plane, Program, Mesh, Texture } = await import("ogl");
  type OglTexture = InstanceType<typeof Texture>;
  type OglMesh = InstanceType<typeof Mesh>;

  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let renderer: InstanceType<typeof Renderer>;
  try {
    const s = measureContainerSize(canvas);
    renderer = new Renderer({ canvas, alpha: true, dpr, width: s.width, height: s.height });
  } catch {
    throw new Error("webgl unavailable");
  }
  const gl = renderer.gl;
  if (!gl) throw new Error("webgl unavailable");

  const camera = new Camera(gl);
  const scene = new Transform();
  const geometry = new Plane(gl, { widthSegments: 20, heightSegments: 1 });
  const emptyTexture: OglTexture = new Texture(gl);

  let viewWidth = 1;
  let viewHeight = 1;
  let planeH = 1;
  let images: GalleryImage[] = [];
  let planes: PlaneLayout[] = [];
  let totalWidth = 0;
  let lastScroll = 0;

  interface Cell { mesh: OglMesh; imageIndex: number }
  const cells: Cell[] = [];
  const cache = new Map<string, { texture: OglTexture; bitmap: ImageBitmap; w: number; h: number }>();

  function makeProgram() {
    return new Program(gl, {
      vertex: VERTEX,
      fragment: FRAGMENT,
      uniforms: {
        tMap: { value: emptyTexture },
        uOpacity: { value: 1 },
        uStrength: { value: 0 },
        uViewportSizes: { value: [1, 1] },
        uPlaneSizes: { value: [1, 1] },
        uImageSizes: { value: [1, 1] },
      },
      transparent: true,
      depthTest: false,
      cullFace: false,
    });
  }

  function relayout() {
    planeH = (opts.planeHeightVh / 100) * viewHeight;
    const built = computeStrip(images, { planeH, gap: GAP_PX, aspectClamp: ASPECT_CLAMP });
    planes = built.planes;
    totalWidth = built.totalWidth;
    for (const c of cells) { try { (c.mesh.program as InstanceType<typeof Program>).remove(); } catch {} c.mesh.setParent(null); }
    cells.length = 0;
    for (const p of planes) {
      const mesh = new Mesh(gl, { geometry, program: makeProgram() });
      mesh.setParent(scene);
      cells.push({ mesh, imageIndex: p.index });
    }
  }

  async function setImages(next: GalleryImage[]): Promise<void> {
    images = next.slice();
    const toFetch = images.filter((im) => !cache.has(im.url));
    const settled = await Promise.allSettled(toFetch.map((im) => fetchBitmap(im.url)));
    let authExpired = false;
    toFetch.forEach((im, i) => {
      const r = settled[i];
      if (r.status === "rejected") {
        if (r.reason instanceof PreviewAuthExpiredError) authExpired = true;
        return;
      }
      const texture = new Texture(gl, { flipY: false });
      texture.image = r.value as unknown as OglTexture["image"];
      cache.set(im.url, { texture, bitmap: r.value, w: r.value.width, h: r.value.height });
    });
    while (cache.size > opts.maxTextures) {
      const oldest = cache.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      const e = cache.get(oldest);
      cache.delete(oldest);
      if (e) { try { gl.deleteTexture(e.texture.texture); } catch {} try { e.bitmap.close(); } catch {} }
    }
    relayout();
    if (authExpired) throw new PreviewAuthExpiredError(images[0]?.url ?? "");
  }

  function resize(): void {
    const s = measureContainerSize(canvas);
    viewWidth = s.width;
    viewHeight = s.height;
    renderer.setSize(viewWidth, viewHeight);
    camera.orthographic({ left: -viewWidth / 2, right: viewWidth / 2, bottom: -viewHeight / 2, top: viewHeight / 2, near: 0.1, far: 1000 });
    camera.position.z = 100;
    relayout();
  }
  resize();

  function render(scroll: { current: number; velocity: number }): void {
    const uStrength =
      (Math.max(-VELOCITY_CLAMP, Math.min(VELOCITY_CLAMP, scroll.velocity)) / viewWidth) * opts.curveStrength * 40;
    for (let i = 0; i < cells.length; i++) {
      const cell = cells[i];
      const p = planes[i];
      if (!p) continue;
      const entry = cache.get(images[cell.imageIndex].url);
      const x = screenX(p.basePosX, scroll.current, totalWidth);
      cell.mesh.position.set(x, 0, 0);
      cell.mesh.scale.set(p.planeW, p.planeH, 1);
      const u = (cell.mesh.program as InstanceType<typeof Program>).uniforms;
      u.tMap.value = entry ? entry.texture : emptyTexture;
      u.uOpacity.value = entry ? 1 : 0;
      u.uStrength.value = uStrength;
      u.uViewportSizes.value = [viewWidth, viewHeight];
      u.uPlaneSizes.value = [p.planeW, p.planeH];
      u.uImageSizes.value = entry ? [entry.w, entry.h] : [1, 1];
    }
    lastScroll = scroll.current;
    renderer.render({ scene, camera });
  }

  function hitTest(clientX: number, clientY: number): number | null {
    const rect = canvas.getBoundingClientRect();
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    if (Math.abs(py - viewHeight / 2) > planeH / 2) return null;
    return hitTestStrip(planes, lastScroll, totalWidth, viewWidth / 2, px);
  }

  function destroy(): void {
    for (const key of Array.from(cache.keys())) {
      const e = cache.get(key);
      cache.delete(key);
      if (!e) continue;
      try { gl.deleteTexture(e.texture.texture); } catch {}
      try { e.bitmap.close(); } catch {}
    }
    try { gl.deleteTexture(emptyTexture.texture); } catch {}
    for (const c of cells) { try { (c.mesh.program as InstanceType<typeof Program>).remove(); } catch {} }
    cells.length = 0;
    try { geometry.remove(); } catch {}
    // Deliberately does NOT loseContext() — the canvas is reused across renderer
    // recreations (settings changes); forcing loss would brick every future
    // renderer on this canvas. Genuine loss fires webglcontextlost, handled by
    // ShowcaseCanvas.
  }

  return { setImages, render, resize, hitTest, destroy };
}
```

- [ ] **Step 7: Create the static grid fallback**

Create `admin-web/src/components/showcase/ShowcaseStaticGrid.tsx`:

```tsx
"use client";
import type { ShowcaseItem } from "@/lib/api";

const GRID_COUNT = 8;

/** Reduced-motion / minimal fallback: a still grid of large aspect-correct images. No rAF, no pointer, no WebGL. */
export default function ShowcaseStaticGrid({ items }: { items: ShowcaseItem[] }) {
  const shown = items.slice(0, GRID_COUNT);
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-8">
      <div className="grid max-w-5xl grid-cols-2 gap-3 sm:grid-cols-4">
        {shown.map((it) => (
          <img
            key={it.work_id}
            src={it.thumb_url}
            alt=""
            aria-hidden="true"
            width={it.width ?? undefined}
            height={it.height ?? undefined}
            className="h-auto w-full rounded-lg object-cover"
            decoding="async"
          />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 8: Rewrite ShowcaseCanvas (WebGL gallery path + static fallback)**

Rewrite `admin-web/src/components/showcase/ShowcaseCanvas.tsx`. The degradation branch for this task is the static grid; Task 4 swaps the low-end/fellBack branch to `ShowcaseGalleryDOM`. Full replacement:

```tsx
"use client";

// Gallery canvas: owns auto-scroll + wheel/drag, drives the WebGL renderer with
// a dt-independent scroll lerp, and the degradation contract. `hardwareOk`
// (reduced-motion/low-end) is frozen at mount; `config.minimal` is live;
// `fellBack` (runtime WebGL loss) is one-way. When not WebGL -> static grid
// (Task 4 makes low-end/fellBack use ShowcaseGalleryDOM instead).

import { useEffect, useRef, useState } from "react";
import type { ShowcaseItem } from "@/lib/api";
import type { ShowcaseConfig } from "@/lib/showcase/config";
import { motionConfig } from "@/lib/motion";
import { frameIndependentAlpha, MAX_DT_MS } from "@/lib/showcase/smoothing";
import { createShowcaseRenderer, PreviewAuthExpiredError, type ShowcaseRenderer } from "@/lib/showcase/webgl";
import type { GalleryImage } from "@/lib/showcase/galleryLayout";
import ShowcaseStaticGrid from "./ShowcaseStaticGrid";

const MAX_AUTO_REFETCH_STREAK = 2;
const DRAG_PX_PER_UNIT = 1;
const WHEEL_SCALE = 0.6;
const SCROLL_EASE = 0.1;

export type GalleryCanvasConfig = Pick<
  ShowcaseConfig,
  "planeHeightVh" | "autoScrollSpeed" | "curveStrength" | "minimal"
>;

function toGalleryImages(items: ShowcaseItem[]): GalleryImage[] {
  return items.map((it) => ({ url: it.preview_url, width: it.width, height: it.height }));
}

export default function ShowcaseCanvas({
  items,
  config,
  onPreviewExpired,
  onHit,
}: {
  items: ShowcaseItem[];
  config: GalleryCanvasConfig;
  onPreviewExpired?: () => void;
  onHit?: (index: number) => void;
}) {
  const [hardwareOk] = useState(() => typeof window !== "undefined" && motionConfig.shouldAnimate());
  const useWebGL = hardwareOk && !config.minimal;
  const [fellBack, setFellBack] = useState(false);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<ShowcaseRenderer | null>(null);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const onPreviewExpiredRef = useRef(onPreviewExpired);
  onPreviewExpiredRef.current = onPreviewExpired;
  const onHitRef = useRef(onHit);
  onHitRef.current = onHit;
  const expiredStreakRef = useRef(0);

  function reportSetImagesOutcome(expired: boolean): void {
    if (expired) {
      expiredStreakRef.current += 1;
      if (expiredStreakRef.current <= MAX_AUTO_REFETCH_STREAK) onPreviewExpiredRef.current?.();
    } else {
      expiredStreakRef.current = 0;
    }
  }

  useEffect(() => {
    if (!useWebGL || fellBack) return;
    const canvas = canvasRef.current;
    if (!canvas) return;

    let cancelled = false;
    let renderer: ShowcaseRenderer | null = null;
    let rafId = 0;
    let running = true;
    let lastFrameTime: number | null = null;
    const scroll = { current: 0, target: 0, velocity: 0 };

    let dragging = false;
    let lastDragX = 0;
    function onPointerDown(e: PointerEvent) {
      dragging = true;
      lastDragX = e.clientX;
      canvas!.setPointerCapture(e.pointerId);
    }
    function onPointerMove(e: PointerEvent) {
      if (!dragging) return;
      scroll.target -= (e.clientX - lastDragX) * DRAG_PX_PER_UNIT;
      lastDragX = e.clientX;
    }
    function onPointerUp(e: PointerEvent) {
      dragging = false;
      try { canvas!.releasePointerCapture(e.pointerId); } catch {}
    }
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      scroll.target += (e.deltaY + e.deltaX) * WHEEL_SCALE;
    }
    function onContextLost(e: Event) { e.preventDefault(); setFellBack(true); }
    function onResize() { renderer?.resize(); }

    function paint(now: number) {
      if (!running || !renderer) return;
      const dt = lastFrameTime === null ? 0 : Math.min(now - lastFrameTime, MAX_DT_MS);
      lastFrameTime = now;
      scroll.target += config.autoScrollSpeed * (dt / (1000 / 60));
      const alpha = frameIndependentAlpha(SCROLL_EASE, dt);
      const prev = scroll.current;
      scroll.current += (scroll.target - scroll.current) * alpha;
      scroll.velocity = dt > 0 ? ((scroll.current - prev) / dt) * 1000 : 0;
      renderer.render({ current: scroll.current, velocity: scroll.velocity });
      rafId = requestAnimationFrame(paint);
    }
    function onVisibility() {
      if (document.hidden) { running = false; cancelAnimationFrame(rafId); lastFrameTime = null; }
      else if (!running && renderer) { running = true; rafId = requestAnimationFrame(paint); }
    }

    canvas.addEventListener("webglcontextlost", onContextLost);

    void (async () => {
      let created: ShowcaseRenderer;
      try {
        created = await createShowcaseRenderer(canvas!, {
          planeHeightVh: config.planeHeightVh,
          curveStrength: config.curveStrength,
          maxTextures: Math.max(24, itemsRef.current.length + 4),
        });
      } catch { if (!cancelled) setFellBack(true); return; }
      if (cancelled) { created.destroy(); return; }
      renderer = created;
      rendererRef.current = created;
      renderer.resize();
      try {
        await renderer.setImages(toGalleryImages(itemsRef.current));
        if (!cancelled) reportSetImagesOutcome(false);
      } catch (err) { if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError); }
      if (cancelled) return;
      canvas!.addEventListener("pointerdown", onPointerDown);
      canvas!.addEventListener("pointermove", onPointerMove);
      canvas!.addEventListener("pointerup", onPointerUp);
      canvas!.addEventListener("wheel", onWheel, { passive: false });
      window.addEventListener("resize", onResize);
      document.addEventListener("visibilitychange", onVisibility);
      if (!document.hidden) rafId = requestAnimationFrame(paint);
    })();

    return () => {
      cancelled = true;
      running = false;
      cancelAnimationFrame(rafId);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("wheel", onWheel);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
      rendererRef.current = null;
      renderer?.destroy();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useWebGL, fellBack, config.planeHeightVh, config.curveStrength, config.autoScrollSpeed]);

  useEffect(() => {
    if (!useWebGL || fellBack) return;
    const renderer = rendererRef.current;
    if (!renderer) return;
    let cancelled = false;
    renderer.setImages(toGalleryImages(items))
      .then(() => { if (!cancelled) reportSetImagesOutcome(false); })
      .catch((err) => { if (!cancelled) reportSetImagesOutcome(err instanceof PreviewAuthExpiredError); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, useWebGL, fellBack]);

  if (!useWebGL || fellBack) {
    return <ShowcaseStaticGrid items={items} />;
  }
  return <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />;
}
```

- [ ] **Step 9: page.tsx passes the gallery config**

In `admin-web/src/app/page.tsx`, the `ShowcaseCanvas` call already passes `config={config}` and `onPreviewExpired`; the full `ShowcaseConfig` satisfies `GalleryCanvasConfig` (a `Pick`), so no change is needed to the call. (Task 5 adds the slideshow node + `onHit`.)

- [ ] **Step 10: Delete the obsolete trail files**

```bash
git rm admin-web/src/lib/showcase/trail.ts admin-web/src/components/showcase/ShowcaseTrailDOM.tsx
```

Confirm no remaining imports: `grep -rn "ShowcaseTrailDOM\|showcase/trail\"\|createTrail\|trailTiming" admin-web/src` returns nothing.

- [ ] **Step 11: Verify, deploy, look at it**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build` → clean; shared JS unchanged; ogl still isolated:
```bash
grep -rl "OES_texture_float" .next/static/chunks/*.js   # exactly one chunk, not main-*
```

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
```
Manual on `http://127.0.0.1:13000/` (animations enabled): a horizontal row of large, correctly-oriented, aspect-correct images drifts continuously; wheel/drag accelerates and reverses; images bend only while scrolling fast, flatten at rest; no upside-down, no squish, no ghosting, no idle wobble. (Full acceptance is Task 7.)

- [ ] **Step 12: Commit**

```bash
git add admin-web/src/lib/showcase admin-web/src/components/showcase admin-web/src/app/page.tsx
git commit -m "feat(showcase): replace mouse trail with auto-scroll perspective gallery

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: DOM drift fallback for low-end / WebGL-unavailable

Reduced-motion / minimal keep the static grid (Task 3). This task makes the low-end and `fellBack` paths render a CSS `translateX` drift band, so low-end devices still get an "alive" gallery.

**Files:**
- Create: `admin-web/src/components/showcase/ShowcaseGalleryDOM.tsx`
- Modify: `admin-web/src/components/showcase/ShowcaseCanvas.tsx` (fallback branch)
- Modify: `admin-web/src/app/globals.css` (drift keyframes)

**Interfaces:**
- Consumes: `ShowcaseItem[]`, `GalleryCanvasConfig`.
- Produces: `<ShowcaseGalleryDOM items config />`.

- [ ] **Step 1: Add the drift keyframes**

In `admin-web/src/app/globals.css`, beside the existing keyframes:

```css
@keyframes galleryDrift {
  from { transform: translate3d(0, 0, 0); }
  to   { transform: translate3d(-50%, 0, 0); }
}
.gallery-drift { animation: galleryDrift var(--drift-duration, 60s) linear infinite; }
```

Transform-only; the global `prefers-reduced-motion` kill-switch collapses it (defense-in-depth — reduced-motion never reaches this component, it renders the static grid).

- [ ] **Step 2: Build the DOM drift band**

Create `admin-web/src/components/showcase/ShowcaseGalleryDOM.tsx`:

```tsx
"use client";
import type { ShowcaseItem } from "@/lib/api";
import type { GalleryCanvasConfig } from "./ShowcaseCanvas";

/** Low-end / WebGL-unavailable fallback: a CSS translateX drift band. Transform-only, GPU-composited, no WebGL, no rAF. Images duplicated once for a seamless -50% loop. */
export default function ShowcaseGalleryDOM({ items, config }: { items: ShowcaseItem[]; config: GalleryCanvasConfig }) {
  const strip = [...items, ...items];
  const durationS = Math.max(20, 120 / Math.max(0.2, config.autoScrollSpeed));
  return (
    <div className="absolute inset-0 flex items-center overflow-hidden">
      <div
        className="gallery-drift flex flex-nowrap gap-6 will-change-transform"
        style={{ ["--drift-duration" as string]: `${durationS}s`, height: `${config.planeHeightVh}vh` }}
      >
        {strip.map((it, i) => (
          <img
            key={`${it.work_id}-${i}`}
            src={it.thumb_url}
            alt=""
            aria-hidden="true"
            width={it.width ?? undefined}
            height={it.height ?? undefined}
            className="h-full w-auto rounded-lg object-cover"
            decoding="async"
          />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Route the fallback branch to it**

In `admin-web/src/components/showcase/ShowcaseCanvas.tsx`, add `import ShowcaseGalleryDOM from "./ShowcaseGalleryDOM";` and replace the single fallback return with:

```tsx
  if (!useWebGL || fellBack) {
    const reducedMotion =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || config.minimal) return <ShowcaseStaticGrid items={items} />;
    return <ShowcaseGalleryDOM items={items} config={config} />;
  }
```

- [ ] **Step 4: Verify, deploy, commit**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build` → clean.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/src/components/showcase/ShowcaseGalleryDOM.tsx admin-web/src/components/showcase/ShowcaseCanvas.tsx admin-web/src/app/globals.css
git commit -m "feat(showcase): CSS drift-band gallery fallback for low-end / no-WebGL

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Click a plane → open the slideshow

**Files:**
- Modify: `admin-web/src/components/showcase/ShowcaseCanvas.tsx` (click/drag disambiguation, call `onHit`)
- Modify: `admin-web/src/app/page.tsx` (useSlideshow, map hit → SlideItem[])

**Interfaces:**
- Consumes: `ShowcaseRenderer.hitTest(clientX, clientY)`; `useSlideshow()` → `{ open(items: SlideItem[], startIndex?), node }`; `SlideItem = { assetId, workId, title?, creatorName? }`.

- [ ] **Step 1: Disambiguate click from drag in the canvas**

In `ShowcaseCanvas.tsx`, track pointer travel between down and up; on `pointerup`, if total movement < 6px and duration < 400ms, call `renderer.hitTest` and fire `onHit`. Replace the three pointer handlers inside the effect with:

```tsx
    let dragging = false;
    let lastDragX = 0;
    let downX = 0, downY = 0, downT = 0, moved = 0;
    function onPointerDown(e: PointerEvent) {
      dragging = true;
      lastDragX = e.clientX;
      downX = e.clientX; downY = e.clientY; downT = performance.now(); moved = 0;
      canvas!.setPointerCapture(e.pointerId);
    }
    function onPointerMove(e: PointerEvent) {
      if (!dragging) return;
      moved += Math.abs(e.clientX - lastDragX);
      scroll.target -= (e.clientX - lastDragX) * DRAG_PX_PER_UNIT;
      lastDragX = e.clientX;
    }
    function onPointerUp(e: PointerEvent) {
      dragging = false;
      try { canvas!.releasePointerCapture(e.pointerId); } catch {}
      const dist = Math.hypot(e.clientX - downX, e.clientY - downY);
      if (dist < 6 && moved < 6 && performance.now() - downT < 400 && renderer) {
        const idx = renderer.hitTest(e.clientX, e.clientY);
        if (idx != null) onHitRef.current?.(idx);
      }
    }
```

Give the canvas a click affordance + touch handling: `className="absolute inset-0 h-full w-full cursor-pointer touch-none"`.

- [ ] **Step 2: Wire the slideshow in page.tsx**

In `admin-web/src/app/page.tsx`:

```tsx
import { useSlideshow } from "@/lib/useSlideshow";
import type { SlideItem } from "@/components/SlideshowPlayer";
```

Inside `Home()`, after `const items = ...`:

```tsx
  const slideshow = useSlideshow();
  const slideItems: SlideItem[] = items.map((it) => ({
    assetId: it.asset_id,
    workId: it.work_id,
    title: it.title,
    creatorName: it.creator_name,
  }));
```

Replace the render block:

```tsx
      <ShowcaseCanvas
        items={items}
        config={config}
        onPreviewExpired={() => sample.refetch()}
        onHit={(idx) => slideshow.open(slideItems, idx)}
      />
      <ShowcaseHero config={config} itemCount={items.length} />
      {slideshow.node}
```

- [ ] **Step 3: Verify, deploy, commit**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build` → clean.

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
```
Manual: click a drifting image → fullscreen slideshow opens on that work; a drag scrolls without opening anything.

```bash
git add admin-web/src/components/showcase/ShowcaseCanvas.tsx admin-web/src/app/page.tsx
git commit -m "feat(showcase): click a gallery plane to open the fullscreen slideshow

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Config cleanup + settings UI migration

Now that no consumer reads the trail fields, remove them and migrate the settings 动效 group to the gallery knobs.

**Files:**
- Modify: `admin-web/src/lib/showcase/config.tsx` (remove trail fields)
- Modify: `admin-web/src/app/admin/settings/showcase/page.tsx` (动效 controls, ~lines 209–245)
- Modify: `admin-web/src/lib/i18n.tsx` (动效 keys, zh + en)

**Interfaces:**
- Produces: `ShowcaseConfig` without `trailMax`/`spawnIntervalMs`/`followDamping`/`parallaxStrength`.

- [ ] **Step 1: Remove trail fields from the config**

In `admin-web/src/lib/showcase/config.tsx`: delete the four trail lines from `interface ShowcaseConfig`, from `DEFAULT_SHOWCASE_CONFIG`, and from `sanitizeShowcaseConfig`. Leave the gallery fields and every other group untouched. (Old stored prefs with `trailMax` etc. are silently dropped by the sanitizer.)

- [ ] **Step 2: Migrate the settings 动效 controls**

In `admin-web/src/app/admin/settings/showcase/page.tsx`, replace the four trail `RangeRow`s with three gallery `RangeRow`s; keep the `minimal` `SettingRow` below them exactly as is. Match the existing `RangeRow` prop shape (`label`/`value`/`min`/`max`/`step`/`disabled`/`onChange`):

```tsx
          <RangeRow
            label={t("showcase_settings.plane_height")}
            value={config.planeHeightVh}
            min={30} max={70} step={5}
            disabled={config.minimal}
            onChange={(value) => update({ planeHeightVh: value })}
          />
          <RangeRow
            label={t("showcase_settings.auto_scroll_speed")}
            value={config.autoScrollSpeed}
            min={0.2} max={3.0} step={0.1}
            disabled={config.minimal}
            onChange={(value) => update({ autoScrollSpeed: value })}
          />
          <RangeRow
            label={t("showcase_settings.curve_strength")}
            value={config.curveStrength}
            min={0} max={1} step={0.05}
            disabled={config.minimal}
            onChange={(value) => update({ curveStrength: value })}
          />
```

(min/max/step equal the sanitizer clamps from Task 2, `(max-min)/step` integer.)

- [ ] **Step 3: Migrate i18n keys (zh AND en)**

In `admin-web/src/lib/i18n.tsx`, in BOTH the `zh` dict (~line 1175) and the `buildEn` overrides (~line 3863): remove `showcase_settings.trail_max`, `.spawn_interval`, `.follow_damping`, `.parallax`; add:

```
zh:  "showcase_settings.plane_height": "图片高度",
     "showcase_settings.auto_scroll_speed": "自动滚动速度",
     "showcase_settings.curve_strength": "弯曲强度",
en:  "showcase_settings.plane_height": "Image height",
     "showcase_settings.auto_scroll_speed": "Auto-scroll speed",
     "showcase_settings.curve_strength": "Curve strength",
```

Keep `showcase_settings.minimal_hint` — its "以上动效参数将不再生效 / the motion parameters above no longer take effect" copy is still accurate for the three gallery controls above it.

- [ ] **Step 4: Verify, deploy, commit**

Run: `cd <repo-root>/admin-web && npx tsc --noEmit && npm run build` → clean. Confirm `/admin/settings/showcase` shows three gallery sliders + minimal, and each writes the right field (values land in localStorage `auto-gallery-showcase-v1`).

```bash
docker compose build --build-arg CACHEBUST="$(date +%s)" admin-web && docker compose up -d --force-recreate admin-web
git add admin-web/src/lib/showcase/config.tsx admin-web/src/app/admin/settings/showcase/page.tsx admin-web/src/lib/i18n.tsx
git commit -m "refactor(showcase): migrate settings motion group from trail to gallery

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Real-browser acceptance + docs/ledger

**Files:**
- Modify: `docs/frontend-motion-audit.md` (append gallery section)
- Modify: `.superpowers/sdd/progress.md` (ledger)

**Interfaces:**
- Consumes: the T7 browser harness (playwright-core in the scratchpad, Chromium at `<local-home>/.cache/ms-playwright/chromium-1232/chrome-linux64/chrome`), JWT minted in the backend container, admin-web at `http://127.0.0.1:13000`.

- [ ] **Step 1: Seed a handful of works if the library is empty**

The gallery needs images. If `SELECT count(*) FROM works` is 0, seed ~12 real-file works with a `qa-seed-gallery` prefix (reuse the earlier T7 report's seeding approach) so `/` shows the gallery, and record the exact cleanup query. Clean them up at the end and confirm 0 remain.

- [ ] **Step 2: Run the visual + interaction acceptance**

Drive the real browser and capture evidence for each (screenshot / network log / DOM assertion). **Every one must pass:**
1. Orientation correct — images NOT upside-down.
2. Aspect correct — non-square images not squished (measure rendered plane w/h vs source aspect).
3. Large — planes are a real fraction of viewport height (≈ `planeHeightVh`), not 96px.
4. Auto-scroll drifts continuously; wheel accelerates/reverses; drag scrolls.
5. Curve appears only while scrolling fast, flattens at rest.
6. No chromatic fringe, no idle wobble.
7. Click a plane → slideshow opens on the correct work; a drag does NOT open it.
8. Infinite loop: no visible seam / hard pop-in at wrap.
9. ogl chunk fetched exactly once on the normal profile.

- [ ] **Step 3: Run the degradation acceptance**

1. `prefers-reduced-motion: reduce` → static large-image grid, `document.getAnimations()` running == 0, ogl chunk NOT requested.
2. `hardwareConcurrency <= 4` (override via `addInitScript`) → CSS drift band (DOM), ogl NOT requested.
3. Force `webglcontextlost` from the console → silent one-way fall back to the drift band, no error UI.
4. `--disable-gpu` launch → drift band on load, no error UI.
5. `config.minimal: true` live-toggle → switches to static grid without reload; fresh load with minimal:true → ogl NOT requested.
6. `document.hidden` → rAF frozen (draw-call counter stops), resumes on visible.

- [ ] **Step 4: Run the perf acceptance**

Reuse the perf recorder against `/` in both `no-preference` and `reduce`:
- `/` long tasks == 0 in both modes.
- `/` CLS < 0.1 (the gallery is a full-viewport absolute layer over a fixed-height shell — it must not shift the hero).
- `/admin/works`, `/admin/jobs` no worse than baseline.

If any visual (Step 2) or perf (Step 4) criterion fails, STOP and report precisely — do not paper over it. Fixes are separate dispatches.

- [ ] **Step 5: Write the appendix and ledger**

Append a 展示页画廊 subsection to the existing 附录 in `docs/frontend-motion-audit.md` (same table shape as the MU-T2 table: 页面 / 模式 / FCP / LCP / CLS / long tasks / 运行中动画), plus a line stating the ogl chunk was requested only on the normal profile.

Append to `.superpowers/sdd/progress.md`:

```
# ── Phase: 展示页画廊化重构 (2026-07-22, frontend-motion) ──
Spec docs/superpowers/specs/2026-07-22-showcase-gallery-redesign-design.md; plan docs/superpowers/plans/2026-07-22-showcase-gallery-redesign.md.
拖尾→makemepulse 式自动滚动透视画廊。四缺陷根治:原图比例 cover(修挤扁)、createImageBitmap(imageOrientation:flipY)(修倒置)、删色差删 idle 抖动只留速度驱动弯曲(修虚影/波动)。asset_id 后端字段;画廊渲染器重写(galleryLayout 纯函数单测 + webgl.ts);自动滚动+滚轮/拖拽 dt 无关 lerp;降级两路(reduced-motion/minimal→静态大图网格、低端/WebGL 不可用→CSS 漂移带);点击命中→幻灯片(6px/400ms 区分拖拽);配置动效组 trail→gallery。真实浏览器验收全清单通过。
```

- [ ] **Step 6: Commit**

```bash
git add docs/frontend-motion-audit.md .superpowers/sdd/progress.md
git commit -m "docs(showcase): gallery redesign perf/a11y acceptance and ledger

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** ①后端 asset_id→T1;②画廊渲染器(cover/flipY/curve/无限带)→T3;③自动滚动+滚轮/拖拽→T3;④降级两路→T3(静态网格)+T4(漂移带);⑤点击开幻灯片→T5;⑥配置迁移→T2(加)+T6(删+UI);⑦真实浏览器验收→T7。spec 第四节四缺陷全部对应 T3 的具体着色器/位图/尺寸改动;第六节降级映射→T3/T4;第七节字段迁移→T2/T6;第十节验收→T7 全清单;风险 G1(命中)→galleryLayout 单测+屏幕区间命中,G2(接缝)→screenX 居中回绕,G6(chunk)→T3 Step 11 现查。
- **依赖顺序修正 vs spec §12:** 配置字段 T2 先加、T6 后删,保证每个任务边界构建绿(spec §12 把配置放第 6 步会让 T3/T4 消费不存在的字段)。
- **Type consistency:** `ShowcaseItem.asset_id`(T1 后端+TS)一致;`GalleryImage`/`PlaneLayout`/`computeStrip`/`screenX`/`hitTestStrip`(T3 galleryLayout 定义,webgl.ts 消费);`ShowcaseRenderer` 新接口(`setImages(GalleryImage[])`/`render(scroll)`/`hitTest`)T3 定义、ShowcaseCanvas 消费;`GalleryCanvasConfig`(T3 定义,page.tsx 传全量 ShowcaseConfig 满足 Pick);`SlideItem`(T5 从 SlideshowPlayer 消费,assetId=asset_id);`frameIndependentAlpha`/`MAX_DT_MS`(smoothing.ts,T3 保留、ShowcaseCanvas 消费)。
- **Open verification point:** T3 Step 2 的 vitest 若 admin-web 未配置 runner,降级为 scratchpad 下 Node 断言脚本;T7 的 ogl chunk 名为内容哈希,现查 `OES_texture_float` 再断言。
