"""Bounded module microbenchmarks using the production image's own functions."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time

from app.config import settings


def memory_tree():
    pending = [__import__("os").getpid()]
    total = {"rss_bytes": 0, "pss_bytes": 0, "anon_bytes": 0}
    for pid in pending:
        try:
            pending.extend(int(v) for v in Path(f"/proc/{pid}/task/{pid}/children").read_text().split())
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in ("Rss", "Pss", "Pss_Anon"):
                    total[{"Rss": "rss_bytes", "Pss": "pss_bytes", "Pss_Anon": "anon_bytes"}[key]] += int(value.split()[0]) * 1024
        except (OSError, ValueError):
            pass
    return total


class Measure:
    """Timing mode has no sampling thread; memory mode is diagnostic only."""
    mode = "memory"
    interval = 0.1

    def __init__(self, stage, rows):
        self.stage, self.rows = stage, rows

    def __enter__(self):
        self.before = memory_tree() if self.mode == "memory" else None
        self.peak = dict(self.before) if self.before else None
        self.stop = threading.Event()
        self.thread = None
        if self.mode == "memory":
            def sample():
                while not self.stop.wait(self.interval):
                    for key, value in memory_tree().items():
                        self.peak[key] = max(value, self.peak[key])
            self.thread = threading.Thread(target=sample, daemon=True)
            self.thread.start()
        self.wall, self.cpu = time.perf_counter(), time.process_time()
        self.children = resource.getrusage(resource.RUSAGE_CHILDREN)
        return self

    def __exit__(self, typ, exc, tb):
        wall, cpu = time.perf_counter() - self.wall, time.process_time() - self.cpu
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.stop.set()
        if self.thread:
            self.thread.join()
        after = memory_tree() if self.mode == "memory" else None
        if after:
            for key, value in after.items():
                self.peak[key] = max(value, self.peak[key])
        self.rows.append({"stage": self.stage, "wall_s": wall, "cpu_s": cpu,
                          "children_cpu_s": children.ru_utime + children.ru_stime - self.children.ru_utime - self.children.ru_stime,
                          "memory_before": self.before, "memory_peak_observed": self.peak, "memory_after": after,
                          "pss_increase_bytes": max(0, self.peak["pss_bytes"] - self.before["pss_bytes"]) if self.peak else None,
                          "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                          "measurement": self.mode, "outcome": "error" if typ else "ok",
                          "sampling_interval_ms": self.interval * 1000 if self.thread else None})


def media(request):
    measurement = request.get("measurement", "memory")
    entry_memory_before = memory_tree() if measurement == "memory" else None
    from app.services.thumbnail import inspect_and_generate_thumbnail
    from app.services.work_import import WorkImportService
    from app.services.image_utils import can_compute_phash
    from app.services.media_assets import render_video_derivatives
    from app.services.media_derivatives import _derive_sync, MEDIA_ALGORITHM_VERSION, PHASH_ALGORITHM_VERSION
    entry_memory_after = memory_tree() if measurement == "memory" else None
    Measure.mode = measurement
    Measure.interval = request.get("sample_ms", 100) / 1000
    asset = request["asset"]
    source = Path(settings.download_root) / asset["file_path"]
    output = Path("/perf-results/media") / request["operation_id"]
    output.mkdir(parents=True)
    rows = []
    stat = source.stat()
    library = Path(settings.library_root) / ".perf-probe" / request["operation_id"]
    descriptor = {"asset_id": asset["id"], "file_path": asset["file_path"], "file_name": source.name,
                  "mime_type": asset["mime"], "library_dir": str(library), **request.get("descriptor", {})}
    intent = {"source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
              "algorithm_version": MEDIA_ALGORITHM_VERSION,
              "requested": {"thumbnail": True, "dimensions": True, "sha256": True,
                            "phash": can_compute_phash(str(source)), "video": source.suffix.lower() in (".mp4", ".webm")}}
    results = {}
    stages = request.get("stages", ["thumbnail_and_dimensions", "sha256", "phash", "native_derive_sync"])
    try:
        for stage in stages:
            if stage == "native_derive_existing_thumbnail":
                # Preparing an already-existing card in this interpreter would
                # load libvips and contaminate the worker's RSS high-water mark.
                preparation = subprocess.run(
                    [sys.executable, "-c",
                     "import json,sys; from pathlib import Path; "
                     "from app.services.thumbnail import inspect_and_generate_thumbnail; "
                     "source,library=json.loads(sys.stdin.read()); "
                     "print(json.dumps(inspect_and_generate_thumbnail(source,Path(library))))"],
                    input=json.dumps([str(source), str(library)]),
                    capture_output=True, text=True, check=True,
                )
                thumb, width, height = json.loads(preparation.stdout)
                if not thumb:
                    raise RuntimeError("Cannot prepare the pre-existing first card")
                descriptor.update(thumb_sm_path=str(Path(thumb).relative_to(settings.library_root)),
                                  width=width, height=height, derivative_version=MEDIA_ALGORITHM_VERSION,
                                  derivative_source_size=stat.st_size, derivative_source_mtime_ns=stat.st_mtime_ns)
            for iteration in range(request.get("iterations", 1)):
                applicable = True
                with Measure(stage, rows):
                    if stage == "thumbnail_and_dimensions":
                        if intent["requested"]["video"]:
                            result = render_video_derivatives(source, output, "isolated", force=True)
                            if result.error or not result.thumbnail_path:
                                raise RuntimeError("Native video derivatives failed")
                        else:
                            thumbnail, width, height = inspect_and_generate_thumbnail(str(source), output)
                            if not thumbnail or not width or not height:
                                raise RuntimeError("Native thumbnail failed")
                            results["dimensions"] = [width, height]
                    elif stage == "sha256":
                        if not WorkImportService.sha256(source):
                            raise RuntimeError("No native SHA fingerprint")
                    elif stage == "phash":
                        from app.services.image_utils import open_pillow_image
                        applicable = can_compute_phash(str(source))
                        if applicable:
                            with open_pillow_image(source) as image:
                                applicable = not (image.format == "AVIF" and image.n_frames > 1)
                        results["phash_applicable"] = applicable
                        if applicable:
                            if PHASH_ALGORITHM_VERSION == "imagehash-phash-v1":
                                value = WorkImportService.phash(source)
                            else:
                                from app.services.visual_image import prepare_visual_work
                                work = prepare_visual_work(source)
                                if work is None:
                                    value = WorkImportService.phash(source, oriented=True)
                                else:
                                    with work:
                                        value = WorkImportService.phash(source, working=work, oriented=True)
                            if not value:
                                raise RuntimeError("No native perceptual fingerprint")
                            results["phash"] = value
                    elif stage == "ssim":
                        applicable = asset["mime"].startswith("image/")
                        if applicable:
                            from app.services.asset_reconciliation import structural_similarity
                            right = Path(settings.download_root) / request.get("right_file_path", asset.get("right_file_path", asset["file_path"]))
                            options = request.get("ssim_options", {})
                            if PHASH_ALGORITHM_VERSION != "imagehash-phash-v1":
                                options = {"shared": True, **options}
                                if asset.get("use_sample_cache"):
                                    from uuid import UUID
                                    options.update(left_id=UUID(asset["id"]), right_id=UUID(asset["right_id"]),
                                                   left_sha=asset["sha256"], right_sha=asset["right_sha256"])
                            results["ssim"] = structural_similarity(source, right, **options)
                    elif stage in ("native_derive_sync", "native_derive_existing_thumbnail"):
                        derived = _derive_sync(descriptor, intent)
                        results["native_result_fields"] = sorted(derived)
                    else:
                        raise ValueError("Unknown media stage: " + stage)
                rows[-1].update(iteration=iteration, process_state="first_call" if iteration == 0 else "repeat_call")
                if stage == "thumbnail_and_dimensions" and intent["requested"]["video"]:
                    rows[-1]["stage"] = "video_probe_and_render"
                if not applicable:
                    rows[-1]["outcome"] = "not_applicable"
    finally:
        if library.exists():
            for child in library.iterdir():
                child.unlink()
            library.rmdir()
    results["loaded_numeric_libraries"] = {name: name in sys.modules for name in ("numpy", "scipy")}
    return {"asset_id": asset["id"], "format": source.suffix, "bytes": stat.st_size,
            "width": asset["width"], "height": asset["height"], "metrics": rows,
            **results, "entry_memory_before": entry_memory_before, "entry_memory_after": entry_memory_after,
            "mode": "isolated_stage_process_warm_filesystem"}


def media_sequence(request):
    """Measure different assets in one interpreter, as in an outbox slice."""
    metrics, results = [], []
    for index, asset in enumerate(request["assets"]):
        result = media({**request, "action": "media", "asset": asset,
                        "operation_id": request["operation_id"] + "-" + str(index)})
        for row in result["metrics"]:
            row.update(asset_id=asset["id"], file_path=asset["file_path"],
                       sequence_index=index,
                       sequence_state="first_asset" if index == 0 else "following_asset")
        metrics.extend(result["metrics"])
        results.append(result)
    return {"asset_id": "sequence", "format": "mixed", "width": 0, "height": 0,
            "bytes": sum(result["bytes"] for result in results), "metrics": metrics,
            "assets": results, "mode": "sequential_media_assets"}


async def provider(request):
    from app.providers import registry
    from app.database import async_session, engine
    from app.models import WorkSource
    from sqlalchemy import select
    item = request["provider"]
    parser = registry.get(item["source"])
    if str(parser.parse_work_source(item["raw_metadata"]).get("source_work_id")) != item["source_work_id"]:
        raise RuntimeError("Provider fixture does not resolve to its stored source work identity")
    rows = []
    # Amortize tiny parser operations, excluding process/import startup time.
    for method in ("parse_work_source", "parse_source_creator", "parse_source_tags"):
        with Measure(method, rows):
            for _ in range(100):
                getattr(parser, method)(item["raw_metadata"])
        rows[-1]["operations"] = 100
        rows[-1]["per_operation_ms"] = rows[-1]["wall_s"] * 10
    try:
        async with async_session() as db:
            with Measure("existing_identity_lookup_with_connection", rows):
                found = (await db.execute(select(WorkSource.id).where(WorkSource.source == item["source"],
                         WorkSource.source_work_id == item["source_work_id"]))).scalar_one_or_none()
            if found is None:
                raise RuntimeError("Fixture source identity missing from the database")
    finally:
        await engine.dispose()
    return {"source": item["source"], "metrics": rows, "mode": "provider_parse_and_existing_lookup"}


request = json.loads(sys.stdin.read())
if request["action"] == "media_sequence":
    result = media_sequence(request)
else:
    result = media(request) if request["action"] == "media" else asyncio.run(provider(request))
print(json.dumps(result))
