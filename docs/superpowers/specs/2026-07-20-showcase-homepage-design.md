# 展示页首页 + 通用幻灯片 设计文档
# Showcase Homepage & Universal Slideshow — Design Spec

> 日期: 2026-07-20 · 分支: `frontend-motion`(实施时切新分支)
> 经 brainstorming 逐题确认的决策:展示页占 `/` 且仍需登录;视觉取到 **WebGL 扭曲/流体**层级;幻灯片做**通用全屏播放器、多处可启动**;设置页覆盖**内容源筛选 / 动效强度 / 幻灯片参数 / 首页行为**四组配置。

---

## 一、目标与非目标

**目标**:把落地页从数据密集的仪表盘换成以作品为主角的沉浸式展示页(参考 makemepulse 的鼠标跟随图片流质感),并让"看图"这件事在画师维度有一个全屏幻灯片形态。

**非目标**(本 spec 明确不做):
- 不改仪表盘本身(它留在 `/admin`,功能与布局原样)
- 不做滚动驱动的多场景叙事(brainstorming 中被排除的选项)
- 不引入虚拟滚动、不改 works 列表分页
- 不改现有六套配色/明暗主题与 GitHub 外壳

---

## 二、现状事实(实施依据)

| 事实 | 影响 |
|---|---|
| `app/page.tsx` 当前只有 `redirect("/admin")` | 展示页直接替换此文件即可,天然在 admin 布局之外(无侧栏,适合全屏) |
| middleware 用 `ag_token` cookie 门控,`/` 不在 `PUBLIC_PREFIXES` | `/` 已需登录,**鉴权零改动** |
| `/media/thumb/{id}` 公开;`/media/preview/{id}?expires=&token=` 用 HMAC 签名;`signed_media_url(asset_id,size,ttl)` 已存在 | WebGL 纹理可用签名 URL 直接加载,无需请求头 |
| works 列表 API 只有 `sort_by/sort_order`,**无随机取样** | 需新增取样端点 |
| 库存量级约 3.6 万作品 | `ORDER BY random()` 会全表扫,需随机窗口策略 |
| 用户 `preferences` JSONB + `PUT /me/preferences`,白名单 `{theme,palette,lang,appearance}` | 展示配置加一个 `showcase` 键即可复用整套偏好同步 |
| 已有 motion 体系:`shouldAnimate()` 门控、`usePresence`、reduced-motion 三层、零 long task 基线 | 新动效必须并入该体系,不得另起炉灶 |

---

## 三、架构:四个边界清晰的单元

### ① 后端取样端点

**文件**:`backend/app/api/showcase.py`(新),注册进 `app/api/__init__.py`

```
GET /api/v1/showcase/sample
  ?count=24&scope=all|favorites&source=&tag=&include_nsfw=false
```

- 挂 `RequirePermission("library")`——鉴权、策展可见性、NSFW 全部沿用现有体系,**零安全面变化**
- `include_nsfw` 受账号 `nsfw_visible` **硬限**:`force_sfw = not user.nsfw_visible or not include_nsfw`
- **取样策略(随机窗口)**:
  1. 取缓存的作品总数(复用现有 works count 缓存)
  2. 随机偏移 `o = randint(0, max(0, total - count))`
  3. 复用 `WorkRepository.list_all(offset=o, limit=count, ...)` 取一窗(索引扫描)
  4. 服务端窗内洗牌后返回

  取的是随机**窗口**而非随机**行**,但配合每次请求换窗 + 窗内洗牌,观感足够随机,且是毫秒级索引扫描而非全表扫。
- 响应(轻载荷,`width/height` 随载荷返回以消除 CLS):
  ```json
  { "items": [ { "work_id": "<uuid>", "title": "...", "creator_name": "...",
                 "source": "pixiv", "thumb_url": "/media/thumb/<asset>",
                 "preview_url": "/media/preview/<asset>?expires=<ts>&token=<hmac>",
                 "width": 1200, "height": 1600 } ] }
  ```
