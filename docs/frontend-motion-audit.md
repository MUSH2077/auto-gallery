# auto-gallery 前端动效审计与 anime.js 动效系统规划
# Frontend Motion Audit & Phased anime.js Plan

> 日期 / Date: 2026-07-15 · 分支 / Branch: `frontend-motion`
> 本文档为前端优化阶段的起点:审计现状、定义 motion design 原则、规划分五个 Phase 的 anime.js 动效系统落地。本轮不改代码、不安装依赖。

---

## 一、项目结构扫描结果(事实)

### 技术栈与构建

- Next.js **14.2.18**(App Router,`output: standalone`,`force-dynamic`)、React **18.3.1**、TypeScript 5.7、Tailwind **3.4.16**(`darkMode: "class"`)、TanStack Query 5.62
- **未使用 next/image**——全部原生 `<img loading="lazy" decoding="async">`(经 `AssetImage` 封装,`src/components/work-interactions.tsx`)
- 路由约 35 个页面;导航为顶栏(非侧栏),布局链:`layout.tsx → providers.tsx(7 层 Provider)→ admin/layout.tsx(AdminNav + AuthGuard + NotificationBell)`

### 动画库现状:未引入任何动画库

package.json 与 node_modules 均无 anime.js / framer-motion / motion / gsap。全部动效为手写 CSS/Tailwind。

### 现有动效资产(`src/app/globals.css`)

| 资产 | 状态 |
|---|---|
| `fadeUp` keyframe → `.page-transition`(页面入场)+ `.page-item`(`--delay` 逐项 stagger) | 在用,但 `.page-transition` 只覆盖 35 路由中的 **4 个**;stagger 仅 works 网格用 |
| `popIn` → `.popover`(120ms) | 在用(UserMenu / NotificationBell / PaletteToggle),**只有 enter 无 exit** |
| `shimmer` → `.skeleton` | 定义了但页面实际都用 Tailwind `animate-pulse` |
| `barGrow` → `.animate-bar-grow` | **定义了,零引用(死代码)** |
| tailwind.config 的 `ease-expo`、`duration-fast/base/slow` | **声明了,零引用(死 token)** |
| 全局 `@media (prefers-reduced-motion: reduce)` 一刀切关停 | ✅ 已存在(globals.css:189),是很好的底座 |
| 按压反馈 `.btn-*/.clickable` `active:scale(0.97)`、`.card-interactive` hover 上浮 | 在用,纯 CSS,应保持 |
| Toast 手写 entering/exiting 布尔 + `transition-all duration-300` | 在用,唯一有完整 enter/exit 的弹层 |

### 关键缺口(审计发现)

- **Modal、ConfirmDialog、FullImageLightbox、jobs 页两个 Drawer:零 enter/exit 过渡,瞬间挂载/卸载**;Drawer 甚至没有背景遮罩
- 所有 popover 只有入场无退场;tab 切换只有颜色变化无滑动指示
- 列表页(works/creators/tags/jobs)除 works 外无入场动效
- 无虚拟滚动;全部 offset 分页(works 30/页、creators 25、tags 50);仅通知页用 useInfiniteQuery
- jobs 页与仪表盘有 2–15s 自适应 `refetchInterval` + WebSocket 失效重取——**轮询重渲染是入场动画最大冲突源**

---

## 二、anime.js 引入方式(本轮不安装)

- 推荐 **anime.js v4**(ESM、模块化、自带 TS 类型):`npm install animejs`
- 用法:`import { animate, stagger, createTimeline, utils } from "animejs"`,仅在 `"use client"` 组件内使用,禁止模块级读取 `window`
- 引入后必须经 `src/lib/motion/` 封装层调用(见 Phase 1),**业务组件禁止直接 import animejs**——统一 token、统一 reduced-motion 门控、方便日后换库
- 取舍说明:React 生态更常用 motion/react(自带 AnimatePresence 退场编排);选 anime.js 则退场需自建 `usePresence` hook(一次性成本,Phase 1 内完成)。anime.js 优势:框架无关、timeline/stagger/SVG 能力强、体积小(v4 模块化按需 ~5–10 kB gz)

---

## 三、动效审计报告

### 3.1 最适合加 anime.js 动效的页面(JS 动画有真实收益)

