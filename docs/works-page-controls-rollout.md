# Works page controls rollout

This runbook covers the works-page filter, sort, display, heat-ranking, and
stable-random release. The database changes are additive and intentionally
remain in place if the application is rolled back.

## Release order

1. Create the ordinary application rollback point, then deploy the schema and
   backend while the previous frontend remains active.
2. Run the idempotent heat backfill for every imported source:

   ```bash
   cd backend
   python scripts/recompute_work_heat.py
   ```

   Use repeated `--source pixiv` arguments only when repairing selected
   sources. Re-running the same inputs must report no changed works and must
   not enqueue unchanged search documents.
3. Rebuild the versioned search indexes through **Data management → Rebuild
   search index**, or the existing `scripts/rebuild_search_indexes.py`
   operational command. Wait for the atomic index swap to complete.
4. Capture a pre-release existing-sort baseline, then run the candidate at no
   less than 70,000 works:

   ```bash
   python scripts/benchmark_work_search.py \
     --require-scale --repeats 20 --target-ms 500 \
     --output /tmp/work-search-baseline.json

   python scripts/benchmark_work_search.py \
     --require-scale --repeats 20 --target-ms 500 \
     --baseline-file /tmp/work-search-baseline.json --require-baseline \
     --output /tmp/work-search-candidate.json
   ```

   Heat and random first/cursor pages must each remain at or below 500 ms p95,
   existing sort p95 must regress by no more than 10%, and pages remain capped
   at 30 works. Work-list SQL fan-out remains capped at six statements,
   including the existing search-index consistency reads. Query-plan coverage
   also rejects a full-table `random()` sort.
   Before `scripts/run-acceptance.sh performance`, copy the pre-release report
   to the active acceptance run's
   `reports/work-search-baseline.json`; the performance phase fails closed when
   that baseline is absent.
5. Enable the new frontend. Smoke-test desktop popovers, mobile sheets, all
   three layouts and card sizes, search clear, random reshuffle, NSFW blur,
   selection hiding, and recycle-bin row actions.
6. Start or retain the ordinary scheduler. Pixiv ranking refreshes use healthy
   accounts and the existing rate limits. Missing credentials, missing R18
   permission, stale snapshots, or provider failures only activate the local
   heat fallback; they do not block the works page. Every successful snapshot
   also schedules a guarded expiry check just after 48 hours. The check becomes
   a no-op when a newer snapshot exists and queues one coalesced heat rebuild
   when the latest snapshot is stale.

## Rollback

- Disable the new frontend and Pixiv ranking schedule, then restore the
  previous compatible application images together.
- Do not run an Alembic downgrade. Keep `shuffle_key`, heat fields, source
  metrics, indexes, and ranking snapshots for forward-schema compatibility.
- Keep search snapshots and queued recomputations for diagnosis. Never delete
  works or source metadata as a rollback shortcut.
- List requests never call providers per work, so disabling the ranking task is
  sufficient to stop new external ranking traffic.

## 中文发布说明

本说明适用于作品页筛选、排序、显示、热度和稳定随机功能。数据库改动全部为向前兼容的
增量改动；即使回滚应用，也应保留这些字段、索引和榜单快照。

### 发布顺序

1. 先建立常规应用回滚点，再发布数据库结构和后端，此时可继续使用旧前端。
2. 在 `backend` 目录运行 `python scripts/recompute_work_heat.py`，为所有已导入来源回填
   稳定随机键、来源指标和热度。只修复指定来源时可重复传入 `--source pixiv`。命令可安全
   重跑；相同输入不应产生变更，也不应重复投影搜索文档。
3. 在“数据管理 → 重建搜索索引”执行全量重建（或使用既有
   `scripts/rebuild_search_indexes.py` 运维命令），等待版本化索引完成原子切换。
4. 在不少于 70,000 件作品的数据上，按上方命令先记录旧排序基线，再运行候选版本。
   热度和随机的首屏、游标下一页 p95 均不得超过 500 ms；现有排序 p95 回归不得超过
   10%；每页仍限制为 30 件；包含既有搜索索引一致性读取在内，单次列表请求最多执行
   6 条 SQL。查询计划测试同时禁止全表 `random()` 排序。运行
   `scripts/run-acceptance.sh performance` 前，将旧版本报告复制到当前验收目录的
   `reports/work-search-baseline.json`；缺少基线时性能阶段会直接失败，不会静默跳过。
5. 启用新前端，检查桌面弹窗、移动端底部面板、三种布局、三档卡片、搜索清除、重新随机、
   NSFW 模糊、隐藏多选，以及回收站单项操作。
6. 启动或保留正常调度器。Pixiv 榜单同步复用健康账号和既有限流；无账号、无 R18 权限、
   榜单过期或同步失败时只会回退到本地热度，不会阻断作品页。每次成功快照还会在 48 小时
   后安排一次带保护条件的过期检查；若已有更新快照则直接跳过，仅在最新快照确已过期时
   排入一次可合并的热度重算。

### 回滚

- 停用新前端和 Pixiv 榜单计划任务，并一起恢复上一版兼容的前后端应用。
- 不执行 Alembic 降级；保留 `shuffle_key`、热度字段、来源指标、索引和榜单快照。
- 保留搜索快照与待处理重算任务以便排查，不得通过删除作品或来源元数据来回滚。
- 作品列表不会逐件访问外部平台；停用榜单任务即可停止新增榜单请求。