- 签名 TTL 取现有 `MEDIA_SIGNED_URL_TTL_SECONDS` 默认值;前端在图片 401 时整批重取

### ② WebGL 展示页(`/`)

**路由**:改写 `admin-web/src/app/page.tsx`(移除 redirect),在 admin 布局之外 → 全屏无侧栏。

**文件结构**:
```
src/app/page.tsx                       # 展示页装配(thin)
src/components/showcase/
  ShowcaseCanvas.tsx                   # WebGL 图层(客户端组件)
  ShowcaseTrailDOM.tsx                 # DOM 降级版图片流
  ShowcaseHero.tsx                     # 大字排版 / 库存统计 / 入口链接
  ShowcaseEmpty.tsx                    # 空库引导
src/lib/showcase/
  webgl.ts                             # ogl 唯一入口:装配、纹理池、rAF 循环
  trail.ts                             # 拖尾状态机(生成/寿命/封顶),两版共用
```

**技术选型**:**ogl**(~15kB gz)而非 three.js(~150kB gz)——只需一个 plane + 自定义 shader,three 是杀鸡用牛刀。**动态 import 懒加载**,只在展示页拉取,复刻 Phase 1 animejs 的懒 chunk 手法,其余路由 bundle 不受影响。

**效果**:鼠标移动时沿轨迹生成图片平面,随寿命淡出;shader 做 RGB 通道位移 + 基于鼠标速度的流体式扭曲;背景层轻微视差。

**降级契约(非可选,必须实现)**:

| 条件 | 行为 |
|---|---|
| `prefers-reduced-motion: reduce` | 不启动 rAF、不加载 ogl;渲染静态精选网格 |
| `hardwareConcurrency ≤ 4`(`shouldAnimate()` 判低端) | 走 `ShowcaseTrailDOM`(transform/opacity) |
| WebGL 上下文创建失败 / `webglcontextlost` | 静默切 DOM 版,无错误 UI |
| `document.hidden` | 暂停 rAF |

纹理经 `createImageBitmap` 离主线程解码,拖尾数量按配置封顶,`pointermove` 节流——守住 MU-T2 建立的零 long task 基线。

### ③ 通用全屏幻灯片

**文件**:`src/components/SlideshowPlayer.tsx` + `src/lib/useSlideshow.ts`(启动器)

- 入参:`items`(作品/资产列表)、`startIndex`、`onClose`;配置读自用户偏好
- 转场:Ken Burns(transform 缩放+平移)/ 交叉淡入,二选一可配
- 交互:自动播放(停留时长可配)、← / → / 空格(暂停) / Esc(退出)、循环开关、元信息浮层开关
- enter/exit 复用现有 `usePresence`;reduced-motion 下转场退化为瞬时切换,自动播放仍可用
- **三处入口**(一份实现):画师详情页、作品列表页、标签页

### ④ 设置子页

**文件**:`src/app/admin/settings/showcase/page.tsx`;设置索引页加卡片入口。

**存储**:用户 `preferences.showcase`。后端仅需把 `"showcase"` 加入 `backend/app/api/auth_api.py` 的 `_ALLOWED_PREFERENCE_KEYS`(一行),整套 Task 7 偏好同步(服务端优先读 + 防抖回写)直接复用。

**配置结构**:
```ts
showcase: {
  // 内容源
  scope: "all" | "favorites",
  source: string | null,          // 限定来源
  tag: string | null,             // 限定标签
  includeNsfw: boolean,           // 受账号 nsfw_visible 硬限
  // 动效
  trailMax: number,               // 拖尾图片数上限
  spawnIntervalMs: number,        // 生成间隔
  followDamping: number,          // 跟随阻尼 0-1
  parallaxStrength: number,       // 视差强度 0-1
  minimal: boolean,               // 一键极简(等效关闭 WebGL 走静态)
  // 幻灯片
  slideDwellMs: number,
  slideTransition: "crossfade" | "kenburns",
  slideLoop: boolean,
  slideShowMeta: boolean,
  // 首页行为
  landing: "showcase" | "dashboard",   // 登录后默认落地页
  headline: string,                    // 展示页标题文案
  showStats: boolean,                  // 是否显示库存统计
}
```