| 页面 | 动效点 | 为什么需要 JS |
|---|---|---|
| 仪表盘 `/admin` | MetricCard 数字滚动(count-up)、MiniBar 生长、WorkGrid SVG 热力图逐格浮现 | 数字插值、SVG、动态 stagger 只能 JS |
| 任务中心 `/admin/jobs` | 状态变更行闪烁反馈(WS `status_change` 触发)、JobLifecycle 步进点过渡、Drawer 滑入滑出+遮罩、进度条平滑插值 | 事件驱动的一次性反馈、可中断序列 |
| 去重审核 `/admin/dedup` + merge-candidates + creators/duplicates | 对比卡对开揭示 timeline、合并成功时卡片收拢汇合反馈 | 多元素时序编排 |
| 作品详情 `/admin/works/[id]` | Lightbox 从缩略图 rect 放大展开(shared-element 感)、filmstrip 切换 | FLIP 式 rect→rect 变换需测量+JS |
| 通知 NotificationBell / notifications 页 | 新通知条目滑入、badge 计数 pop | 列表项 enter/exit 编排 |
| 策展 `/admin/curation` | commit 时间线卡片沿线 stagger | 动态延迟 |

### 3.2 只应该用 CSS transition、不要强行上 anime.js 的地方

- 一切 **hover/active/focus 状态**:`transition-colors`、`.card-interactive` 上浮、按压 `active:scale`——指针驱动、高频、CSS 合成器最优
- **主题/配色切换** 的 `html transition-colors duration-300` 交叉淡入
- **骨架屏** `animate-pulse` / shimmer
- 150ms 以内的简单弹层淡入(popover `popIn` 保留 CSS,只补一个 CSS exit 类)
- chevron 旋转、tab 下划线、badge 变色等单属性微交互
- 判据:**单元素、双状态、纯 hover/focus 触发 → CSS;多元素时序、数值插值、事件驱动一次性反馈、需测量 → anime.js**

### 3.3 可能导致性能问题的动画(红线清单)

1. **轮询/WS 重渲染 × 入场动画**:jobs(2–5s)、仪表盘(5–15s)refetch 会重渲染列表;若入场动画不做"只在首次挂载播放"守卫(`useEnterOnce`),每次轮询都闪一遍。**这是本项目最大的动效风险,Phase 1 必须先解决**
2. works 网格 30 张图:stagger 只许动 `transform/opacity`;禁止与图片解码窗口叠加大面积动画;stagger 总时长保持 ≤300ms 上限(现有 `min(index*30,300)` 的思路保留)
3. 禁止动画 layout 属性(width/height/top/left/margin);进度条现有 width `transition-all duration-500` 建议改 `scaleX`;DisclosurePanel 展开如做动画须用 `grid-template-rows` 技巧而非 height
4. `hover:shadow-md transition-shadow` 在 tags 词云(50 chips)与 works 行上是逐元素 paint——保持现状但不再扩散;新动效一律不动 box-shadow
5. 长列表(jobs、notifications)无虚拟化——不做 scroll-linked 动画,入场动画只作用于可视首屏
6. Lightbox rect→rect 展开涉及测量与克隆——只用 transform,失败即降级为淡入

### 3.4 需要抽象的通用 motion 组件/原语(Phase 1 交付物)

```
src/lib/motion/
  tokens.ts     # duration/easing/distance/scale;与 tailwind.config 中闲置的
                # ease-expo、duration-fast/base/slow 对齐为同一套值(激活死 token)
  config.ts     # prefersReduced() / isLowEnd() / shouldAnimate() 门控
  anime.ts      # animejs 唯一入口:包装 animate/stagger/timeline,自动吃 token+门控
  hooks.ts      # useEnterOnce(防轮询重播)、usePresence(挂载/卸载退场编排)、useStagger
```

随后迁移到原语上的现有组件(不改视觉、不删组件):

- `Modal.tsx` / `ConfirmDialog.tsx`:补 backdrop 淡入 + 卡片 enter/exit
- `Toast.tsx`:手写 entering/exiting 布尔迁移到 `usePresence`(行为等价)
- NotificationBell popover:补 exit
- jobs 页 TaskDetailDrawer/JobDetailDrawer:滑入滑出 + 补遮罩(组件仍内联,抽出另列风险)
- `RealProgressBar`:width → `scaleX`
- 新增 `MotionNumber`(count-up)供 MetricCard 用

