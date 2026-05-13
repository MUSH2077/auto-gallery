# 风险登记册

## 高风险

### 1. gallery-dl 版本 / 输出格式稳定性

**风险**：gallery-dl 频繁更新。锁定版本会导致源站点变化时提取器失效。不锁定版本则输出格式变化可能使元数据解析器静默失败。

**影响**：下载中断、导入失败、元数据缺失

**缓解措施**：
- 在 `requirements.txt` 中锁定已知可用的 gallery-dl 版本
- 添加集成冒烟测试：用 gallery-dl 下载已知 URL，验证 JSON 输出结构与解析器期望一致
- 升级 gallery-dl 前先运行冒烟测试套件
- 版本化 gallery-dl 配置 schema

**决策**：锁定版本 + 冒烟测试。禁止自动升级。

---

### 2. 跨来源创作者身份映射

**风险**：跨平台关联账号（"Pixiv 用户 123456" ↔ "@artist_handle on X"）是根本性难题。个人资料 URL 嵌入在自由文本字段中。许多创作者在不同平台使用完全不同的名字。自动化准确率有限。

**影响**：管理员手动映射负担重。创作者记录碎片化。

**缓解措施**：
- 接受自动化映射最高只能达到约 60% 准确率
- 以手动映射为主要路径
- Danbooru 参考充其量是建议引擎，而非权威来源
- 添加 `creator_link.confidence` 字段（0.0–1.0）用于建议链接评分
- 建议链接的管理审核队列
- 绝不让自动映射成为 Phase 1-6 的前置依赖

**决策**：手动优先 + 建议引擎。架构可容纳此方案。

---

### 3. X/Twitter API 可行性

**风险**：X 已大幅限制 API 访问。gallery-dl 的 X 提取器可能永久失效或需要付费 API。

**影响**：X/Twitter 可能无法作为自动化下载的可行来源。

**缓解措施**：
- 保持 X 为占位 provider，`can_download = False`
- Phase 1-8 聚焦于 Pixiv + 本地导入 + 手动上传
- 若日后 gallery-dl X 支持可恢复则激活
- 若不可行，X 导入可能需要替代方案（浏览器扩展、RSS、手动）
- Provider 抽象已兼容此情况——无需架构变更

**决策**：X 明确为"暂无时间表"。Pixiv 管线稳定后重新评估。

---

### 4. DOWNLOAD_ROOT → LIBRARY_ROOT 原子性

**风险**：gallery-dl 写入 `DOWNLOAD_ROOT/<job_id>/`。导入过程将文件移动到 `LIBRARY_ROOT/`。若 worker 在执行过程中崩溃：
- DOWNLOAD_ROOT 中有文件但无导入任务（孤儿下载）
- DB 记录指向移动失败的文件（损坏引用）

**影响**：存储浪费、媒体 URL 损坏、数据库不一致

**缓解措施**：
- 任务状态机：`pending → downloading → downloaded → importing → complete`
- Worker 启动时扫描 DOWNLOAD_ROOT 中的孤儿目录，与 `download_job` 表对账
- 导入必须完全幂等：用相同文件重新运行不产生重复
- 利用 `(source, source_asset_id)` 唯一性约束防止重复 asset 记录
- 同一文件系统内（同一 Docker 卷）的文件移动使用 `os.rename` 保证原子性

**决策**：状态机 + 启动对账 + 幂等导入。

---

## 中等风险

### 5. 队列后端：已选定 RQ

**风险**：RQ vs Celery 此前未定。RQ 更简单但故障恢复功能较少。

**已做出的决策**：v1 使用 RQ。理由：复用已有 Redis、配置更简单、任务模型已抽象队列。迁移 RQ → Celery 可行，因为 `download_job`/`import_job` 表才是数据源头。

---

### 6. Meilisearch 索引一致性

**风险**：向 PostgreSQL 和 Meilisearch 双写可能漂移。定期重建索引意味着搜索结果可能滞后。

**缓解措施**：
- v1：管理员触发的全量重建索引（手动但一致）
- v2：应用层双写 + 定期对账
- 接受 v1 中搜索结果可能滞后数分钟

