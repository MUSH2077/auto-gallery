# 展示页画廊化重构 设计文档
# Showcase Gallery Redesign — Design Spec

> 日期: 2026-07-22 · 分支: `frontend-motion`(实施时切新分支)
> 前身: `2026-07-20-showcase-homepage-design.md`(原始展示页,鼠标跟随拖尾)。本 spec 把展示页的视觉主体从"鼠标跟随拖尾"重构为 **makemepulse 式水平自动滚动透视画廊**,复用原方案的全部周边基础设施。
> 经 brainstorming 逐题确认的决策(四项全选 A):运动=水平自动滚动+滚轮/拖拽加速反向、速度驱动弯曲、无限循环;形态=单行+速度驱动圆柱弯曲+原图比例 cover;排版=画廊作整屏横带、Hero 居中浮层;交互=点击平面开全屏幻灯片。

---

## 一、动机(为什么重构)

原展示页把"随鼠标滑动展示图片"实现为**鼠标跟随拖尾**。真实浏览器运行后暴露两层问题:

**A. 当前拖尾实现的四个功能级缺陷**(定位到 `admin-web/src/lib/showcase/webgl.ts`):
1. **上下颠倒** —— `new Texture(gl, {flipY: true})` 对 `ImageBitmap` 源无效(WebGL 的 `UNPACK_FLIP_Y_WEBGL` 按规范对 ImageBitmap 不生效),图像倒置。
2. **太小且非原图比例** —— `mesh.scale.set(96*scale, 96*scale, 1)` 死钉 96px 正方形,无视 API 已返回的 width/height,非方图被挤扁。
3. **虚影** —— 片元 shader 的 RGB 色差分离 + 18 个半透明平面在光标处 `depthTest:false` 堆叠。
4. **波动** —— 常驻正弦 UV warp `sin/cos(...)*0.012` 由 uTime 驱动,停下也在抖。

**B. 概念错配** —— 经调研,makemepulse 主页并非鼠标拖尾,而是用其自研 NanoGL 做的 **3D 曲面无限自动滚动画廊**(大幅图片保原图比例、速度驱动弯曲、无色差无 idle 抖动、大字排版编织其间)。公开可复刻的同类技术是 Codrops 用 **OGL(本项目正用的同一库)** 做的无限自动滚动画廊。修好拖尾的四个 bug 只会得到一个*正确的鼠标拖尾*,仍非 makemepulse。故重构而非修补。

**目标**:把展示页主体换成大幅、原图比例、水平自动滚动、速度驱动弯曲的作品画廊,点击即看图,一次性消除上述四缺陷。

**非目标**(本 spec 明确不做):
- 不改后端取样端点的取样策略(随机窗口不变)
- 不改幻灯片播放器(T6)本身,只新增"从画廊点击进入"的入口
- 不改仪表盘、不改六套配色/明暗、不改 GitHub 外壳
- 不引入 three.js 或 NanoGL —— 继续用现有的 ogl

---

## 二、复用清单(原样不动)

| 资产 | 说明 |
|---|---|
| 后端 `GET /api/v1/showcase/sample` | 已返回 `width`/`height`/`thumb_url`/`preview_url`,画廊做原图比例 cover 正好需要 |
| ogl 动态 import + 单懒 chunk 隔离 | 继续只在 `webgl.ts` 内 `import("ogl")`,reduced-motion/低端/minimal 下不加载的性质保持 |
| 四条降级契约的**骨架** | `ShowcaseCanvas` 的 `hardwareOk`(冻结)/`useWebGL`(实时含 `!minimal`)/`fellBack`(单向)判定结构保留,仅重定义"落到什么"(见第六节) |
| 偏好同步管道 | `preferences.showcase` + `preferencesSync` 整体替换 + sanitizer + provider 全部复用 |
| 全屏幻灯片(T6) | `SlideshowPlayer` / `useSlideshow` 原样复用,新增画廊点击入口 |
| i18n 框架、middleware 鉴权、landing 路由 | 不变 |

## 三、替换清单