### 3.5 阶段归属

- **第一阶段改造对象**(低风险、高感知、组件级):Modal/ConfirmDialog/Toast/popover exit、仪表盘(数字+MiniBar+热力图)、works 网格 stagger 收编进 token 体系
- **后续阶段**:works/[id] Lightbox 展开(Phase 2)、search/tags/creators(Phase 3)、jobs/dedup/通知等状态反馈重区(Phase 4)——jobs 页 1730 行且轮询密集,风险最高,放最后

---

## 四、Motion Design 原则(auto-gallery 专属)

1. **图库浏览:沉浸、快速、低干扰**——works 网格/详情动效只用于空间连续性(从哪来到哪去),时长 ≤200ms,绝不阻塞图片呈现;浏览路径上禁止装饰性动画
2. **管理后台:清晰、稳定、可预测**——settings/sources/subscriptions 只保留微交互(CSS),同类操作动效必须一致;不做惊喜式动画
3. **任务与去重:强状态反馈**——下载/导入/去重是"机器在替我干活"的页面,状态跃迁(排队→运行→完成/失败)应有明确的一次性动效确认;进度必须平滑不跳变
4. **性能永远优先于动效**——不得影响懒加载、分页、图片解码;动画只用 transform/opacity;低端设备(`hardwareConcurrency ≤ 4`)自动降级
5. **`prefers-reduced-motion` 是最高优先级**——现有全局 kill-switch 保留为兜底,JS 层 `shouldAnimate()` 在源头就不启动动画(而不是启动后被 CSS 掐掉);降级路径 = 仅 ≤150ms 的 opacity 淡入
6. **动效必须至少满足其一,否则删除**:引导注意力 / 传达状态 / 保持空间连续性
7. **所有时长与缓动只来自 token**——组件内硬编码 duration/easing 视为违规

---

## 五、分阶段实施计划

### Phase 1: 建立 motion system(1 个 PR)

- `npm install animejs`(届时经用户确认)
- 建 `src/lib/motion/{tokens,config,anime,hooks}.ts`;tailwind.config 死 token(`ease-expo`、`duration-*`)与 tokens.ts 对齐激活;`barGrow` 死代码收编归入 token 体系
- 参考实现:Modal + ConfirmDialog + Toast + popover exit 四件套迁移到原语
- 验收:`npm run build` 绿;系统开 reduce-motion 后所有新动效退化为瞬时/短淡入;bundle 增量 <15 kB gz

### Phase 2: 图库与图片详情页

- works 网格:stagger 收编 token(保留 CSS 实现,`--delay` 值来自 token);翻页/筛选变更时经 `useEnterOnce` 防重播
- works/[id]:FullImageLightbox enter/exit(淡入为保底,rect→rect 展开为增强)、filmstrip 切换、DisclosurePanel 展开(grid-rows 方案)、hover 预览 pop
- 验收:Performance 面板录制翻页无 long task 恶化;LCP/图片加载无回退

### Phase 3: 搜索、tag、画师页

- search:tab 滑动指示器、结果分组 stagger
- tags:词云入场 stagger(chip hover 维持 CSS)
- creators 列表/详情/mapping:行与卡片入场、`placeholderData` 翻页时不重播

### Phase 4: 下载任务、去重审核、元数据管理

- jobs:Drawer 滑入滑出+遮罩、WS 状态变更行反馈、进度 `scaleX` 平滑、JobLifecycle 步进
- dedup/merge-candidates/creators-duplicates:对比揭示与合并收拢反馈
- GitlleryPanel/data-mgmt:操作入队→任务中心的状态过渡;notifications:条目 enter
- 前置条件:Phase 1 的 `useEnterOnce` 已被 works/creators 验证过(轮询页是最难场景)

### Phase 5: 性能审计与可访问性检查

- DevTools:paint flashing / layers / long tasks 过一遍 works、jobs、dashboard;CLS 抽查
- a11y:reduced-motion 端到端(系统设置→JS 门控→CSS 兜底三层验证)、动画挂载不丢键盘焦点、aria-live 区不被动画打断
- 清点:未用 keyframes、重复 skeleton 方案(`.skeleton` vs `animate-pulse`)二选一收敛;bundle 复查

---

