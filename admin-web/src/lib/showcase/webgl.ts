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