| 资产 | 变化 |
|---|---|
| `webgl.ts` 渲染与着色器 | 重写:拖尾 render → 画廊 render;新顶点/片元 shader(见第四节) |
| `ShowcaseTrailDOM.tsx` | 替换为 `ShowcaseGalleryDOM.tsx`(降级用 CSS 横向漂移带)+ 静态大图网格 |
| `trail.ts`(`createTrail` 拖尾状态机) | 弃用;画廊的滚动/回绕状态由渲染器自持 |
| `trailTiming.ts` 的 `frameIndependentAlpha` | **保留**,用于 lerp 滚动位置(dt 无关平滑),必要时改名为通用 smoothing 工具 |
| 设置页"动效"组 | 拖尾字段 → 画廊字段(见第七节) |

---

## 四、画廊渲染器(一次解决四缺陷)

**文件**:重写 `admin-web/src/lib/showcase/webgl.ts`。接口保留 `createShowcaseRenderer(canvas, opts)` / `ShowcaseRenderer`,但 `render()` 签名从 `render(items, pointer)` 改为 `render(scrollState)`(渲染器不再消费 `TrailItem[]`,改为持有自己的滚动位置与图片列表)。

### 4.1 布局与尺寸(修"太小/比例错")

- 单行水平排布,每个平面按视口分数取**大幅尺寸**(如高度 ≈ 视口高的 42–58%,由 `planeDensity` 决定),宽度按该图 `width/height` 原始比例推导。
- 片元 shader 做 `object-fit: cover` 比例矫正(Codrops 技法):
  ```glsl
  vec2 ratio = vec2(
    min((uPlaneSizes.x / uPlaneSizes.y) / (uImageSizes.x / uImageSizes.y), 1.0),
    min((uPlaneSizes.y / uPlaneSizes.x) / (uImageSizes.y / uImageSizes.x), 1.0)
  );
  vec2 uv = vec2(vUv.x*ratio.x + (1.0-ratio.x)*0.5, vUv.y*ratio.y + (1.0-ratio.y)*0.5);
  ```
  `uImageSizes` 来自采样项的 `width`/`height`;缺失(null)时回退为平面比例(即不裁切)。

### 4.2 朝向(修"上下颠倒")

- 纹理改用 `createImageBitmap(blob, { imageOrientation: "flipY" })` 生成正确朝向的位图,配 `new Texture(gl, { flipY: false })`。**不再**依赖 ogl 的 `flipY:true`(对 ImageBitmap 无效)。
- 验收必须真实浏览器确认非倒置(见第十节)。

### 4.3 弯曲(修"虚影/波动";makemepulse 观感)

- **删除** RGB 色差分离与常驻正弦 UV warp。
- 顶点 shader 只保留**速度驱动的 Z 轴圆柱弯曲**:
  ```glsl
  newPosition.z += sin(newPosition.y / uViewportSizes.y * PI + PI/2.0) * -uStrength;
  ```
  `uStrength` ∝ 当前滚动速度(见 5.2),静止时 `uStrength → 0`,平面摊平。
- 片元 shader 只做 cover 采样 + `uOpacity`(边缘淡入淡出,见 5.3),无色差、无 idle 抖动。

---

## 五、交互

### 5.1 自动滚动 + 用户干预(决策 A)

- `scroll.target` 每帧自增 `autoScrollSpeed`(恒定慢速漂移);`scroll.current` 用 `frameIndependentAlpha` lerp 追 `target`(dt 无关,60/144Hz 观感一致)。
- **滚轮 / 拖拽**把增量叠加到 `scroll.target` → 加速或反向;松手后 `target` 恢复只受 `autoScrollSpeed` 驱动,平滑回落到自动漂移。
- 平面世界 X = `basePosition - scroll.current`。

### 5.2 速度 → 弯曲

- `uStrength = clamp((scroll.current - scroll.last) / viewportW * curveStrength, ...)`,即每帧滚动位移(含钳制,防后台标签恢复的巨跳),乘 `curveStrength`。静止 → 0。

### 5.3 无限循环