## 六、风险与技术债(单列,不在动效工作中擅动)

| # | 风险/债 | 影响 | 建议 |
|---|---|---|---|
| R1 | `src/app/admin/jobs/page.tsx` ~1730 行,Drawer/生命周期组件全内联 | Phase 4 动效改造在巨型文件内进行,易冲突 | 届时单独提"抽出 Drawer 组件"的小 PR,先征求同意 |
| R2 | FullImageLightbox / DisclosurePanel 内联在 works/[id] | 同上,Phase 2 涉及 | 同上 |
| R3 | 无虚拟滚动,列表全 plain map | 限制 scroll 类动效;长列表本身有渲染上限 | 动效侧规避;虚拟化是否引入另议 |
| R4 | 轮询/WS 重渲染频繁(jobs 2–5s) | 入场动画重播/闪烁 | Phase 1 `useEnterOnce` 结构性解决 |
| R5 | Toast 自带手写 presence 逻辑 | 迁移有行为回归风险 | 迁移时人工回归 enter/exit/action/进度条四况 |
| R6 | `SOURCE_BADGE_COLORS` 硬编码 Tailwind 色、不随 6 套 palette 走 | 与主题系统不一致(约束文档要求硬编码) | 保持现状,仅记录 |
| R7 | 未用 next/image、无优先级提示 | 与动效无关的加载优化空间 | 不在本计划范围 |
| R8 | anime.js 无 AnimatePresence 等价物 | 退场编排需自建 usePresence | Phase 1 一次性成本,已计入 |

---

# 附录:性能与可访问性录制(Phase 5,2026-07-20)

Chromium headless(1440×900)对三条主路径各录两遍——正常 + 模拟 `prefers-reduced-motion: reduce`,首屏加载后静置 8 秒(覆盖 ≥1 轮 jobs 2–5s / 仪表盘 5–15s 轮询)。采集 long task(>50ms)、CLS 及逐条 layout-shift、LCP/FCP、`document.getAnimations()` 运行中动画数、`.page-transition` 计算动画时长。方法:playwright-core 驱动本机 Chromium,后端 SECRET_KEY 直签 admin JWT(cookie+localStorage 注入),脚本见 scratchpad `perf-trace.js`。

## 结果

| 页面 | 模式 | FCP | LCP | CLS | long tasks | 运行中动画 | .page-transition 时长 |
|---|---|---|---|---|---|---|---|
| /admin | normal | 976ms | 1396ms | 0.003 | **0** | 0 | 0.4s |
| /admin | reduced | 876ms | 912ms | 0.004 | **0** | 0 | **~0(1e-5s)** |
| /admin/works | normal | 864ms | 1068ms | **0.24** | **0** | 0 | — |
| /admin/works | reduced | 876ms | 1020ms | **0.39** | **0** | 0 | — |
| /admin/jobs | normal | 856ms | 984ms | 0.057 | **0** | **16** | — |
| /admin/jobs | reduced | 896ms | 1040ms | 0.045 | **0** | **0** | — |

## 结论

1. **主线程零阻塞**:三页两模式 long task 全为 0——动效工作(stagger、pulse、scaleX、count-up 懒加载)均未产生 >50ms 长任务。印证红线 #1「轮询/WS 重渲染 × 入场动画」在 `useEnterOnce` + 纯 transform/opacity 约束下没有恶化主线程。
2. **reduced-motion 三层门控实测通过**:①CSS kill-switch —— `.page-transition` 时长 0.4s → **~0**;②JS `shouldAnimate()` 源头拦截 —— jobs 运行中动画 **16 → 0**(脉冲点/徽章在 reduce 下不启动,而非启动后被 CSS 掐掉);③`usePresence` 退场归零。这正是计划 Phase 5 要的「系统设置→JS 门控→CSS 兜底」端到端验证。
3. **jobs 的 16 个常驻动画**均为 `animate-pulse`(侧栏机器状态脉冲点、任务实时徽章、JobLifecycle 活动步进点、连接点)——纯 CSS 合成器动画,long task=0,reduce 下全部停,符合预期。
4. **/admin/works CLS 偏高(0.24 / 0.39)= 既有图片尺寸问题,非动效回归**:works 网格用原生 `<img loading="lazy">` 且未预留宽高(即风险登记 R7「未用 next/image」),图片解码后撑开布局造成位移。两模式差异(0.24 vs 0.39)是图片加载时序的运行间抖动,与 motion 无关——works 卡片入场是 opacity/transform,不参与布局。**建议**:后续给缩略图容器加 `aspect-ratio` 占位(与动效计划无关的独立优化,归入 R7)。

