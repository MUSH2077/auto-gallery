import { expect, test, type BrowserContext, type Route } from "@playwright/test";

test.describe.configure({ timeout: 30_000 });

const json = (route: Route, body: unknown, status = 200) => route.fulfill({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

const works = [
  {
    id: "work-sfw",
    title: "Harbor light",
    posted_at: "2026-09-20T00:00:00Z",
    created_at: "2026-09-21T00:00:00Z",
    is_nsfw: false,
    is_ai_generated: true,
    is_favorite: true,
    asset_count: 1,
    thumbnail_asset_id: "asset-sfw",
    thumbnail_width: 1200,
    thumbnail_height: 800,
    source: "pixiv",
  },
  {
    id: "work-nsfw",
    title: "Portrait study",
    posted_at: "2026-09-18T00:00:00Z",
    created_at: "2026-09-19T00:00:00Z",
    is_nsfw: true,
    is_ai_generated: false,
    is_favorite: false,
    asset_count: 1,
    thumbnail_asset_id: "asset-nsfw",
    thumbnail_width: 800,
    thumbnail_height: 1200,
    source: "x",
  },
];

function queryParts(query: string): string[] {
  return [...(query.match(/(?:[^\s"]|"[^"]*")+/g) || [])];
}

function parsedTokens(query: string) {
  return queryParts(query).map((raw) => {
    const match = raw.match(/^(-?)([a-z_]+):(.*)$/i);
    if (!match) return { kind: "text", value: raw.replace(/^"|"$/g, "") };
    return {
      kind: "qualifier",
      key: match[2],
      value: match[3].replace(/^"|"$/g, ""),
      negated: match[1] === "-",
    };
  });
}

type Compose = {
  key: string;
  value?: string | null;
  operation?: "set" | "add" | "toggle" | "remove" | "replace-group";
  negated?: boolean;
  replace_values?: string[];
};

function composeQuery(query: string, operations: Compose[]) {
  let parts = queryParts(query);
  for (const compose of operations) {
    const operation = compose.operation || "set";
    const qualifier = (raw: string) => {
      const match = raw.match(/^(-?)([a-z_]+):(.*)$/i);
      return match ? { key: match[2], value: match[3].replace(/^"|"$/g, ""), negated: match[1] === "-" } : null;
    };
    const matchesKey = (raw: string) => {
      const parsed = qualifier(raw);
      return !!parsed && parsed.key === compose.key && parsed.negated === !!compose.negated;
    };
    const matchesValue = (raw: string) => {
      const parsed = qualifier(raw);
      return matchesKey(raw) && parsed?.value === compose.value;
    };
    if (operation === "set") {
      parts = parts.filter((raw) => !matchesKey(raw));
    } else if (operation === "replace-group") {
      parts = parts.filter((raw) => {
        const parsed = qualifier(raw);
        return !parsed || !matchesKey(raw) || !compose.replace_values?.includes(parsed.value);
      });
    } else if (operation === "remove") {
      parts = parts.filter((raw) => !matchesValue(raw));
    } else if (operation === "toggle" && parts.some(matchesValue)) {
      parts = parts.filter((raw) => !matchesValue(raw));
      continue;
    }
    if (compose.value && !parts.some(matchesValue)) {
      const value = /\s/.test(compose.value) ? `"${compose.value}"` : compose.value;
      parts.push(`${compose.negated ? "-" : ""}${compose.key}:${value}`);
    }
  }
  return parts.join(" ");
}

async function setup(
  context: BrowserContext,
  options: {
    preferences?: Record<string, unknown>;
    permissions?: string[];
    isAdmin?: boolean;
  } = {},
) {
  let preferences = options.preferences || {};
  let searchRequests = 0;
  const searchUrls: URL[] = [];
  const preferenceWrites: Record<string, unknown>[] = [];
  await context.addCookies([{ name: "ag_session", value: "fixture", domain: "127.0.0.1", path: "/" }, { name: "ag_csrf", value: "fixture-csrf", url: process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:13000" }]);
  await context.addInitScript(() => {
    localStorage.setItem("auto-gallery-lang", "en");
  });
  await context.route("**/media/**", route => route.fulfill({contentType:"image/gif",body:Buffer.from("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==","base64")}));
  await context.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/auth/me" && route.request().method() === "GET") {
      return json(route, {
        id: 1,
        username: "works-controls",
        display_name: "Works Controls",
        is_admin: options.isAdmin ?? true,
        is_active: true,
        permissions: options.permissions || ["library", "curation", "system"],
        modules: { library: true, curation: options.permissions?.includes("curation") ?? true, system: options.permissions?.includes("system") ?? true },
        preferences,
        nsfw_visible: true,
        upload_used_bytes: 0,
        must_change_password: false,
      });
    }
    if (path === "/api/v1/auth/me/preferences" && route.request().method() === "PUT") {
      const payload = route.request().postDataJSON() as { preferences: Record<string, unknown> };
      preferences = payload.preferences;
      preferenceWrites.push(preferences);
      return json(route, payload);
    }
    if (path === "/api/v1/auth/ws-ticket") return json(route, { detail: "offline" }, 503);
    if (path === "/api/v1/works/derivative-progress") {
      return json(route, { queued: 0, running: 0, failed: 0, complete: 0, remaining: 0, total: 0, affected_works: 0, completion_percent: 100, status: "idle" });
    }
    if (path === "/api/v1/search") {
      searchRequests += 1;
      searchUrls.push(url);
      const query = url.searchParams.get("q") || "";
      return json(route, {
        query,
        canonical_query: query,
        parsed: { raw: query, canonical: query, scope: "works", targets: ["works"], tokens: parsedTokens(query) },
        groups: { works: { total: works.length, items: works, next_cursor: null, previous_cursor: null } },
        total: works.length,
        seed: url.searchParams.has("seed") ? Number(url.searchParams.get("seed")) : null,
        results: works,
        creators: [],
        tags: [],
        repositories: [],
        subscriptions: [],
      });
    }
    if (path === "/api/v1/search/assist") {
      const payload = route.request().postDataJSON() as { before_cursor?: string; compose?: Compose; composes?: Compose[] };
      const query = payload.before_cursor || "";
      const operations = payload.composes || (payload.compose ? [payload.compose] : []);
      const canonical = operations.length ? composeQuery(query, operations) : query;
      return json(route, { query: canonical, canonical_query: canonical, parsed: { tokens: parsedTokens(canonical) }, diagnostics: [], suggestions: [] });
    }
    return json(route, { detail: `Unhandled fixture request: ${route.request().method()} ${path}` }, 501);
  });
  return {
    searchRequests: () => searchRequests,
    searchUrls,
    preferenceWrites,
  };
}

for (const width of [1440,390]) test(`display popover fits ${width}px and restores keyboard focus`,async({context,page})=>{
 await setup(context);await page.setViewportSize({width,height:844});await page.goto('/admin/works');
 const trigger=page.getByRole('button',{name:'Display',exact:true});await trigger.focus();await page.keyboard.press('Enter');
 const menu=page.getByRole('dialog',{name:'Display settings',exact:true});await expect(menu).toBeVisible();
 await expect(menu.getByRole('radio',{name:'Masonry',exact:true})).toBeFocused();
 const box=await menu.boundingBox();expect(box).not.toBeNull();expect(box!.x).toBeGreaterThanOrEqual(0);expect(box!.x+box!.width).toBeLessThanOrEqual(width);expect(box!.y+box!.height).toBeLessThanOrEqual(844);
 await page.keyboard.press('Escape');await expect(menu).toHaveCount(0);await expect(trigger).toBeFocused();
 await trigger.click();await page.locator('main').click({position:{x:5,y:5}});await expect(menu).toHaveCount(0);
});

async function advancedDisplay(page: import('@playwright/test').Page){
 await page.getByRole('button',{name:'Display',exact:true}).click();
 await page.getByRole('dialog',{name:'Display settings',exact:true}).getByRole('button',{name:'Display settings',exact:true}).click();
 return page.getByRole('dialog',{name:'Display settings',exact:true});
}

test('display preferences persist without requesting works again',async({context,page})=>{
 const fixture=await setup(context);await page.goto('/admin/works');await expect(page.locator('[data-work-id="work-sfw"]')).toBeVisible();const initial=fixture.searchRequests();
 await page.getByRole('button',{name:'Display',exact:true}).click();await page.getByText('Grid',{exact:true}).click();
 const dialog=await advancedDisplay(page);await dialog.getByRole('combobox',{name:'Card size',exact:true}).click();await page.getByRole('option',{name:'Small',exact:true}).click();await dialog.getByLabel('AI',{exact:true}).uncheck();await dialog.getByRole('button',{name:'Done',exact:true}).click();
 await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','grid');
 await expect.poll(()=>fixture.preferenceWrites.at(-1)?.appearance).toMatchObject({worksViewMode:'grid',workCardSize:'small',workCardShowAi:false});
 expect(fixture.searchRequests()).toBe(initial);
 await page.reload();await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','grid');await expect(page.locator('.work-collection')).toHaveAttribute('data-card-size','small');
 await expect(page.locator('[data-work-id="work-sfw"]').getByText('AI assisted',{exact:true})).toHaveCount(0);
});

test('saved appearance fills unspecified settings from current defaults',async({context,page})=>{
 await setup(context,{preferences:{appearance:{worksViewMode:'list',workCardSize:'large',workCardShowAi:false}}});await page.goto('/admin/works');
 await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','list');await expect(page.locator('.work-collection')).toHaveAttribute('data-card-size','large');
 await expect(page.locator('[data-work-id="work-sfw"]').getByText('AI assisted',{exact:true})).toHaveCount(0);
 await page.getByRole('button',{name:'Display',exact:true}).click();await expect(page.getByRole('radio',{name:'List',exact:true})).toBeChecked();await expect(page.getByLabel('Blur adult content')).toBeChecked();
});

test('card badge and blur preferences update existing images without refetching works',async({context,page})=>{
 const fixture=await setup(context);await page.goto('/admin/works');const sfw=page.locator('[data-work-id="work-sfw"]'),adult=page.locator('[data-work-id="work-nsfw"]');
 await expect(sfw.getByText('AI assisted',{exact:true})).toBeVisible();await expect(adult.getByText('Adult content',{exact:true})).toBeVisible();await expect(adult.locator('img')).toHaveClass(/blurred/);
 const image=await adult.locator('img').elementHandle(),initial=fixture.searchRequests();
 await page.getByRole('button',{name:'Display',exact:true}).click();await page.getByLabel('Blur adult content').uncheck();await page.getByRole('dialog',{name:'Display settings',exact:true}).getByRole('button',{name:'Display settings',exact:true}).click();const dialog=page.getByRole('dialog',{name:'Display settings',exact:true});
 await dialog.getByLabel('AI',{exact:true}).uncheck();await dialog.getByLabel('NSFW',{exact:true}).uncheck();await dialog.getByLabel('Show favorite marks',{exact:true}).uncheck();await dialog.getByRole('button',{name:'Done',exact:true}).click();
 await expect(sfw.getByText('AI assisted',{exact:true})).toHaveCount(0);await expect(adult.getByText('Adult content',{exact:true})).toHaveCount(0);await expect(sfw.getByRole('button',{name:'Unfavorite: Harbor light',exact:true})).toHaveCount(0);await expect(adult.locator('img')).not.toHaveClass(/blurred/);
 expect(await image!.evaluate(node=>node.isConnected)).toBe(true);expect(fixture.searchRequests()).toBe(initial);
});

test('filter draft cancellation and applying controlled qualifiers preserve unrelated search terms',async({context,page})=>{
 await setup(context);const original='cat tag:sky creator:"Ada Lovelace" after:2025-01-01 -source:weibo -has:animation -is:favorite source:x is:nsfw is:ai sort:title-asc';await page.goto('/admin/works?q='+encodeURIComponent(original));
 const open=async()=>{await page.getByRole('combobox',{name:'Work source',exact:true}).click();await page.getByRole('option',{name:'Filter',exact:true}).click();return page.getByRole('dialog');};
 let dialog=await open();await dialog.getByLabel('Pixiv',{exact:true}).check();await dialog.getByRole('button',{name:'Cancel',exact:true}).click();expect(new URL(page.url()).searchParams.get('q')).toBe(original);
 dialog=await open();await expect(dialog.getByLabel('Pixiv',{exact:true})).not.toBeChecked();await dialog.getByLabel('Pixiv',{exact:true}).check();
 await dialog.getByRole('combobox',{name:'Filters',exact:true}).click();await page.getByRole('option',{name:'SFW',exact:true}).click();
 await dialog.getByRole('combobox',{name:'AI generation',exact:true}).click();await page.getByRole('option',{name:'Non-AI',exact:true}).click();await dialog.getByLabel('Favorites',{exact:true}).check();await dialog.getByLabel('Image',{exact:true}).check();await dialog.getByLabel('Video',{exact:true}).check();await dialog.getByRole('button',{name:'Apply',exact:true}).click();
 await expect.poll(()=>new URL(page.url()).searchParams.get('q')).toContain('source:pixiv');const q=new URL(page.url()).searchParams.get('q')!;
 for(const token of ['cat','tag:sky','creator:"Ada Lovelace"','after:2025-01-01','-source:weibo','-has:animation','-is:favorite','source:x','source:pixiv','is:sfw','is:human','is:favorite','has:image','has:video','sort:title-asc'])expect(q).toContain(token);
 expect(q).not.toContain('is:nsfw');expect(q).not.toContain('is:ai');
});

test('sort selection maintains random seeds and removes them when returning to a fixed sort',async({context,page})=>{
 const fixture=await setup(context);await page.goto('/admin/works?q=tag%3Asky');
 const select=async(name:string)=>{await page.getByRole('combobox',{name:'Sort',exact:true}).click();await page.getByRole('option',{name,exact:true}).click();};
 await select('Random');await expect.poll(()=>new URL(page.url()).searchParams.get('seed')).toMatch(/^\d+$/);const first=new URL(page.url()).searchParams.get('seed');
 await expect.poll(()=>fixture.searchUrls.at(-1)?.searchParams.get('seed')).toBe(first);
 await select('Reshuffle');await expect.poll(()=>new URL(page.url()).searchParams.get('seed')).not.toBe(first);
 await select('Recently imported');await expect.poll(()=>new URL(page.url()).searchParams.get('seed')).toBeNull();
 await expect.poll(()=>new URL(page.url()).searchParams.get('q')).toBe('tag:sky');
 await expect(page.getByRole('combobox',{name:'Sort',exact:true})).toContainText('Recently imported');
});

test('search clear resets paging and random seed while preserving explicit view',async({context,page})=>{
 await setup(context,{preferences:{appearance:{worksViewMode:'masonry',workCardSize:'large'}}});await page.goto('/admin/works?q='+encodeURIComponent('cat sort:random')+'&seed=123&view=list');
 await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','list');await page.getByRole('button',{name:'Clear search'}).click();await expect.poll(()=>new URL(page.url()).searchParams.has('q')).toBe(false);
 await expect.poll(()=>new URL(page.url()).searchParams.has('seed')).toBe(false);const params=new URL(page.url()).searchParams;expect(params.has('p')).toBe(false);expect(params.get('view')).toBe('list');
 await expect(page.locator('.work-collection')).toHaveAttribute('data-card-size','large');
});

test('explicit view overrides saved layout until the user chooses another view',async({context,page})=>{
 await setup(context,{preferences:{appearance:{worksViewMode:'masonry'}}});await page.goto('/admin/works?view=list');await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','list');
 await page.getByRole('button',{name:'Display',exact:true}).click();await expect(page.getByRole('radio',{name:'List',exact:true})).toBeChecked();await page.getByText('Grid',{exact:true}).click();
 await expect(page.locator('.work-collection')).toHaveAttribute('data-works-layout','grid');expect(new URL(page.url()).searchParams.has('view')).toBe(false);
 await expect.poll(()=>page.evaluate(()=>JSON.parse(localStorage.getItem('auto-gallery-appearance-v1')!).worksViewMode)).toBe('grid');
});

test('hiding selection controls clears selection and readers cannot curate',async({context,page})=>{
 await setup(context);await page.goto('/admin/works');await page.getByRole('button',{name:'Select works',exact:true}).click();await page.getByRole('checkbox',{name:'Select work: Harbor light',exact:true}).check();
 const selection=page.locator('.layout-selection-bar');await expect(selection).toContainText('1');const dialog=await advancedDisplay(page);await dialog.getByLabel('Show selection controls',{exact:true}).uncheck();await dialog.getByRole('button',{name:'Done',exact:true}).click();
 await expect(selection).toHaveCount(0);
 const restricted=await context.browser()!.newContext();try{await setup(restricted,{permissions:['library'],isAdmin:false});const p=await restricted.newPage();await p.goto('/admin/works');await expect(p.getByRole('button',{name:'Select works',exact:true})).toHaveCount(0);await expect(p.locator('.favorite-button')).toHaveCount(0);}finally{await restricted.close();}
});

test("favorite requests lock only their own work", async ({ context, page }) => {
  await setup(context);
  const calls: string[] = [];
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await context.route("**/api/v1/works/*/favorite", async route => {
    const id = new URL(route.request().url()).pathname.split("/")[4];
    calls.push(id);
    await pending;
    await json(route, { id, is_favorite: id !== "work-sfw" });
  });
  try {
    await page.goto("/admin/works");
    const first = page.locator("article").filter({ hasText: works[0].title });
    const second = page.locator("article").filter({ hasText: works[1].title });
    await first.getByRole("button", { name: "Unfavorite: Harbor light", exact: true }).click();
    const other = second.getByRole("button", { name: "Favorite: Portrait study", exact: true });
    await expect(other).toBeEnabled();
    await other.click();
    await expect.poll(() => calls).toEqual(["work-sfw", "work-nsfw"]);
  } finally { release(); }
});


test("favorite updates immediately and serializes rapid changes", async ({ context, page }) => {
  await setup(context);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  let serverFavorite = true;
  await context.route("**/api/v1/works/work-sfw/favorite", async (route) => {
    calls += 1;
    if (calls === 1) await gate;
    serverFavorite = !serverFavorite;
    works[0].is_favorite = serverFavorite;
    await json(route, { id: "work-sfw", is_favorite: serverFavorite });
  });
  try {
    await page.goto("/admin/works");
    const card = page.locator("article").filter({ hasText: works[0].title });
    const favorite = card.getByRole("button", { name: "Unfavorite: Harbor light", exact: true });
    await favorite.focus();
    await page.keyboard.press("Enter");
    const next = card.getByRole("button", { name: "Favorite: Harbor light", exact: true });
    await expect(next).toHaveAttribute("aria-pressed", "false");
    expect(await next.evaluate((node) => getComputedStyle(node).cursor)).not.toBe("progress");
    await next.click();
    await expect(card.getByRole("button", { name: "Unfavorite: Harbor light", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect(calls).toBe(1);
    release();
    await expect.poll(() => calls).toBe(2);
    await expect(card.getByRole("button", { name: "Unfavorite: Harbor light", exact: true })).toHaveAttribute("aria-pressed", "true");
  } finally { release(); works[0].is_favorite = true; }
});

test("favorite failure restores the confirmed state", async ({ context, page }) => {
  await setup(context);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await context.route("**/api/v1/works/work-sfw", route => json(route, works[0]));
  await context.route("**/api/v1/works/work-sfw/favorite", async route => {
    await gate;
    await json(route, { detail: "Favorite failed" }, 500);
  });
  try {
    await page.goto("/admin/works");
    const card = page.locator("article").filter({ hasText: works[0].title });
    await card.getByRole("button", { name: "Unfavorite: Harbor light", exact: true }).click();
    await expect(card.getByRole("button", { name: "Favorite: Harbor light", exact: true })).toBeVisible();
    release();
    await expect(card.getByRole("button", { name: "Unfavorite: Harbor light", exact: true })).toBeVisible();
    await expect(card.getByRole("alert")).toBeVisible();
  } finally { release(); }
});


test("favorite filter reflects confirmed removal", async ({ context, page }) => {
  await setup(context);
  let favorite = true;
  await context.route(/\/api\/v1\/search(?:\?.*)?$/, route => {
    const items = favorite ? [{ ...works[0], is_favorite: true }] : [];
    return json(route, { query: "is:favorite", canonical_query: "is:favorite", parsed: { tokens: [{kind:"qualifier",key:"is",value:"favorite",negated:false}] }, groups: { works: { total: items.length, items } }, total: items.length, results: items });
  });
  await context.route("**/api/v1/works/work-sfw/favorite", route => { favorite = false; return json(route, {id:"work-sfw", is_favorite:false}); });
  await page.goto("/admin/works?q=is%3Afavorite");
  await page.getByRole("button", { name: "Unfavorite: Harbor light" }).click();
  await expect(page.locator('[data-work-id="work-sfw"]')).toHaveCount(0);
});

test("lost favorite response reconciles server state without toggling again", async ({ context, page }) => {
  await setup(context);
  let favorite = true; let calls = 0;
  await context.route("**/api/v1/works/work-sfw", route => json(route, { ...works[0], is_favorite: favorite }));
  await context.route(/\/api\/v1\/search(?:\?.*)?$/, route => {
    const items = [{...works[0],is_favorite:favorite},works[1]];
    return json(route, { query:"",canonical_query:"",parsed:{tokens:[]},groups:{works:{total:2,items}},total:2,results:items });
  });
  await context.route("**/api/v1/works/work-sfw/favorite", route => { calls++; favorite = false; return route.abort("timedout"); });
  await page.goto("/admin/works");
  const card=page.locator('[data-work-id="work-sfw"]');
  await card.getByRole("button", { name: "Unfavorite: Harbor light" }).click();
  await expect(card.getByRole("button", { name: "Favorite: Harbor light" })).toBeVisible();
  await expect(card.getByRole("alert")).toBeVisible();
  expect(calls).toBe(1);
});


test("favorite keeps existing thumbnail and avoids unnecessary list requests",async({page,context})=>{
 const fixture=await setup(context);await context.route('**/media/**',route=>route.fulfill({status:200,contentType:'image/gif',body:Buffer.from('R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==','base64')}));await context.route('**/api/v1/works/work-sfw/favorite',route=>json(route,{id:'work-sfw',is_favorite:false}));
 await page.goto('/admin/works');const card=page.locator('[data-work-id="work-sfw"]');await expect(card).toBeVisible();
 const image=await card.locator('img').elementHandle();const before=fixture.searchRequests();
 await card.getByRole('button',{name:'Unfavorite: Harbor light'}).click();
 await expect(card.getByRole('button',{name:'Favorite: Harbor light'})).toBeVisible();
 await expect(card.getByRole('button',{name:'Favorite: Harbor light'})).not.toHaveAttribute('aria-busy','true');
 expect(fixture.searchRequests()).toBe(before);
 expect(await image!.evaluate(node=>node.isConnected)).toBe(true);
});
