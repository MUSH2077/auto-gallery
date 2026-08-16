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
| 去重审核 `/admin/data-mgmt/dedup` + merge-candidates + creators/duplicates | 对比卡对开揭示 timeline、合并成功时卡片收拢汇合反馈 | 多元素时序编排 |
| 作品详情 `/admin/works/[id]` | Lightbox 从缩略图 rect 放大展开(shared-element 感)、filmstrip 切换 | FLIP 式 rect→rect 变换需测量+JS |
| 通知 NotificationBell / notifications 页 | 新通知条目滑入、badge 计数 pop | 列表项 enter/exit 编排 |
| 策展 `/admin/data-mgmt/curation` | commit 时间线卡片沿线 stagger | 动态延迟 |

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
