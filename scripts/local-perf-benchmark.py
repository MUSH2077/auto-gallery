#!/usr/bin/env python3
"""Repeatable query measurements against the existing local Docker stack.

Uses only Python's standard library plus Node's built-in fetch. It samples
Docker from the host, so no DB observer runs inside the measured backend.
"""
import atexit
import argparse
import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parent.parent
PERF = Path(os.environ.get("AUTO_GALLERY_PERF_ROOT", REPO / "data/local-perf")).resolve()
PROJECT = os.environ.get("AUTO_GALLERY_PERF_PROJECT", "auto-gallery-perf")
STORAGE_LIMIT = 15_000_000_000
PROFILE_SERVICES = ["backend", "worker-download", "worker-import", "worker-operations"]
STORAGE_MOUNTS = []


def compose(*arguments, overlay=None, use_local_workers=False):
    command = ["bash", str(REPO / "scripts/local-perf.sh"), "compose"]
    if overlay:
        command += ["-f", str(overlay)]
    environment = dict(os.environ)
    environment["AUTO_GALLERY_PERF_BASE_ONLY"] = "0" if use_local_workers else "1"
    subprocess.run([*command, *arguments], cwd=REPO, env=environment, check=True)


def enable_profile(output):
    # Keep the trace owned by the invoking WSL user; containers run as root.
    (output / "events.jsonl").touch(mode=0o600)
    overlay = output / "profile.compose.json"
    services = {name: {"environment": {"PYTHONPATH": "/perf-hooks:/app", "AUTO_GALLERY_PERF_TRACE": "/perf-results/events.jsonl",
                                        "AUTO_GALLERY_PERF_DOWNLOAD_LIMIT": "1000000000"},
                       "volumes": [str(REPO / "scripts/perf/instrumentation") + ":/perf-hooks:ro",
                                   str(REPO / "scripts/perf") + ":/perf-tools:ro", str(output) + ":/perf-results"]}
                for name in PROFILE_SERVICES}
    overlay.write_text(json.dumps({"services": services}, indent=2) + "\n")
    compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *PROFILE_SERVICES, overlay=overlay)
    events = output / "events.jsonl"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        rows = [json.loads(s) for s in events.read_text().splitlines()] if events.exists() else []
        if any(r["event"] == "instrumentation_error" for r in rows):
            raise RuntimeError("Temporary profiling hook failed to initialize; see events.jsonl")
        ready = {r["container"] for r in rows if r["event"] == "instrumentation_ready"}
        if len(ready) >= len(PROFILE_SERVICES):
            return
        time.sleep(1)
    raise RuntimeError("Profiling initialization timed out")