`landing: "dashboard"` 时,登录成功后跳 `/admin` 而非 `/`(改 `auth.tsx` 登录后跳转目标读该偏好)。

---

## 四、数据流

```
展示页挂载
  → useQuery(GET /showcase/sample?count&filters from prefs, 长 staleTime)
  → items(含已签名 preview URL + 宽高)
  → createImageBitmap 解码 → WebGL 纹理池(LRU,上限 = trailMax * 2)
  → pointermove(节流)→ trail.ts 生成拖尾(封顶)→ rAF 渲染
  → 空闲时定期重取,保持随机新鲜度
```

---

## 五、错误处理

| 场景 | 行为 |
|---|---|
| WebGL 创建失败 / 上下文丢失 | 静默降级 DOM 版,无错误 UI |
| 空库(无作品) | `ShowcaseEmpty`:引导去添加订阅或上传 |
| 签名 URL 过期(图片 401) | 整批重取 sample |
| 单张纹理加载失败 | 跳过该图,不阻塞其余渲染 |
| sample 接口报错 | 展示 `ErrorState` + 重试,不白屏 |

---

## 六、测试与验收

- **后端 pytest**(`backend/tests/test_showcase_api.py`):取样遵守 `nsfw_visible` 硬限与 curation 可见性;`scope=favorites`/`source`/`tag` 筛选生效;返回数量 ≤ count;签名 URL 可通过 `verify_media_token`;空库返回空数组不报错
- **前端**:`tsc --noEmit` + `npm run build` 绿
- **性能与可访问性验收(硬闸门)**:复用 MU-T2 的 `perf-trace.js` 对 `/` 录制——要求 **long task 仍为 0**、reduced-motion 下 `document.getAnimations()` 运行中动画为 0 且不加载 ogl chunk、CLS < 0.1(宽高随载荷返回,不应有位移)
- **手工回归**:六套配色 + 明暗下展示页与幻灯片观感;键盘操作幻灯片;WebGL 强制关闭时降级正常

---

## 七、风险与取舍

| # | 风险 | 缓解 |
|---|---|---|
| S1 | WebGL 在不同设备/浏览器表现差异大 | 降级契约四条兜底,DOM 版是一等公民而非事后补丁 |
| S2 | ogl 体积影响其他路由 | 动态 import 懒加载,构建后核验其余路由 bundle 不变 |
| S3 | 3.6 万作品取样性能 | 随机窗口(索引扫描)而非 `ORDER BY random()`;涨到百万级再换 `TABLESAMPLE SYSTEM` |
| S4 | 签名 URL TTL 短于展示会话 | 图片 401 触发整批重取;必要时为 showcase 单独放宽 TTL |
| S5 | 首页改动影响既有书签/流程 | 仪表盘原样留在 `/admin`;`landing` 偏好可让老用户仍落到仪表盘 |
| S6 | 展示页动效与既有零 long task 基线冲突 | 纹理离线程解码、拖尾封顶、rAF 页面隐藏即停;验收用 MU-T2 录制把关 |

---

## 八、实施顺序(供 writing-plans 参照)

```
1 后端取样端点 + pytest
2 偏好白名单加 showcase + 设置子页(先有配置,后续页面直接读)
3 展示页骨架:路由 + Hero + DOM 版图片流 + 空/错状态(先保证无 WebGL 也完整可用)
4 WebGL 图层 + 降级切换(在已可用的 DOM 版之上增强)
5 通用幻灯片播放器 + 三处入口
6 性能录制验收 + 文档/记账
```

先做 DOM 版再叠 WebGL,保证任何时候中断都有一个完整可用的展示页,且降级路径天然被验证过。