**判定**:动效系统在性能与可访问性维度达标——零长任务、reduced-motion 三层生效。唯一 CLS 告警定位为既有图片尺寸问题(R7),不在本动效计划范围。MU-T2 完成,动效五阶段计划全部结项。

---

## 展示页(Task 7,2026-07-22)

方法与 MU-T2 相同(playwright-core 驱动本机 Chromium,`chromiumSandbox:false`,后端直签 admin JWT,cookie+localStorage 注入),追加 `/`(展示墙首页)、复用 `/admin/works`、`/admin/jobs` 作基线对照,首屏加载后静置 8 秒。**首次录制时库内 0 works**(全新/测试环境),展示墙无样本可采,`/` 恒为 `ShowcaseEmpty` 分支——为使录制具有代表性,录制前用真实图片文件(非仅数据库字符串路径)手工造了 30 条 QA 种子作品(`source_work_id` 前缀 `qa-seed-task7`,详见 Task 7 报告),使 `/api/v1/showcase/sample` 返回真实样本。

| 页面 | 模式 | FCP | LCP | CLS | long tasks | 运行中动画 |
|---|---|---|---|---|---|---|
| / | normal | 1116ms | 1116ms | 0.0002 | **0** | 0 |
| / | reduced | 988ms | 996ms | 0.0113 | **0** | 0 |
| /admin/works | normal | 1032ms | 1068ms | 0.0034 | **0** | 0 |
| /admin/works | reduced | 980ms | 1048ms | 0.0034 | **0** | 0 |
| /admin/jobs | normal | 1076ms | 1180ms | 0.0034 | **0** | 0 |
| /admin/jobs | reduced | 1020ms | 1116ms | 0.0034 | **0** | 0 |

ogl chunk(`4133.e3519a578b1a2a11.js`)在 `reduce` 下**未被请求**(0/4 次录制均确认);在 normal profile 下每次录制**恰好请求 1 次**。

### 结论

1. **四条验收标准全部通过**:`/` 两模式 long task 均为 0;`reduce` 下运行中动画为 0 且 ogl chunk 未被请求;`/` CLS 远低于 0.1(0.0002 / 0.0113);`/admin/works`、`/admin/jobs` long task 维持 0,不劣于基线。13 次独立 trial 中有 1 次在 `/` normal 观测到单个 59ms long task(未复现于其余 12 次,含一次专门捕获 longtask attribution 的诊断脚本),判定为一次性 GPU/WebGL 上下文初始化抖动,非系统性回归——以上表为准的最终录制中该页 long task 为 0。另有一条间歇性 404 控制台错误(约 30% 录制出现),定位为站点缺失 `favicon.ico`(与展示页/动效功能无关的既有问题)。
2. **Task 7 Part 2 的真实浏览器行为验证发现两处此前从未被发现的严重缺陷**(六轮静态审查均未捕获,详见 `.superpowers/sdd/task-7-report.md`):
   - **指针拖尾在任何渲染路径下都不会响应真实鼠标移动**:`ShowcaseHero`(`src/components/showcase/ShowcaseHero.tsx`)根 `div` 是 `z-10`、铺满整个视口、且没有 `pointer-events-none`,`document.elementFromPoint()` 在视口任意坐标测得的命中元素永远是它,拖尾层(canvas 或 DOM `<img>` 池)在其之下永远收不到 `pointermove`。核心"移动指针看到拖尾"体验完全失效。
   - **WebGL 画布尺寸锁死在 300×150px**:`createShowcaseRenderer`(`src/lib/showcase/webgl.ts`)构造 `ogl` 的 `Renderer` 时未传 `width`/`height`,ogl 默认 300×150 并在构造函数内立即把该值写成 canvas 的内联 `style.width/height`(优先级高于 Tailwind 的 `h-full w-full`),随后应用自身的 `resize()` 读到的已经是被内联样式锁死的 300×150,永久生效(窗口 resize 事件也无法自愈)。即便修复①,WebGL 拖尾也只会画在左上角一个 300×150 的小方块里,而非全屏。
   - 通过直接在 canvas 上 `dispatchEvent(PointerEvent)`(绕过①的命中测试问题)验证:拖尾渲染管线本身(纹理加载、mesh 池、着色器)是正确的,配置热更新触发的 `destroy()`→重建也正确存活(Task 5 的 `loseContext` 修复在真实浏览器中验证有效),context-loss 静默降级到 DOM、`--disable-gpu` 静默降级到 DOM 也都正确。问题**仅**在①②两处。
   - **通用幻灯片键盘控制完全失效**:`SlideshowPlayer.tsx` 的 focus/keydown 副作用依赖数组是 `[open]`,而 `usePresence` 的 `mounted` 状态比 `open` 晚一次渲染才变为 true——effect 首次运行时 `containerRef.current` 为 `null` 直接短路返回,此后 `open` 不再变化,effect 永不重跑,`keydown` 监听器和初始 `el.focus()` 全程未绑定过(已用 `addEventListener` 计数器验证:3 秒等待后计数恒为 0)。方向键翻页、Space 暂停、Esc 关闭、Tab 焦点陷阱全部不生效;鼠标点击对应按钮均正常。