def fixtures(output, backend):
    result = subprocess.run(["docker", "exec", "-i", "-e", "AUTO_GALLERY_PERF_TRACE=", backend,
                             "python", "/perf-tools/control.py"], input=json.dumps({"action": "fixtures"}),
                            capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    (output / "fixtures.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return value


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=5)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class _EnginePipe:
    """One CLI tunnel per run when Docker Desktop has no WSL socket.

    HTTPResponse must not close the shared stdout after each response.
    """
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def get(self, path):
        with self.lock:
            if self.process is None:
                self.process = subprocess.Popen(["docker", "system", "dial-stdio"],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            process = self.process
            class Reader:
                def read(self, *args): return process.stdout.read(*args)
                def readline(self, *args): return process.stdout.readline(*args)
                def close(self): pass
            class ResponseSocket:
                def makefile(self, *_args): return Reader()
            try:
                process.stdin.write(f"GET {path} HTTP/1.1\r\nHost: docker\r\n\r\n".encode())
                process.stdin.flush()
                response = http.client.HTTPResponse(ResponseSocket())
                response.begin()
                body = response.read()
                if response.status != 200:
                    raise RuntimeError(f"Docker API returned {response.status}")
                return json.loads(body)
            except Exception:
                self.close()
                raise


_engine_pipe = _EnginePipe()
atexit.register(_engine_pipe.close)


def docker_get(path):
    if not Path("/var/run/docker.sock").exists():
        return _engine_pipe.get(path)
    connection = DockerConnection()
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Docker API returned {response.status}")
        return json.loads(response.read())
    finally:
        connection.close()


def allocated_storage():
    exclusions = ["--exclude=" + entry[2] for entry in STORAGE_MOUNTS]
    result = subprocess.run(["du", "-sx", "--block-size=1", *exclusions, str(PERF)], capture_output=True, text=True)
    # A concurrently removed temporary lock file can make du return 1 with a valid total.
    if not result.stdout.strip():
        raise RuntimeError("Unable to measure runtime storage")
    if "Permission denied" in result.stderr:
        raise RuntimeError("Runtime storage has an unreadable directory not covered by container-side accounting")
    total = int(result.stdout.split()[0])
    for container, target, excluded in STORAGE_MOUNTS:
        scan = subprocess.run(["docker", "exec", container, "du", "-sk", target], capture_output=True, text=True)
        if not scan.stdout.strip():
            raise RuntimeError("Unable to measure protected runtime storage in " + container)
        total += int(scan.stdout.split()[0]) * 1024
    return total


def node_binary():
    candidate = os.environ.get("NODE_BINARY") or shutil.which("node")
    if candidate:
        return candidate
    versions = sorted((Path.home() / ".nvm/versions/node").glob("v*/bin/node"))
    if versions:
        return str(versions[-1])
    raise RuntimeError("Node.js 20+ is required; activate nvm or set NODE_BINARY")


def generator_pss():
    """WSL load-generator subtree, kept separate from server containers."""
    pending = [os.getpid()]
    total = 0
    for pid in pending:
        try:
            pending.extend(int(v) for v in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError):
            pass
    return total


def collect(output, containers, stopped, interval=2):
    last_scan = 0
    storage = None
    try:
        with (output / "memory.jsonl").open("w") as handle:
            while not stopped.is_set():
                started = time.monotonic()
                phase_file = output / "phase.json"
                phase = json.loads(phase_file.read_text())["name"] if phase_file.exists() else "setup"
                rows = []
                for container in containers:
                    result = docker_get(f"/containers/{container['Id']}/stats?stream=false&one-shot=true")
                    memory = result.get("memory_stats", {})
                    stats = memory.get("stats", {})
                    if not memory.get("usage") or "anon" not in stats:
                        raise RuntimeError("Expected Linux cgroup v2 memory accounting is unavailable")
                    raw = memory["usage"]
                    rows.append({"name": container["Names"][0].lstrip("/"),
                                 "pids": result.get("pids_stats", {}).get("current"),
                                 "cpu_usage_ns": result.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0),
                                 "cpu_user_ns": result.get("cpu_stats", {}).get("cpu_usage", {}).get("usage_in_usermode", 0),
                                 "cpu_system_ns": result.get("cpu_stats", {}).get("cpu_usage", {}).get("usage_in_kernelmode", 0),
                                 "cpu_throttled_ns": result.get("cpu_stats", {}).get("throttling_data", {}).get("throttled_time", 0),
                                 "cpu_throttled_periods": result.get("cpu_stats", {}).get("throttling_data", {}).get("throttled_periods", 0),
                                 "raw_bytes": raw, "working_set_bytes": max(0, raw - stats.get("inactive_file", 0)),
                                 **{key + "_bytes": stats.get(key, 0) for key in
                                    ("anon", "file", "active_file", "inactive_file", "kernel")}})
                if started - last_scan >= 10:
                    storage = allocated_storage()
                    last_scan = started
                    if storage >= STORAGE_LIMIT - 1_000_000_000:
                        raise RuntimeError("Runtime data reached the 14 GB stop threshold; 1 GB headroom is reserved")
                sample = {"at": datetime.now(timezone.utc).isoformat(), "phase": phase,
                          "containers": rows, "storage_bytes": storage, "generator_pss_bytes": generator_pss()}
                handle.write(json.dumps(sample) + "\n")
                handle.flush()
                latest = output / "resource-latest.json"
                latest.with_suffix(".tmp").write_text(json.dumps(sample))
                latest.with_suffix(".tmp").replace(latest)
                stopped.wait(max(0, interval - (time.monotonic() - started)))
    except Exception as exc:
        (output / "stop.txt").write_text(str(exc) + "\n")
        print(f"Collector stopped: {exc}", file=sys.stderr, flush=True)


def summarize(output):
    request_file = output / "requests.json"
    run = json.loads(request_file.read_text()) if request_file.exists() else {"cases": [], "error": "No client results"}
    memory_file = output / "memory.jsonl"
    samples = [json.loads(line) for line in memory_file.read_text().splitlines()] if memory_file.exists() else []
    groups = {}
    for case in run["cases"]:
        if not case.get("completed"):
            continue
        rows = [x for x in samples if x["phase"] == case["name"]]
        case["memory_samples"] = len(rows)
        case["container_memory"] = {}
        for name in sorted({c["name"] for x in rows for c in x["containers"]}):
            selected = [c for x in rows for c in x["containers"] if c["name"] == name]
            case["container_memory"][name] = {
                key: max(c[key] for c in selected)
                for key in ("anon_bytes", "file_bytes", "active_file_bytes", "working_set_bytes", "raw_bytes")}
        groups.setdefault((case["route"], case["via"], case["concurrency"]), []).append(case)
    summary = {
        "scope": run.get("scope"),
        "fixtures": run.get("fixtures"), "error": run.get("error"),
        "completed": bool(run.get("completed_at")) and not (output / "stop.txt").exists(),
        "passed": bool(run.get("completed_at")) and not any(c.get("errors") for c in run["cases"]) and not (output / "stop.txt").exists(),
        "limits": run.get("limits"), "cases": [{k: v for k, v in c.items() if k != "requests"} for c in run["cases"]],
        "max_sampled_stack_working_set_bytes": max((sum(c["working_set_bytes"] for c in x["containers"]) for x in samples), default=0),
        "max_sampled_stack_and_client_bytes": max((sum(c["working_set_bytes"] for c in x["containers"]) + x["generator_pss_bytes"] for x in samples), default=0),
        "max_runtime_storage_bytes": max((x["storage_bytes"] or 0 for x in samples), default=0),
        "memory_definition": "Container anon and file are separate. Working set includes active_file and is not process RSS/PSS. Per-container maxima are not simultaneous.",
    }
    if run.get("scope") == "tail":
        summary["tail_measurements"] = [operation["measurements"] for case in run["cases"]
                                        for operation in case["requests"] if operation.get("measurements")]
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 模块性能测试结果", "", f"范围：{summary['scope']}；完成：{summary['completed']}；结果目录：`{output}`。", "",
             "各模块独立测量；frontend 为真实前端代理，backend 为直接调用相同后端端点。",
             "查询采用闭环客户端和热缓存，每个请求读完响应体；有限模块按明确任务数执行并等待完成。",
             "客户端耗时与后文函数计时分开；两条入口顺序执行，差值不能直接当作纯代理耗时。", "",
             "| 路径 | 入口 | 并发 | 完成轮次 | QPS 中位数 | 每轮 P95 中位数 | P95 范围 | 错误/请求 |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for (route, via, n), cases in groups.items():
        p95s = [c["p95_ms"] for c in cases if c["p95_ms"] is not None]
        latency = f"{statistics.median(p95s):.1f} ms | {min(p95s):.1f}–{max(p95s):.1f} ms" if p95s else "无成功样本 | 无成功样本"
        lines.append(f"| {route} | {via} | {n} | {len(cases)} | {statistics.median(c['qps'] for c in cases):.1f} | {latency} | {sum(c['errors'] for c in cases)}/{sum(len(c['requests']) for c in cases)} |")
    if run.get("scope") == "tail":
        tails = summary["tail_measurements"]
        lines += ["", "## 索引与去重收尾", "",
                  "| 批次 | 全批索引文档可读 | 本地确认 | 即时去重完成 |", "|---|---:|---:|---:|"]
        for measurement in tails:
            marks = measurement["marks"]
            durations = [f"{marks[key]:.3f} s" if key in marks else "—"
                         for key in ("visible_s", "acknowledged_s", "dedup_s")]
            lines.append("| " + measurement["label"] + " | " + " | ".join(durations) + " |")
        lines += ["", "以上为每秒观察的单调时钟完成上界，含发布、执行与等待；文档可读通过搜索服务文档接口确认，表示全批更新已可读取，不是点击搜索的响应时间。真实搜索查询及分组、代表和存储效果在完成时另行核对。",
                  "未来才到期的隔离清理不计入即时收尾。远端、RQ 和 SQL 时间戳保留在 tail_measurements，不能把批次 QPS 当作查询吞吐。"]
    lines += ["", "## 内存记录", "", "| 容器 | 匿名内存峰值 | 文件缓存峰值 | 容器工作集峰值 |", "|---|---:|---:|---:|"]
    for name in sorted({c["name"] for x in samples for c in x["containers"]}):
        selected = [c for x in samples for c in x["containers"] if c["name"] == name]
        peaks = [max(c[key] for c in selected) / 2**20 for key in ("anon_bytes", "file_bytes", "working_set_bytes")]
        lines.append(f"| {name} | {peaks[0]:.1f} MiB | {peaks[1]:.1f} MiB | {peaks[2]:.1f} MiB |")
    lines += ["", "## 本次观察", ""]
    for (route, via, n), cases in groups.items():
        if n != 8 or (route, via, 4) not in groups:
            continue
        lower = groups[(route, via, 4)]
        high_qps = statistics.median(c["qps"] for c in cases)
        low_qps = statistics.median(c["qps"] for c in lower)
        high_p95 = [c["p95_ms"] for c in cases if c["p95_ms"] is not None]
        low_p95 = [c["p95_ms"] for c in lower if c["p95_ms"] is not None]
        if high_p95 and low_p95:
            lines.append(f"- {route}/{via}：4→8 并发，QPS {low_qps:.1f}→{high_qps:.1f}，每轮 P95 中位数 {statistics.median(low_p95):.1f}→{statistics.median(high_p95):.1f} ms；仅对应本次样本、时长和重复次数。")
    lines.append(f"共 {len(groups)} 组路径/并发组合，错误 {sum(c.get('errors', 0) for c in run['cases'])}；任务完成与错误数分别记录，失败结果保留。")
    lines += ["", "这些内存峰值包含已驻留数据和后台服务，不等于单次请求新增内存；各列、各容器峰值也不能直接相加。",
              "`summary.json` 保留每个测试窗口的容器指标及采样数量；短窗口可能没有足够采样，不能宣称精准峰值。", "",
              "## 结论边界", "", "当前可比较单条 HTTP 路径在 1/4/8 并发下的延迟、吞吐和错误率，并排除旧混合请求比例的干扰。",
              "HTTP 总时间包含认证、通信和应用处理；启用 profiling 的内部阶段、SQL 及原生函数计时单独列在后文，嵌套阶段不相加。",
              "有限任务模块记录原生函数成本或正常页面操作及异步完成，具体模式保存在每个 case 和 measurements。查询默认不写入媒体；上传会新增真实作品，下载可能新增真实媒体。", "",
              "记录：config.json（无密码）、requests.json（逐请求延迟）、memory.jsonl、summary.json、client.log。"]
    if run.get("error"):
        lines += ["", "运行错误：" + run["error"]]
    if (output / "stop.txt").exists():
        lines += ["", "采集错误：" + (output / "stop.txt").read_text().strip()]
    (output / "results.zh.md").write_text("\n".join(lines) + "\n")
    from importlib.util import spec_from_file_location, module_from_spec
    specification = spec_from_file_location("perf_analyze", REPO / "scripts/perf/analyze.py")
    analyzer = module_from_spec(specification)
    specification.loader.exec_module(analyzer)
    if request_file.exists():
        analyzer.analyze(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=["queries", "media", "tail", "import", "upload", "download", "all"], default="queries")
    parser.add_argument("--native-container", help="prepared media/tail container; skips HTTP login and Compose profiling")
    parser.add_argument("--fixtures", type=Path, help="explicit fixture JSON for a prepared native container")
    parser.add_argument("--asset-ids", nargs="+", help="restrict media measurements to these fixture IDs")
    parser.add_argument("--tail-kind", choices=["search", "dedup", "mixed"], default="mixed")
    parser.add_argument("--media-stages", nargs="+", choices=["thumbnail_and_dimensions", "sha256", "phash", "ssim", "native_derive_sync", "native_derive_existing_thumbnail"],
                        default=["thumbnail_and_dimensions", "sha256", "phash", "native_derive_sync"])
    parser.add_argument("--media-measurement", choices=["timing", "memory"], default="timing")
    parser.add_argument("--media-sequence", action="store_true", help="process selected media assets successively in one interpreter")
    parser.add_argument("--media-sample-ms", type=int, default=100)
    parser.add_argument("--resource-interval", type=float, default=2, help="seconds; 0 disables concurrent collection for media/tail/upload timing")
    parser.add_argument("--media-iterations", type=int, default=2, help="first and repeated calls in each isolated stage process")
    parser.add_argument("--seconds", type=float, default=10, help="seconds per case (default: 10)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--routes", choices=["list", "search", "thumbnail"], nargs="+", default=["list", "search", "thumbnail"])
    parser.add_argument("--modules", nargs="+", type=Path, help="replacement CommonJS module files; overrides --routes")
    parser.add_argument("--via", choices=["frontend", "backend"], nargs="+", default=["frontend", "backend"])
    parser.add_argument("--search", help="known indexed text; otherwise discover a title term with nonempty results")
    parser.add_argument("--no-profile", action="store_true", help="measure existing runtime without temporary Compose tracing mounts")
    parser.add_argument("--profile", action="store_true", help="temporarily enable backend/worker substage tracing (automatic for non-query scopes)")
    parser.add_argument("--samples", type=int, default=2, help="smallest/largest media samples per format")
    parser.add_argument("--tasks", type=int, default=0, help="finite upload tasks per case; default equals input concurrency")
    parser.add_argument("--timeout-seconds", type=int, default=1200, help="asynchronous settle deadline")
    parser.add_argument("--download-posts", type=int, default=40)
    parser.add_argument("--replay-source", choices=["pixiv", "x", "danbooru", "manual"], default="pixiv")
    args = parser.parse_args()
    if args.seconds <= 0 or args.repeats < 1 or any(n < 1 or n > 8 for n in args.concurrency):
        parser.error("seconds > 0, repeats >= 1, and concurrency between 1 and 8 are required")
    if args.samples < 1 or args.tasks < 0 or not 1 <= args.download_posts <= 60:
        parser.error("samples >= 1, tasks >= 0, download-posts between 1 and 60")
    names = args.routes if args.scope == "queries" else (["media", *args.routes, "import", "upload", "download-fresh", "download", "import-replay"] if args.scope == "all" else [args.scope])
    modules = [p.resolve() for p in args.modules] if args.modules else [REPO / "scripts/perf/modules" / (name + ".cjs") for name in names]
    for module in modules:
        if not module.is_file():
            parser.error(f"Module file does not exist: {module}")
    if args.media_sample_ms < 20 or args.resource_interval < 0:
        parser.error("media-sample-ms >= 20 and resource-interval >= 0")
    if args.resource_interval == 0 and args.scope not in ("media", "tail", "upload"):
        parser.error("resource-interval 0 is only supported for media/tail/upload measurements")
    if args.media_iterations < 1:
        parser.error("media-iterations >= 1")
    if args.native_container and (args.scope not in ("media", "tail") or not args.fixtures or args.modules):
        parser.error("native-container requires --scope media/tail and --fixtures without replacement modules")
    PERF.mkdir(parents=True, exist_ok=True)
    if not args.native_container and not (PERF / "login.json").exists():
        raise RuntimeError("Missing data/local-perf/login.json; configure the local test administrator first")
    env = {} if args.native_container else dict(line.split("=", 1) for line in (PERF / "perf.env").read_text().splitlines() if line and not line.startswith("#") and "=" in line)
    containers = [c for c in docker_get("/containers/json") if c["Labels"].get("com.docker.compose.project") == PROJECT]
    seen_mounts = set()
    for c in containers:
        details = docker_get(f"/containers/{c['Id']}/json")
        for mount in details["Mounts"]:
            source_text = mount["Source"]
            # Docker Desktop reports WSL bind sources as UNC paths.
            for host in ("wsl$", "wsl.localhost"):
                prefix = "//" + host + "/" + os.environ.get("WSL_DISTRO_NAME", "") + "/"
                normalized = source_text.replace(chr(92), "/")
                if normalized.startswith(prefix):
                    source_text = "/" + normalized[len(prefix):]
                    break
            source = Path(source_text)
            key = mount.get("Name") or str(source)
            if key in seen_mounts:
                continue
            local_bind = source != PERF and source.is_relative_to(PERF)
            if mount["Type"] == "volume" or local_bind:
                seen_mounts.add(key)
                excluded = str(source) if local_bind else "__engine_volume__"
                STORAGE_MOUNTS.append((c["Names"][0].lstrip("/"), mount["Destination"], excluded))
    if args.native_container:
        details = docker_get(f"/containers/{args.native_container}/json")
        if not details["State"]["Running"]:
            raise RuntimeError("Prepared native container is not running")
        for mount in details["Mounts"]:
            if mount["Type"] == "volume" and mount.get("Name") not in seen_mounts:
                STORAGE_MOUNTS.append((args.native_container, mount["Destination"], "__native_volume__"))
        if not any(c["Id"] == details["Id"] for c in containers):
            containers.append({"Id": details["Id"], "Names": ["/" + args.native_container], "Labels": details["Config"]["Labels"] or {}})
    if not args.native_container and not any(c["Labels"].get("com.docker.compose.service") == "backend" for c in containers):
        raise RuntimeError("Local stack is down; run bash scripts/local-perf.sh up")
    node = node_binary()
    if allocated_storage() >= STORAGE_LIMIT:
        raise RuntimeError("Runtime data already reaches 15 GB; no benchmark started")
    os.umask(0o077)
    # Two simultaneous invocations would exceed the user's global test concurrency budget.
    with (PERF / "benchmark.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another performance run is active")
        output = PERF / "benchmarks" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output.mkdir(parents=True)
        print(f"Results: {output}", flush=True)
        profile = not args.native_container and not args.no_profile and (args.profile or args.scope != "queries" or any(p.stem in ("media", "import", "import-replay", "upload", "download", "download-fresh") for p in modules))
        config = {**vars(args), "profile": profile, "run_id": output.name, "modules": [str(p) for p in modules], "output": str(output), "credentials": str(PERF / "login.json"),
                  "frontend": "http://localhost:" + env.get("ADMIN_WEB_PORT", "23000"),
                  "backend": "http://localhost:" + env.get("BACKEND_PORT", "18818"),
                  "limits": {"concurrency": 8, "download_bytes": 1_000_000_000, "runtime_storage_bytes": STORAGE_LIMIT},
                  "downloads_enabled": any(p.stem in ("download", "download-fresh") for p in modules),
                  "download_root": str(PERF / "downloads"), "backend_container": args.native_container or PROJECT + "-backend-1",
                  "playwright_module": str(REPO / "admin-web/node_modules/playwright"),
                  "initial_runtime_bytes": allocated_storage(), "storage_stop_bytes": 14_000_000_000}
        config_file = output / "config.json"
        if args.fixtures:
            config["fixtures"] = json.loads(args.fixtures.read_text())
        if args.native_container:
            config["native_only"] = True
        config["fixtures_file"] = str(args.fixtures) if args.fixtures else None
        if not args.fixtures:
            config.pop("fixtures", None)
        config_file.write_text(json.dumps(config, default=str, indent=2) + "\n")
        profile_started = False
        try:
            if profile:
                profile_started = True
                enable_profile(output)
                config["fixtures"] = config.get("fixtures") or fixtures(output, config["backend_container"])
                if config["downloads_enabled"]:
                    first = config["fixtures"]["assets"][0]
                    probe = subprocess.run(["docker", "exec", "-e", "AUTO_GALLERY_PERF_TRACE=", config["backend_container"],
                                            "python", "/perf-tools/budget_check.py", "/downloads/" + first["file_path"]],
                                           capture_output=True, text=True)
                    check_file = output / "byte-budget-check/first-bytes.bin"
                    if probe.returncode != 74 or not check_file.exists() or check_file.stat().st_size != 9:
                        raise RuntimeError("Download byte-budget hook check failed before platform traffic")
                    (output / "byte-budget-verified.json").write_text(json.dumps({"limit": 10, "written": 9, "over_limit_exit": 74}) + "\n")
            if not args.native_container:
                containers = [c for c in docker_get("/containers/json") if c["Labels"].get("com.docker.compose.project") == PROJECT]
        except BaseException:
            (output / "setup-failed.json").write_text(json.dumps({"failed": True}) + "\n")
            if profile_started:
                compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *PROFILE_SERVICES)
            raise
        config_file.write_text(json.dumps(config, default=str, ensure_ascii=False, indent=2) + "\n")
        metadata = []
        for c in containers:
            details = docker_get(f"/containers/{c['Id']}/json")
            metadata.append({"name": c["Names"][0].lstrip("/"), "image": details["Config"]["Image"],
                             "image_id": details["Image"], "memory_limit": details["HostConfig"]["Memory"],
                             "nano_cpus": details["HostConfig"]["NanoCpus"], "pids_limit": details["HostConfig"].get("PidsLimit")})
        (output / "containers.json").write_text(json.dumps(metadata, indent=2) + "\n")
        stopped = threading.Event()
        collector = threading.Thread(target=collect, args=(output, containers, stopped, args.resource_interval), daemon=True)
        if args.resource_interval:
            collector.start()
        process = None
        status = 1
        try:
            with (output / "client.log").open("w") as log:
                process = subprocess.Popen([node, str(REPO / "scripts/perf/runner.cjs"), str(config_file)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                status = process.wait()
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stopped.set()
            if args.resource_interval:
                collector.join(timeout=60)
            if collector.is_alive():
                (output / "stop.txt").write_text("Memory collector did not finish; report may be incomplete\n")
            try:
                summarize(output)
            finally:
                if profile_started:
                    compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", *PROFILE_SERVICES)
                    (output / "profile-restored.json").write_text(json.dumps({"restored": True}) + "\n")
            print(f"Report: {output / 'results.zh.md'}", flush=True)
        return status or int((output / "stop.txt").exists())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
