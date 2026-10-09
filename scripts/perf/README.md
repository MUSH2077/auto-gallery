# 本地性能测试工具

入口为项目根目录的 `bash scripts/local-perf.sh`。工具复用实际后端函数、持久 outbox、RQ、公开接口及项目 Playwright；查询默认测热缓存，媒体可分别测时间和内存。工具结果保存在忽略目录 `data/local-perf/`，不提交报告、数据、日志、截图或凭据。

## 环境与资源

在 Linux／WSL 运行，需要启用Docker Desktop的WSL集成，以及 Docker Compose v2（支持 `!override`）、Node.js（支持内置 fetch、FormData、fs.openAsBlob）和 Python。Node 与 Playwright优先使用项目已有安装；不用下载一套新的运行环境。

```bash
bash scripts/local-perf.sh init
bash scripts/local-perf.sh build
bash scripts/local-perf.sh up
bash scripts/local-perf.sh seed --target-gib 0.1
```

`init` 只在不存在时创建 `data/local-perf/perf.env`，凭据随机生成，权限0600；已有配置不自动覆盖。`seed` 在容器内生成合成图片并通过真实上传接口登记，不能将其当作真实照片样本。需要真实平台／图片表现时复用现有真实素材。

浏览器与API测试使用 `data/local-perf/login.json` 中的本地测试管理员会话，账号需完成首次密码修改。密码、cookie和CSRF值不能写入源码或报告。原生媒体容器模式不需要登录。

新初始化环境保持资源治理 `enforce`，10个稳态服务内存上限合计4032MiB（约3.94GiB／4.23GB）：PostgreSQL384、Redis128、Meili512、backend512、下载512、导入768、operations512、发现256、scheduler256、前端192MiB。配额不是实际使用量；构建及迁移属于单独准备阶段，不和性能窗口重叠。旧perf.env中的数据库／搜索配置可能覆盖这些默认值，执行前按本次预算调整。

基础入口默认忽略旧的 `workers.compose.json`，沿用生产队列命令。仅在明确需要既有本地覆盖时设置 `AUTO_GALLERY_PERF_BASE_ONLY=0`；增加进程数本身不证明有效并行度提高。本工具提交不正式化并发或调度参数。

数据累计上限15GB；主控在14GB停止继续投放，有限任务在写入前预留空间。平台下载单次上限1GB。数据预算统计源码之外的运行数据和相关卷，同一卷只计一次；镜像与构建缓存另计。不要在主库中执行临时数据清理。

## 按任务选择范围

`benchmark --help` 只显示参数，不启动测试。

```bash
# 默认仅列表、搜索和缩略图查询；关闭详细埋点。
bash scripts/local-perf.sh benchmark --seconds 20 --repeats 3
bash scripts/local-perf.sh benchmark --routes list search --concurrency 1 4 --via frontend

# 单模块：也支持 import、upload、download。
bash scripts/local-perf.sh benchmark --scope media
bash scripts/local-perf.sh benchmark --scope upload --tasks 2 --concurrency 1 --repeats 1

# 完整范围会新增上传数据，并可能真实访问平台，按任务需要显式使用。
bash scripts/local-perf.sh benchmark --scope all
```

`all` 依次运行媒体原生函数、选定查询、Provider／身份查找、页面上传、未见作品下载、平台订阅同步和限定来源磁盘重放。定位局部问题时使用单scope或替换模块，不默认执行全部范围。平台下载使用现有测试订阅及其认证配置，prepare／cleanup记录并恢复临时设置。

查询采用闭环请求、读完响应体，每窗口5次预热，默认并发1／4／8。输入最多8路。有限模块不自动预热，只执行明确任务数；上传／下载分别记录入口响应和后续导入、媒体、索引完成。观察间隔会造成完成时间上界，不能当作精确函数执行时间。

`--profile` 启用临时Compose埋点，只用于定位。非查询scope默认会启用，正式性能对照使用 `--no-profile`。详细埋点包含SQL计数、函数及资源阶段，具有性能开销；不能将这类结果当作无扰动吞吐。主控恢复临时挂载，异常结果保留在对应运行目录。

## 媒体时间与内存分开测