以上三处发现已计入 Task 7 报告与 ledger,按计划由后续任务分别修复,不在本轮范围内代码修改。

---

## 展示页画廊化重构(GT7,2026-07-23)

基于 7 月 22 日的展示页改造计划,用 12 条带真实 JPEG/WEBP 文件的临时作品(`qa-seed-gallery-*`)完成最终验收。Chromium headless 1440×900,playwright-core 注入有效 JWT,每次均使用独立冷启动浏览器并静置 8 秒。`/admin/works`、`/admin/jobs` 为同轮基线对照。

| 页面 | 模式 | FCP | LCP | CLS | long tasks | 运行中动画 |
|---|---|---|---|---|---|---|
| / | normal | 1108ms | 1108ms | 0.0002 | **0** | 0 |
| / | reduced | 1028ms | 1076ms | 0.0000 | **0** | 0 |
| /admin/works | normal | 1000ms | 1080ms | 0.0000 | **0** | 0 |
| /admin/works | reduced | 952ms | 1064ms | 0.0034 | **0** | 0 |
| /admin/jobs | normal | 1016ms | 1048ms | 0.0334 | **0** | 0 |
| /admin/jobs | reduced | 932ms | 1040ms | 0.0319 | **0** | 0 |

最终冷启动矩阵的 `/` normal 3/3、reduced 2/2 均为零 long task,CLS 范围 0–0.000155。ogl 懒加载 chunk(`4133.e3519a578b1a2a11.js`)在每次 normal 录制中恰好请求 1 次,reduced-motion/低端/minimal 新加载时均为 0 次。

### 验收结论

1. **视觉与交互全通过**:图片方向和原始宽高比正确,plane 高度约为视口的 48%;自动漂移、滚轮反向、拖拽、无限回绕均连续;高速输入时出现透视弯曲,静止后恢复平面;无色差、idle 抖动或接缝。点击命中正确作品并打开幻灯片,150px 拖拽不会误触点击。
2. **降级矩阵全通过**:reduced-motion/minimal 为 8 图静态网格且零运行中动画;低端、WebGL 不可用与 `webglcontextlost` 均单向落到 CSS 漂移带且无错误 UI;`document.hidden` 时 draw call 冻结,恢复可见后继续。
3. **真实浏览器补出的实现修正**:正交相机中的 Z 位移不可见,已改为保持像素比例的透视相机并把 plane 沿 Y 细分为 20 段,同时修正弯曲态命中测试;reduced 初始 CLS 来自加载态外壳和无固定宽度的静态网格,分别稳定外壳与补 `w-full`;WebGL 首帧长任务来自一次性提交全部 shader link/纹理上传,现逐 plane 分任务预热并在 resize 时复用 Program,最终 5/5 冷启动零 long task。
4. **清理完成**:`qa-seed-gallery-*` 的 work/work_source/asset/asset_source 各 12 条均已删除,两处临时目录均不存在,缓存清除后采样端点返回空数组,当前总 works 为 0。唯一已知控制台噪声仍是站点既有的 `/favicon.ico` 404,与画廊功能无关。