---

### 7. gallery-dl 认证/cookie 过期

**风险**：Pixiv 会话 cookie 过期。认证失败导致下载静默失败。

**缓解措施**：
- 在 `subscription_source` 上添加 `last_successful_auth` 时间戳
- 健康端点检测近期发生认证失败的来源
- 管理后台 cookie/配置状态页面显示每个来源的认证健康状况
- 在 gallery-dl 输出中检测 HTTP 401/403

---

### 8. Provider 抽象泄漏

**风险**：Pixiv 元数据（系列、标题结构）与 Iwara（声优、3D 标签）和 X（话题标签、线程上下文）差异显著。强制通过相同 `parse_*` 方法可能导致尴尬的最小公分母 schema。

**缓解措施**：
- `raw_metadata` JSONB 列是逃生阀——一切来源特有数据存于此
- 类型化字段仅捕获可清晰映射的内容
- admin-web 可有 provider 特有组件，按来源渲染 `raw_metadata`
- Provider `parse_*` 方法尽量提取类型化数据；不丢弃任何信息

---

### 9. 单一 worker 瓶颈

**风险**：一个 worker 一次处理一个任务。同步拥有 1000+ 作品的创作者需要数小时/数天。

**缓解措施**：
- v1 接受（个人归档，非实时服务）
- Worker 可水平扩展：`docker compose up --scale worker=3`
- 只要每个任务使用唯一的 DOWNLOAD_ROOT 子目录（`<job_id>`），扩展即安全
- 调度器可错开订阅同步时间避免惊群效应

---

### 10. NAS HDD I/O 性能

**风险**：在 NAS HDD（可能是 SMR 盘）上对大媒体文件计算 SHA-256 和 pHash 会很慢。

**缓解措施**：
- 尽可能在下载流式传输过程中计算哈希
- 大图片使用 `pyvips` 而非 Pillow（更快、更低内存）
- 缩略图在导入时生成，而非首次请求时
- 接受大型创作者的首次同步会很慢

---

## 低风险

### 11. Alembic 迁移执行
通过 `docker compose run --rm backend alembic upgrade head` 在启动前运行迁移。添加启动检查：若有待执行迁移则拒绝启动。

### 12. 文件命名 / 重新组织
gallery-dl 在下载时命名文件。导入任务可重新组织。Asset 路径在导入后存储于数据库，非导入前。若命名模板在下载和导入之间变更，无路径损坏风险。

### 13. Iwara gallery-dl 支持未知
Phase 8 前验证 gallery-dl 是否支持 Iwara。若不支持，Iwara 保持占位。无架构影响。

### 14. Admin 认证机制
Phase 6 前决定：v1 admin 路由使用简单 API key。JWT + 多用户后续再议。不影响模型层。

### 15. Danbooru 阶段排序
创作者身份（Phase 4）无需 Danbooru（Phase 5）即可工作。Danbooru 丰富身份信息，并非使其可用。排序正确。

---

## 方案变更汇总

| 变更 | 触发风险 | 缓解产物 |
|---|---|---|
| RQ 选定为队列后端 | #5 | CLAUDE.md — 技术栈 |
| `creator_link.confidence` 字段 | #2 | backend-architecture.md — 风险衍生模型字段 |
| 下载任务状态机 | #4 | gallerydl-integration.md — 下载任务状态机 |
| Worker 启动孤儿对账 | #4 | gallerydl-integration.md — Worker 启动孤儿对账 |
| `last_successful_auth` 在 subscription_source | #7 | backend-architecture.md — 风险衍生模型字段 |
| `auth_healthy` 在 subscription_source | #7 | gallerydl-integration.md — 认证健康追踪 |
| v1 管理员触发 Meilisearch 重建索引 | #6 | backend-architecture.md — Meilisearch 同步 |
| gallery-dl 输出格式冒烟测试 | #1 | backend-architecture.md — gallery-dl 冒烟测试 |
| X provider 明确为"暂无时间表" | #3 | CLAUDE.md — 风险衍生决策 |
| pyvips 优先于 Pillow | #10 | CLAUDE.md — 风险衍生决策 |