- 平面沿 X 首尾相接排成一条总长 `totalWidth` 的带;某平面移出视口左/右边界即整体位移 `±totalWidth` 回绕(Codrops 的 `extra += totalWidth` 技法),形成两个方向的无缝循环。
- 循环接缝处平面 `uOpacity` 在进入/离开视口边缘时短暂淡入淡出,避免硬弹入。

### 5.4 点击开幻灯片(决策 A)

- **命中映射**:指针坐标 → 判定命中哪个平面(平面在正交相机下位置已知,做屏幕坐标区间命中即可,无需完整 raycast)。
- **点击/拖拽区分**:`pointerdown` 到 `pointerup` 间累计位移 < 阈值(~6px)且时长 < ~400ms → **点击**,打开 T6 幻灯片,`items` = 当前整批采样映射的 `SlideItem[]`,`startIndex` = 命中平面对应的作品;否则视作拖拽滚动,不触发点击。
- **后端小改**:取样响应 `ShowcaseItem` 新增显式 `asset_id: str` 字段(当前只隐含在 `thumb_url` 里),供 `SlideItem.assetId` 直接使用,免去前端解析 URL。

---

## 六、降级映射(契约骨架不变,落点重定义)

| 条件 | 行为 |
|---|---|
| `prefers-reduced-motion: reduce` 或 `config.minimal` | **静态大图网格**:无自动滚动、无弯曲、无 rAF、不加载 ogl。顺带把原"小方块"静态网格换成**原图比例大图**网格 |
| 低端 `hardwareConcurrency ≤ 4` 或 WebGL 创建失败/`webglcontextlost` | **`ShowcaseGalleryDOM`**:纯 CSS `transform: translateX` 横向漂移带(GPU 合成,transform-only),原图比例大图,无弯曲无 WebGL。保留"活着"的漂移感 |
| `document.hidden` | 暂停自动滚动 rAF(WebGL 与 DOM 两路都暂停) |

- WebGL↔DOM 决策仍在挂载时定一次;`fellBack`(真实上下文丢失)仍单向 WebGL→DOM;`minimal` 仍实时门控(翻真 → 静态网格,翻假 → 恢复,不刷新)。
- ogl 在 reduced-motion/低端/minimal 下**绝不加载**的网络断言不变(T7 验收项)。

---

## 七、配置 / 设置迁移

四组配置里**内容源筛选、幻灯片参数、首页行为、`minimal`** 全部不变。仅"动效"组的字段迁移:

| 移除(拖尾专用) | 新增(画廊) | 指示性 clamp(实施可微调) | 语义 |
|---|---|---|---|
| `trailMax` | `planeHeightVh` | 约 30–70(step 5) | 平面高度占视口高的百分比,决定同屏可见数(越大越少) |
| `spawnIntervalMs` | `autoScrollSpeed` | 约 0.2–3.0 px/帧(step 0.1) | 自动漂移快慢 |
| `followDamping` | `curveStrength` | 约 0–1(step 0.05) | 速度→弯曲强度,0=永不弯曲 |
| `parallaxStrength` | —(去掉,画廊无独立视差) | | |

> 指示性范围仅界定量级,实施时可在同量级内微调;唯一硬约束:控件 min/max/step 与 sanitizer clamp 必须逐一对齐且 `(max-min)/step` 为整数(沿用原展示页的滑块-sanitizer 对齐原则)。

- `ShowcaseConfig` 类型、`DEFAULT_SHOWCASE_CONFIG`、`sanitizeShowcaseConfig` 的 clamp、设置页"动效"组控件、i18n 键同步更新。控件 min/max/step 必须与 sanitizer clamp 逐一对齐(沿用原则:`(max-min)/step` 为整数)。
- **向后兼容**:旧偏好里的 `trailMax` 等废弃键被 sanitizer 自动丢弃、新键填默认,无痛;`SHOWCASE_STORAGE_KEY` 不变。

---

## 八、数据流

```
展示页挂载
  → useQuery(GET /showcase/sample?count&filters, 长 staleTime) → items(含 width/height/asset_id/preview_url)
  → ShowcaseCanvas 判定 useWebGL
     ├ WebGL: createShowcaseRenderer → createImageBitmap(flipY) 解码 → 纹理 → 按原图比例排成无限带
     │         rAF: scroll.current lerp→target(+滚轮/拖拽) → uStrength ∝ 速度 → render
     └ 降级: ShowcaseGalleryDOM(CSS translateX 漂移) 或 静态大图网格
  → 点击命中平面(非拖拽) → useSlideshow.open(items→SlideItem[], startIndex)
```