```bash
# 已准备的完整测试栈：/perf-tools须已挂载，提供fixture避免重新采集。
bash scripts/local-perf.sh benchmark --scope media --no-profile \
  --fixtures /absolute/fixtures.json --asset-ids ASSET_ID \
  --media-stages native_derive_sync native_derive_existing_thumbnail \
  --media-measurement timing --resource-interval 0 --media-iterations 2 --repeats 3

# 已准备的媒体专用容器：app、/perf-tools、输入输出须已挂载。
bash scripts/local-perf.sh benchmark --scope media --native-container CONTAINER \
  --fixtures /absolute/fixtures.json --asset-ids ASSET_ID \
  --media-stages phash native_derive_sync \
  --media-measurement memory --media-sample-ms 100 --resource-interval 0
```

现有stage为 `thumbnail_and_dimensions`、`sha256`、`phash`、`ssim`、`native_derive_sync`、`native_derive_existing_thumbnail`。每个stage独立进程，`--media-iterations 2` 分别记录首次和重复调用。未显式选择ID时只抽每格式最小／最大像素样本，不能覆盖中等尺寸。

时间模式没有PSS采样线程；`--resource-interval 0` 同时关闭外层Docker／磁盘采集，仅支持media scope。内存模式每100毫秒读取PSS，另记录内核进程RSS高水位、入口导入前后及调用前后。短调用可能漏峰；未采到不等于新增内存为零，采样CPU不能当作纯算法CPU。

`native_derive_existing_thumbnail` 在另一个进程准备首卡，避免准备工作抬高被测进程的RSS高水位。正式函数计时不包括业务模块入口导入，也不包括解释器启动；原生阶段默认文件缓存已热，不能宣称冷磁盘性能。

`--media-sequence --asset-ids ID_A ID_B` 在同一解释器连续处理不同素材，用于观察缓存与对象生命周期，不代表生产RQ已改为多图任务。汇总按各资产metrics统计，不能再加一次序列顶层metrics。pHash阶段包含解码／采样，不等于数值变换；完整派生包含SHA、指纹和适用的样本生成，嵌套耗时不相加。

## 可替换模块

```bash
bash scripts/local-perf.sh benchmark --scope upload --no-profile \
  --modules "$PWD/scripts/perf/modules/upload-e2e.cjs" \
  --fixtures /absolute/fixtures.json --tasks 1 --concurrency 1 --repeats 1
```

`--modules` 替换整组内置模块。模块导出name、prepare、execute；有限操作声明 `mode: "finite"` 和tasks，可提供setup／cleanup／reserveBytes。沿用runner会话、请求、并发和存储预算，不另外建立测试主控。

| 文件 | 职责 |
|---|---|
| local-perf-benchmark.py | 参数、模块选择、环境挂载、结果与宿主采样 |
| local-perf-data.py | 通过实际上传建立有限合成样本 |
| perf/runner.cjs | 认证、预热、有限任务、请求和清理 |
| perf/bridge.cjs | 项目Playwright、backend控制进程与原生probe |
| perf/control.py | 现有fixture、导入／下载状态及身份盘点 |
| perf/probe.py | 图片／Provider原生测量 |
| perf/analyze.py | 将客户端、执行、队列和内存结果分别汇总 |
| perf/budget_check.py | 以9／10字节边界验证gallery-dl写入限制 |
| perf/instrumentation/sitecustomize.py | 仅临时挂载启用的诊断埋点 |
| perf/modules/ | 查询、媒体、导入、上传、下载和重放模块 |

`upload-e2e` 加入公开列表／缩略图／原图Range及原图对话框验证；`upload-receive` 将上传响应与异步尾部分开记录。它们只在任务需要时显式选用，不混入纯函数性能窗口。`import-replay` 测限定来源重放，不等同于全局入口分类验证。

## 结果与清理

每次运行保留config.json、requests.json、summary.json、module-summary.json、results.zh.md、client.log及适用的events.jsonl／memory.jsonl。报告生成器属于工具，生成报告本身不提交Git。

RSS、PSS、容器匿名内存与文件缓存分别解释，不相加或混用；不同时间的峰值不合计。小样本不提供函数P99。比较时使用相同素材、计时边界和资源配置，交错运行实现，排除准备／清理时间。

`stop` 只停止本地测试栈，数据保留；不自动删除原图、数据库卷或凭据。研究版扩容、入口并发及上传数据库研究驱动不在基础入口公开，待对应实现正式化后再整理提交。
