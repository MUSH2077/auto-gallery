"""Summarize measured module execution separately from client and queue time."""
from collections import defaultdict
import json
from pathlib import Path
import statistics


def quantile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, __import__("math").ceil(len(ordered) * fraction) - 1)]


def analyze(output):
    output = Path(output)
    run = json.loads((output / "requests.json").read_text())
    functions = defaultdict(list)
    jobs = []
    replays = []
    for case in run["cases"]:
        for operation in case["requests"]:
            measurements = operation.get("measurements") or {}
            if measurements.get("mode") == "sequential_media_assets":
                # The parent also exposes a flat metrics convenience view.  Use
                # the child records as the accounting source so each operation
                # is counted once and keeps its asset/sequence metadata.
                metric_sources = [
                    (metric, asset_result)
                    for asset_result in measurements.get("assets", [])
                    for metric in asset_result.get("metrics", [])
                ]
            else:
                metric_sources = [
                    (metric, measurements)
                    for metric in measurements.get("metrics", [])
                ]
            for metric, context in metric_sources:
                if metric.get("outcome") == "not_applicable":
                    continue
                copies = metric.get("operations", 1)
                row = {**metric, "wall_ms": metric["wall_s"] * 1000 / copies,
                       "format": context.get("format"), "source": context.get("source"),
                       "bytes": context.get("bytes"), "width": context.get("width"), "height": context.get("height"),
                       "concurrency": case["concurrency"], "asset_id": context.get("asset_id")}
                sample = (row.get("format") or row.get("source") or "unknown")
                if row.get("asset_id"):
                    sample += f" {row['width']}×{row['height']} {row['bytes']/1e6:.2f}MB {row['asset_id'][:8]}"
                functions[(case["route"], sample, metric["stage"] + ("/" + metric["process_state"] if metric.get("process_state") else ""))].append(row)
            if measurements.get("snapshots") or measurements.get("snapshot"):
                jobs.append({"case": case["name"], "route": case["route"], **measurements})
            if measurements.get("mode") == "public_api_source_scoped_idempotent_replay":
                replays.append(measurements)
    traces = []
    event_file = output / "events.jsonl"
    if event_file.exists():
        with event_file.open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row["event"] != "process_memory":
                    traces.append(row)
    function_summary = []
    for (module, sample, stage), rows in functions.items():
        times = [r["wall_ms"] for r in rows]
        function_summary.append({"module": module, "sample": sample, "stage": stage, "samples": len(rows),
            "p50_ms": quantile(times, .5), "p95_ms": quantile(times, .95), "p99_ms": quantile(times, .99) if len(rows) >= 100 else None,
            "max_pss_increase_mib": max((r["pss_increase_bytes"] for r in rows if r.get("pss_increase_bytes") is not None), default=0) / 2**20 if any(r.get("memory_peak_observed") for r in rows) else None,
            "max_observed_pss_mib": max((r["memory_peak_observed"]["pss_bytes"] for r in rows if r.get("memory_peak_observed")), default=0) / 2**20 if any(r.get("memory_peak_observed") for r in rows) else None,
            "max_process_peak_rss_mib": max((r.get("process_peak_rss_bytes", 0) for r in rows), default=0) / 2**20,
            "measurement": rows[0].get("measurement", "memory"), "rows": rows})
    request_spans = defaultdict(list)
    for row in traces:
        if row["event"] == "span" and row.get("case") and row["stage"] in {"http", "search.search", "search._search_meili", "search._search_works_db", "upload.persist_and_publish", "upload.spool_to_download_temp", "upload.validate", "upload.identity", "upload.move", "upload.metadata_write", "import.publish"}:
            request_spans[(row["case"], row["stage"])].append(row)
    roots = []
    for (case, stage), rows in request_spans.items():
        roots.append({"case": case, "stage": stage, "samples": len(rows),
                      "wall_p50_ms": quantile([r["wall_s"] * 1000 for r in rows], .5),
                      "wall_p95_ms": quantile([r["wall_s"] * 1000 for r in rows], .95),
                      "sql_p50_ms": quantile([r["sql_s"] * 1000 for r in rows], .5),
                      "sql_count_p50": quantile([r["sql_count"] for r in rows], .5)})
    native_imports = [r for r in traces if r["event"] == "native_stage" and r.get("stage") == "import_execution"]
    derive_by_asset = defaultdict(list)
    claims = {}
    for row in traces:
        if row["event"] == "span" and row["stage"] == "media.derive" and row.get("asset_id"):
            derive_by_asset[row["asset_id"]].append(row)
        if row["event"] == "media_claimed":
            claims.setdefault(row["asset_id"], row)
    tails = []
    for job in jobs:
        snapshot = job.get("snapshots") or job.get("snapshot")
        for asset in snapshot.get("assets", []):
            if not asset.get("created_at") or not asset.get("completed_at"):
                continue
            from datetime import datetime
            duration = (datetime.fromisoformat(asset["completed_at"]) - datetime.fromisoformat(asset["created_at"])).total_seconds()
            rows = derive_by_asset.get(asset["asset_id"], [])
            compute = sum(r["wall_s"] for r in rows)
            claim = claims.get(asset["asset_id"])
            preclaim = max(0, (datetime.fromisoformat(claim["at"]) - datetime.fromisoformat(asset["created_at"])).total_seconds() - claim["wall_s"]) if claim else None
            tails.append({"asset_id": asset["asset_id"], "case": job["case"], "outbox_lifetime_s": duration,
                          "observed_derive_s": compute if rows else None,
                          "outside_derive_s": max(0, duration - compute) if rows else None,
                          "before_claim_s": preclaim,
                          "note": "outside_derive includes queue, resource waits, metadata/database handling and scheduling; not all pure queue"})
    download_network = [r for r in traces if r["event"] in {"network", "media_transfer", "download_budget_total", "download_budget_stop"}]
    result = {"functions": function_summary, "http_substages": roots, "native_import_attempts": native_imports,
              "media_tail_attribution": tails, "download_network": download_network, "jobs": jobs,
              "idempotent_replays": replays,
              "resource_admission": [r for r in traces if r["event"] == "span" and r["stage"] == "resource.admission"],
              "notes": ["Nested spans are inclusive and must not be summed.",
                        "Observed PSS includes process and descendants; media memory mode samples at the configured interval (100 ms by default), so an absent peak is unobserved rather than zero.",
                        "Sequential media summaries use child assets as the accounting source; the parent flat metrics view is not counted again.",
                        "Microbenchmark request latency includes docker exec and imports; function wall_ms excludes that startup.",
                        "P99 is omitted for module functions with fewer than 100 measured operations.",
                        "CPU deltas include process sampling overhead; concurrent HTTP CPU deltas are not per-request CPU attribution."]}
    (output / "module-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = ["", "## 原生函数时间与内存", "", "下表使用生产镜像中的实际函数；Provider 解析每批执行 100 次，时间按单次折算。",
             "独立媒体阶段通常分别启动进程；sequence 模式按同一解释器中的资产顺序保留 first_asset/following_asset。first_call 为该阶段或资产的首次调用，repeat_call 为同进程后续调用。文件缓存未清空，函数计时不含 Docker/进程启动。", "",
             "| 模块 | 样本 | 步骤 | 序列 | 样本数 | P50 | P95 | 观测最大 PSS 增量 |", "|---|---|---|---|---:|---:|---:|---:|"]
    for row in function_summary:
        memory_label = f"{row['max_pss_increase_mib']:.1f} MiB" if row["measurement"] == "memory" else "未采样"
        sequence_values = sorted({
            (item.get("sequence_index"), item.get("sequence_state"))
            for item in row["rows"]
            if item.get("sequence_index") is not None
        })
        sequence_label = ", ".join(f"{index}:{state}" for index, state in sequence_values)
        lines.append(f"| {row['module']} | {row['sample']} | {row['stage']} | {sequence_label} | {row['samples']} | {row['p50_ms']:.3f} ms | {row['p95_ms']:.3f} ms | {memory_label} |")
    lines += ["", "## HTTP 内部阶段", "", "| 场景 | 阶段 | 次数 | 墙钟 P50 | SQL P50 | SQL 次数 P50 |", "|---|---|---:|---:|---:|---:|"]
    for row in roots:
        lines.append(f"| {row['case']} | {row['stage']} | {row['samples']} | {row['wall_p50_ms']:.2f} ms | {row['sql_p50_ms']:.2f} ms | {row['sql_count_p50']} |")
    lines += ["", "## 异步尾部归因", "", "| 场景 | 资产 | 媒体意图到完成 | 观测 derivation 执行 | 执行之外 |", "|---|---|---:|---:|---:|"]
    for row in tails:
        observed = f"{row['observed_derive_s']:.3f} s" if row["observed_derive_s"] is not None else "未捕获"
        outside = f"{row['outside_derive_s']:.3f} s" if row["outside_derive_s"] is not None else "未归因"
        lines.append(f"| {row['case']} | {row['asset_id'][:8]} | {row['outbox_lifetime_s']:.3f} s | {observed} | {outside} |")
    lines += ["", "‘执行之外’包含排队、资源准入、元数据读取、数据库写回和调度，不能全部算成纯排队。", "",
              "## 导入阶段", "", "| 执行 | 排队 | 元数据准备 | 媒体准备 | 资源准入等待 | 提交 |", "|---|---:|---:|---:|---:|---:|"]
    for row in native_imports:
        values = row.get("phase_timings_ms", {})
        fields = [str(values.get(k, "缺失")) + " ms" for k in ("queue_wait", "metadata_prepare", "media_prepare", "admission_wait", "db_commit")]
        lines.append("| " + row.get("job_id", "")[:8] + " | " + " | ".join(fields) + " |")
    lines += ["", "## 平台下载分解", "", "| 来源 | 新文件／归集字节 | 同步完成 | 全部就绪 | 分类 |", "|---|---:|---:|---:|---|"]
    for job in jobs:
        if job["route"] not in {"download", "download-fresh"}:
            continue
        snapshot = job["snapshot"]
        lines.append(f"| {job['source']} | {snapshot['media_files']} / {snapshot['artifact_bytes']/1e6:.2f} MB | {job['terminal_ms']/1000:.2f} s | {job['all_ready_ms']/1000:.2f} s | {job['classification']} |")
    if download_network:
        transfers = [r for r in download_network if r["event"] == "media_transfer"]
        requests = [r for r in download_network if r["event"] == "network"]
        lines += ["", f"捕获平台请求 {len(requests)} 次，媒体接收 {len(transfers)} 次；具体任务、响应等待、接收及写入时间保存在 module-summary.json。",
                  "响应等待包含网络请求建立与平台响应；媒体接收包含网络读取和落盘，不将两者强行拆成无依据的纯网络时间。"]
    lines += ["", "## 本轮测量结论", ""]
    comparable = [r for r in tails if r["observed_derive_s"] is not None]
    if comparable:
        total = sum(r["outbox_lifetime_s"] for r in comparable)
        derive = sum(r["observed_derive_s"] for r in comparable)
        lines.append(f"- 可关联的 {len(comparable)} 个媒体资产，原生 derivation 执行占其各自 outbox 存续时间总和的 {100*derive/total:.1f}%；其余需要从资源等待、调度和写回继续归因。这里不是并行批次墙钟时间的占比。")
    if native_imports:
        queues = [r.get("phase_timings_ms", {}).get("queue_wait", 0) for r in native_imports]
        lines.append(f"- 捕获 {len(native_imports)} 次导入执行，排队 P50 {quantile(queues,.5)} ms、最大 {max(queues)} ms；各阶段为原生记录，嵌套阶段不相加。")
    if function_summary:
        lines.append("- 已得到按格式／来源分组的函数时间和进程内存增量；首次依赖加载和已加载后的 derivation 分开记录，不能直接混作同一种处理成本。")
    for replay in replays:
        lines.append(f"- {replay['source']} 来源重复导入，作品 {replay['work_count_before']}→{replay['work_count_after']}、资产 {replay['asset_count_before']}→{replay['asset_count_after']}，ID 集合一致：{replay['ids_unchanged']}；重放与子导入确认耗时 {replay['all_imports_ms']/1000:.2f} s。")
    lines += ["", "测量挂载会增加少量采样与写记录开销；查询容量以不启用 --profile 的基线为准。",
              "过程记录与结论对应本轮样本，不据此声称持续容量上限。完整分解在 module-summary.json。"]
    with (output / "results.zh.md").open("a") as handle:
        handle.write("\n".join(lines) + "\n")