## 九、错误处理

| 场景 | 行为 |
|---|---|
| WebGL 创建失败 / 上下文丢失 | 静默降级 `ShowcaseGalleryDOM`,无错误 UI,单向 |
| 空库(无作品) | `ShowcaseEmpty`(区分真空库 vs 筛选为空,沿用现状) |
| 签名 URL 过期(图片 401) | 整批重取 sample(沿用 `MAX_AUTO_REFETCH_STREAK` 有界重取) |
| 单张纹理加载失败 | 跳过该平面(该槽 `uOpacity=0`),不阻塞其余 |
| sample 接口报错 | `ErrorState` + 重试 |

## 十、测试与验收(硬性)

鉴于原展示页六轮纯静态审查漏掉三个功能级 bug、唯真实浏览器暴露,本重构**强制真实浏览器验收**(复用 T7 的 playwright-core + Chromium harness):

- **视觉正确性**:①朝向非倒置 ②原图比例不挤扁 ③大幅尺寸 ④自动滚动顺滑 ⑤仅快滚时弯曲、静止摊平 ⑥无色差、无 idle 抖动 —— 截图佐证
- **交互**:点击平面开幻灯片且起始作品正确;拖拽滚动不误触发点击;滚轮加速/反向;无限循环无接缝硬弹
- **降级四路**:reduced-motion/minimal → 静态大图网格且不请求 ogl chunk;低端/WebGL 不可用 → CSS 漂移带;强制丢失上下文 → 单向降级;document.hidden → 暂停
- **性能**:`/` long task 仍 0、CLS < 0.1(全屏画廊不得回归);ogl 仍单一懒 chunk
- **后端**:`asset_id` 字段的 pytest;`tsc --noEmit` + `npm run build` 绿

## 十一、风险与取舍

| # | 风险 | 缓解 |
|---|---|---|
| G1 | 命中映射在弯曲+滚动的平面上不准 | 用正交相机下的屏幕坐标区间命中(平面 X 已知),弯曲只影响 Z 不影响屏幕 X 区间;阈值区分点击/拖拽 |
| G2 | 无限循环接缝可见硬弹 | 边缘 `uOpacity` 淡入淡出 + 回绕在视口外发生 |
| G3 | 任意库图比例悬殊(超宽/超高)排布难看 | cover 裁切保证不变形;平面宽度按原比例但设上下限,极端比例被夹 |
| G4 | 纯静态审查再次漏掉运行期缺陷 | 第十节强制真实浏览器验收,这是不可省环节 |
| G5 | 自动滚动与 reduced-motion 无障碍冲突 | reduced-motion 明确 → 静态网格(无任何自动运动),最高优先级 |
| G6 | ogl chunk 因重写意外漏进共享 bundle | 构建后按 `OES_texture_float`/`attributeOrder` 现查 chunk,断言其余路由零引用 |

## 十二、实施顺序(供 writing-plans 参照)

```
1 后端:ShowcaseItem 加 asset_id + pytest
2 画廊渲染器 webgl.ts 重写:原图比例 cover + 正确朝向 + 速度驱动弯曲 + 无限带(先不接点击)
3 ShowcaseCanvas:自动滚动 + 滚轮/拖拽 + lerp + 降级落点重定义(WebGL 主路径先跑通)
4 降级两路:ShowcaseGalleryDOM(CSS 漂移带) + 静态大图网格;四条契约实机确认
5 点击开幻灯片:命中映射 + 点击/拖拽区分 + SlideItem 映射
6 配置迁移:动效组字段 trail→gallery + sanitizer + 设置页 + i18n
7 真实浏览器验收(第十节全清单) + 文档/记账
```

先把 WebGL 主路径(2–3)跑通并实机看到正确的画廊,再补降级与点击,保证任何中断点都有一个可见、正确的画廊。
