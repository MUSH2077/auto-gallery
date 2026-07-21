// The ONLY module allowed to reference "ogl" — everything else that wants
// the WebGL showcase trail goes through `createShowcaseRenderer` /
// `ShowcaseRenderer` below. `ogl` is pulled in via a dynamic `import("ogl")`
// inside `createShowcaseRenderer` (mirroring `src/lib/motion/anime.ts`'s
// dynamic `import("animejs")`), so the library only lands in its own
// content-hashed lazy chunk and is never fetched by a route that doesn't use
// it — in particular, never under reduced-motion / low-end / minimal, where
// the caller (ShowcaseCanvas) must not call this factory at all.
//
// This module only ever runs client-side (invoked from a mount effect), so
// referencing `window`/`document`/canvas APIs inside functions is safe; there
// is no top-level code here that touches them, so importing this module
// itself (for its types) is SSR-safe too.

import type { TrailItem } from "./trail";
import { motionTokens } from "@/lib/motion";

export interface ShowcaseRenderer {
  setImages(urls: string[]): Promise<void>;
  render(items: TrailItem[], pointer: { x: number; y: number }): void;
  resize(): void;
  destroy(): void;
}

/**
 * Thrown by `setImages` when any preview URL comes back 401 — the signed
 * `preview_url`s from the showcase sample endpoint carry a short TTL (see
 * backend/app/services/media_signing.py), and once one has expired the rest
 * of the same batch almost certainly has too (they're signed together in one
 * response). The caller (ShowcaseCanvas) should treat this as "refetch the
 * whole sample batch," not as a per-image failure — ordinary per-image
 * failures (404, decode error, transient network error) are swallowed
 * inside `setImages` instead and never surface as a rejection.
 */
export class PreviewAuthExpiredError extends Error {
  constructor(url: string) {
    super(`showcase preview url expired (401): ${url}`);
    this.name = "PreviewAuthExpiredError";
  }
}

// Base plane size in CSS px before the item-age scale envelope multiplies it
// — mirrors the DOM renderer's ~80px/112px pooled <img> footprint.
const BASE_ITEM_SIZE_PX = 96;

// Fade-in reuses the shared motion token (same value ShowcaseTrailDOM uses)
// so the pop-in feels identical between renderers.
const FADE_IN_MS = motionTokens.duration.fast;
const FADE_OUT_MS = motionTokens.duration.slow;
const POP_SCALE_START = 0.7;
const FADE_OUT_SCALE_END = 0.85;

// `render(items, pointer)` is a fixed 2-argument interface — it does not
// receive the config-derived `lifetimeMs` that ShowcaseTrailDOM's ageStyle()
// curve is normalized against (that value lives in ShowcaseCanvas, which
// constructs the trail controller). Rather than duplicate config plumbing
// through a signature we're not allowed to change, this renderer derives a
// self-adjusting *estimate* of the same quantity by watching how old the
// longest-lived currently-alive item gets: the controller upstream already
// culls items at the true lifetimeMs, so the oldest surviving item's age
// converges to it within one spawn/cull cycle. That's enough to drive a
// graceful fade-out envelope without any extra coupling.
const INITIAL_LIFETIME_ESTIMATE_MS = 900; // seeds at the same floor ShowcaseTrailDOM's lifetime is clamped to.

// Clamp on the per-frame pointer delta used for uVelocity/parallax so a tab
// resuming from background (a huge apparent jump) can't blow up the shader
// offset or fling the parallax layer across the screen.
const VELOCITY_CLAMP_PX = 60;

const VERTEX = /* glsl */ `
  attribute vec2 uv;
  attribute vec3 position;

  uniform mat4 modelViewMatrix;
  uniform mat4 projectionMatrix;

  varying vec2 vUv;

  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

// Fluid-distortion look: sample tMap three times with a per-channel UV
// offset proportional to uVelocity (a cheap RGB chromatic-aberration split
// that reads as "fluid" when the cursor moves fast), plus a mild sinusoidal
// UV warp driven by uTime so idle items still have a slow living ripple.
// The final color is multiplied by uOpacity to drive the item-age fade.
const FRAGMENT = /* glsl */ `
  precision highp float;

  uniform sampler2D tMap;
  uniform float uOpacity;
  uniform vec2 uVelocity;
  uniform float uTime;

  varying vec2 vUv;

  void main() {
    vec2 warp = vec2(
      sin(vUv.y * 6.0 + uTime * 1.6),
      cos(vUv.x * 6.0 + uTime * 1.6)
    ) * 0.012;
    vec2 uv = vUv + warp;

    vec2 offset = uVelocity * 0.015;
    float r = texture2D(tMap, uv + offset).r;
    vec4 mid = texture2D(tMap, uv);
    float b = texture2D(tMap, uv - offset).b;

    gl_FragColor = vec4(r, mid.g, b, mid.a) * uOpacity;
  }
`;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

/** Item-age envelope: fade+pop in, hold, fade+shrink out — see the lifetime-estimate note above for why the "out" edge is estimated rather than exact. */
function ageEnvelope(age: number, lifetimeEstimateMs: number): { opacity: number; scale: number } {
  if (age <= FADE_IN_MS) {
    const p = FADE_IN_MS > 0 ? age / FADE_IN_MS : 1;
    return { opacity: p, scale: POP_SCALE_START + (1 - POP_SCALE_START) * p };
  }
  const remaining = lifetimeEstimateMs - age;
  if (remaining <= FADE_OUT_MS) {
    const p = clamp(remaining / FADE_OUT_MS, 0, 1);
    return { opacity: p, scale: FADE_OUT_SCALE_END + (1 - FADE_OUT_SCALE_END) * p };
  }
  return { opacity: 1, scale: 1 };
}

async function fetchBitmap(url: string): Promise<ImageBitmap> {
  const res = await fetch(url);
  if (res.status === 401) throw new PreviewAuthExpiredError(url);
  if (!res.ok) throw new Error(`preview fetch failed: ${res.status} ${res.statusText}`);
  const blob = await res.blob();
  // Decoding off the main thread — this is the whole point of
  // createImageBitmap over new Image(): no main-thread decode cost, keeping
  // the project's zero-long-task baseline intact even while loading two
  // dozen preview images.
  return createImageBitmap(blob);
}

export async function createShowcaseRenderer(
  canvas: HTMLCanvasElement,
  opts: { maxTextures: number; parallaxStrength: number },
): Promise<ShowcaseRenderer> {
  const { Renderer, Camera, Transform, Plane, Program, Mesh, Texture } = await import("ogl");
  type OglTexture = InstanceType<typeof Texture>;
  type OglMesh = InstanceType<typeof Mesh>;

  let renderer: InstanceType<typeof Renderer>;
  try {
    renderer = new Renderer({ canvas, alpha: true, dpr: Math.min(window.devicePixelRatio || 1, 2) });
  } catch {
    throw new Error("webgl unavailable");
  }
  const gl = renderer.gl;
  if (!gl) throw new Error("webgl unavailable");

  const camera = new Camera(gl);
  const scene = new Transform();
  const geometry = new Plane(gl);
  // One shared Program for every pooled mesh (OGL's own idiom for instancing
  // — see e.g. its sort-transparency example, where 50 meshes share one
  // Program). Per-mesh "own uniforms" are achieved via each mesh's
  // onBeforeRender callback writing that mesh's tMap/uOpacity into the
  // shared uniforms object immediately before that mesh draws — the same
  // mechanism OGL itself uses internally for per-mesh matrices.
  const program = new Program(gl, {
    vertex: VERTEX,
    fragment: FRAGMENT,
    uniforms: {
      tMap: { value: null as OglTexture | null },
      uOpacity: { value: 0 },
      uVelocity: { value: [0, 0] as [number, number] },
      uTime: { value: 0 },
    },
    transparent: true,
    depthTest: false,
    cullFace: false,
  });

  // Placeholder so `program.uniforms.tMap.value` is never null — OGL's
  // Program.use() does `uniform.value.texture` on every active sampler
  // uniform without a null-check, so assigning `null` there would throw on
  // the very first frame. An empty Texture uploads a harmless 1x1 pixel.
  const emptyTexture: OglTexture = new Texture(gl);

  interface CacheEntry {
    texture: OglTexture;
    bitmap: ImageBitmap;
  }
  const cache = new Map<string, CacheEntry>();
  let currentUrls: string[] = [];

  function touch(url: string): CacheEntry | undefined {
    const entry = cache.get(url);
    if (!entry) return undefined;
    cache.delete(url);
    cache.set(url, entry); // move to MRU end
    return entry;
  }

  function evictOldest(): void {
    const oldestKey = cache.keys().next().value;
    if (oldestKey === undefined) return;
    const entry = cache.get(oldestKey);
    cache.delete(oldestKey);
    if (entry) {
      try {
        gl.deleteTexture(entry.texture.texture);
      } catch {
        /* already lost */
      }
      try {
        entry.bitmap.close();
      } catch {
        /* already closed */
      }
    }
  }

  async function setImages(urls: string[]): Promise<void> {
    currentUrls = urls.slice();

    const toFetch = urls.filter((u) => !cache.has(u));
    const settled = await Promise.allSettled(toFetch.map((url) => fetchBitmap(url)));

    let authExpired = false;
    toFetch.forEach((url, i) => {
      const result = settled[i];
      if (result.status === "rejected") {
        if (result.reason instanceof PreviewAuthExpiredError) {
          authExpired = true;
        } else {
          // A single failed image is skipped, never rejects the whole call.
          // eslint-disable-next-line no-console
          console.debug("[showcase webgl] skipped preview image", url, result.reason);
        }
        return;
      }
      const texture = new Texture(gl, { flipY: true });
      // ogl's ImageRepresentation type predates ImageBitmap support in its
      // .d.ts, but Texture.update() only ever reads .width/.height and hands
      // the value straight to texImage2D — both of which accept an
      // ImageBitmap at runtime. Narrow cast, not a behavior workaround.
      texture.image = result.value as unknown as OglTexture["image"];
      cache.set(url, { texture, bitmap: result.value });
    });

    // Touch every requested url (already-cached or newly-fetched) in request
    // order so MRU ordering reflects "most recently requested," then trim
    // down to the cap. A url that never got a texture (skipped/expired) is
    // simply absent from the cache — render() treats that as "not ready yet"
    // and skips drawing it this frame rather than failing.
    for (const url of urls) touch(url);
    while (cache.size > opts.maxTextures) evictOldest();

    if (authExpired) throw new PreviewAuthExpiredError(urls[0] ?? "");
  }

  interface PooledMesh {
    mesh: OglMesh;
    texture: OglTexture;
    opacity: number;
  }
  const meshPool: PooledMesh[] = [];

  function ensureMesh(i: number): PooledMesh {
    const existing = meshPool[i];
    if (existing) return existing;
    const mesh = new Mesh(gl, { geometry, program });
    mesh.setParent(scene);
    const state: PooledMesh = { mesh, texture: emptyTexture, opacity: 0 };
    mesh.onBeforeRender(() => {
      program.uniforms.tMap.value = state.texture;
      program.uniforms.uOpacity.value = state.opacity;
    });
    meshPool[i] = state;
    return state;
  }

  let viewWidth = Math.max(1, canvas.clientWidth);
  let viewHeight = Math.max(1, canvas.clientHeight);

  function resize(): void {
    viewWidth = Math.max(1, canvas.clientWidth);
    viewHeight = Math.max(1, canvas.clientHeight);
    renderer.setSize(viewWidth, viewHeight);
    // Orthographic camera in CSS-pixel world units, top-left origin matching
    // DOM pointer coordinates: world (0,0) -> screen top-left, world
    // (viewWidth, viewHeight) -> screen bottom-right. Mesh positions can
    // then use item.x/item.y directly with no per-item flip.
    camera.orthographic({ left: 0, right: viewWidth, bottom: 0, top: viewHeight, near: 0.1, far: 10 });
    camera.position.z = 1;
  }
  resize();

  let hasLastPointer = false;
  const lastPointer = { x: 0, y: 0 };
  let lifetimeEstimateMs = INITIAL_LIFETIME_ESTIMATE_MS;
  let destroyed = false;

  function render(items: TrailItem[], pointer: { x: number; y: number }): void {
    if (destroyed) return;
    const now = performance.now();

    let vx = 0;
    let vy = 0;
    if (hasLastPointer) {
      vx = clamp(pointer.x - lastPointer.x, -VELOCITY_CLAMP_PX, VELOCITY_CLAMP_PX);
      vy = clamp(pointer.y - lastPointer.y, -VELOCITY_CLAMP_PX, VELOCITY_CLAMP_PX);
    }
    lastPointer.x = pointer.x;
    lastPointer.y = pointer.y;
    hasLastPointer = true;

    program.uniforms.uTime.value = now * 0.001;
    program.uniforms.uVelocity.value = [vx, vy];

    const offsetX = vx * opts.parallaxStrength;
    const offsetY = vy * opts.parallaxStrength;

    for (const it of items) {
      lifetimeEstimateMs = Math.max(lifetimeEstimateMs, now - it.bornAt + FADE_OUT_MS);
    }

    const slotCount = Math.max(items.length, meshPool.length);
    for (let i = 0; i < slotCount; i++) {
      const slot = ensureMesh(i);
      const item = items[i];
      if (!item) {
        slot.opacity = 0;
        continue;
      }

      const url = currentUrls.length > 0 ? currentUrls[item.imageIndex % currentUrls.length] : undefined;
      const cached = url ? touch(url) : undefined;
      slot.texture = cached ? cached.texture : emptyTexture;

      const { opacity, scale } = ageEnvelope(now - item.bornAt, lifetimeEstimateMs);
      slot.opacity = cached ? opacity : 0;

      const worldX = item.x + offsetX;
      const worldY = viewHeight - (item.y + offsetY);
      slot.mesh.position.set(worldX, worldY, 0);
      const size = BASE_ITEM_SIZE_PX * scale;
      slot.mesh.scale.set(size, size, 1);
    }

    renderer.render({ scene, camera });
  }

  function destroy(): void {
    if (destroyed) return;
    destroyed = true;
    for (const key of Array.from(cache.keys())) {
      const entry = cache.get(key);
      cache.delete(key);
      if (!entry) continue;
      try {
        gl.deleteTexture(entry.texture.texture);
      } catch {
        /* already lost */
      }
      try {
        entry.bitmap.close();
      } catch {
        /* already closed */
      }
    }
    try {
      gl.deleteTexture(emptyTexture.texture);
    } catch {
      /* already lost */
    }
    meshPool.length = 0;
    currentUrls = [];
    try {
      program.remove();
    } catch {
      /* already gone */
    }
    // No listeners are registered internally by this module (ShowcaseCanvas
    // owns pointermove/resize/visibilitychange/webglcontextlost) — this is
    // the hook to remove any that a future change adds here.
    try {
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      /* not supported — nothing to release */
    }
  }

  return { setImages, render, resize, destroy };
}
