#!/usr/bin/env python3
"""Seed the local-performance stack through its real upload API.

Run through scripts/local-perf.sh seed. Credentials and progress stay in the
ignored local data directory. Images are synthetic RGB noise, not real photos.
"""
import argparse
import asyncio
from collections import Counter
import io
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, "/app")
from sqlalchemy import func, select
from app.database import async_session
from app.models import AssetSource, WorkSource


async def imported_media():
    async with async_session() as db:
        rows = (await db.execute(
            select(WorkSource.source_work_id, WorkSource.work_id, func.count(AssetSource.id))
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .where(WorkSource.source == "manual")
            .group_by(WorkSource.source_work_id, WorkSource.work_id)
        )).all()
    return {source_id: (str(work_id), count) for source_id, work_id, count in rows}


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


async def main(args):
    root = Path("/perf")
    env = dict(line.split("=", 1) for line in (root / "perf.env").read_text().splitlines()
               if line and not line.startswith("#") and "=" in line)
    credentials_path = root / "login.json"
    credentials = (json.loads(credentials_path.read_text()) if credentials_path.exists()
                   else {"username": "admin", "password": env["ADMIN_PASSWORD"]})
    manifest_path = root / "dataset.json"
    manifest = (json.loads(manifest_path.read_text()) if manifest_path.exists()
                else {"source": "synthetic JPEG via upload API", "bytes": 0, "assets": 0, "uploads": []})
    async with httpx.AsyncClient(base_url="http://backend:8000", timeout=120) as client:
        response = await client.post("/api/v1/auth/login", json=credentials)
        response.raise_for_status()
        login = response.json()
        client.headers["Authorization"] = "Bearer " + login["access_token"]
        if login["must_change_password"]:
            password = secrets.token_urlsafe(18)
            response = await client.post("/api/v1/auth/change-password", json={
                "current_password": credentials["password"], "new_password": password})
            response.raise_for_status()
            client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
            credentials["password"] = password
        save(credentials_path, credentials)
        target = int(args.target_gib * 1024**3)
        while manifest["bytes"] < target:
            files = []
            for number in range(4):
                width, height = (1920, 1080) if manifest["assets"] % 3 else (1080, 1920)
                image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
                ImageDraw.Draw(image).text((30, 30), f"LOCAL PERF {manifest['assets'] + number}", fill="white")
                data = io.BytesIO()
                image.save(data, format="JPEG", quality=85)
                content = data.getvalue()
                files.append(("files", (f"perf-{manifest['assets'] + number:06d}.jpg", content, "image/jpeg")))
                if manifest["bytes"] + sum(len(item[1][1]) for item in files) >= target:
                    break
            number = len(manifest["uploads"])
            for attempt in range(30):
                response = await client.post("/api/v1/upload", files=files, data={
                    "title": f"Local performance sample {number:04d}",
                    "tags": f"local-perf,synthetic,batch-{number % 16}"})
                if (response.status_code != 503 or
                        response.json().get("detail", {}).get("code") != "maintenance_in_progress"):
                    break
                if attempt % 3 == 0:
                    print("Storage maintenance is active; waiting for its write barrier", flush=True)
                await asyncio.sleep(int(response.headers.get("Retry-After", "10")))
            response.raise_for_status()
            receipt = response.json()
            if not receipt.get("import_job_id"):
                raise RuntimeError(f"Upload did not schedule an import: {receipt}")
            receipt["asset_count"] = len(files)
            manifest["uploads"].append(receipt)
            manifest["bytes"] += sum(len(item[1][1]) for item in files)
            manifest["assets"] += len(files)
            save(manifest_path, manifest)
            if number % 10 == 0 or manifest["bytes"] >= target:
                print(f"Uploaded {manifest['bytes'] / 1024**3:.3f} GiB, {manifest['assets']} images, {number + 1} works", flush=True)

        deadline = time.monotonic() + args.wait_seconds
        pending = {item["import_job_id"] for item in manifest["uploads"]}
        last_update = 0
        while pending:
            response = await client.get("/api/v1/import-jobs", params={"limit": 1000})
            response.raise_for_status()
            jobs = response.json()["items"]
            live_ids = {job["id"] for job in jobs}
            for job in jobs:
                if job["id"] not in pending:
                    continue
                if job["status"] in {"complete", "completed"}:
                    pending.remove(job["id"])
                elif job["status"] in {"failed", "cancelled", "canceled"}:
                    raise RuntimeError(f"Import {job['id']}: {job['status']} {job.get('error_log')}")
            # Production compaction can remove successful job rows during this
            # seed run. Confirm their persisted media rather than waiting for
            # a removed operational row to reappear.
            media = await imported_media()
            for item in manifest["uploads"]:
                persisted = media.get(item["work_id"])
                if persisted:
                    item["canonical_work_id"] = persisted[0]
                if item["import_job_id"] not in pending or item["import_job_id"] in live_ids:
                    continue
                if persisted and persisted[1] >= item.get("asset_count", 4):
                    pending.remove(item["import_job_id"])
            if time.monotonic() - last_update > 25:
                print(f"Imports: {len(manifest['uploads']) - len(pending)}/{len(manifest['uploads'])} complete", flush=True)
                last_update = time.monotonic()
            if time.monotonic() > deadline:
                raise TimeoutError(f"{len(pending)} imports still pending; rerun seed to resume")
            if pending:
                await asyncio.sleep(3)

        # Validate a real imported asset and its generated thumbnail.
        first = manifest["uploads"][0]
        deadline = time.monotonic() + 180
        while True:
            response = await client.get(f"/api/v1/works/{first['canonical_work_id']}/assets")
            response.raise_for_status()
            assets = response.json()
            if assets and assets[0].get("thumb_sm_path"):
                thumbnail = await client.get(f"/media/thumb/{assets[0]['id']}")
                if thumbnail.status_code == 200:
                    assert thumbnail.headers["content-type"].startswith("image/webp")
                    break
            if time.monotonic() > deadline:
                raise TimeoutError("Imported sample thumbnail is still unavailable")
            await asyncio.sleep(3)

        # Short end-to-end HTTP measurement, deliberately bounded.
        limiter = asyncio.Semaphore(8)
        measurements = []
        async def request(number):
            async with limiter:
                started = time.perf_counter()
                response = await client.get("/api/v1/works", params={"limit": 24, "offset": (number % 4) * 24})
                measurements.append((response.status_code, (time.perf_counter() - started) * 1000))
                response.raise_for_status()
                assert response.json()["total"] >= len(manifest["uploads"])
        started = time.perf_counter()
        await asyncio.gather(*(request(number) for number in range(200)))
        duration = time.perf_counter() - started
        latencies = sorted(item[1] for item in measurements)
        report = {
            "media_bytes": manifest["bytes"], "assets": manifest["assets"],
            "works": len(manifest["uploads"]), "imports_completed": len(manifest["uploads"]),
            "completion_evidence": "completed live import job or persisted assets after job compaction",
            "thumbnail_verified": True, "http_requests": len(measurements), "concurrency": 8,
            "statuses": dict(Counter(str(item[0]) for item in measurements)),
            "duration_seconds": round(duration, 3), "requests_per_second": round(200 / duration, 2),
            "p50_ms": round(latencies[math.ceil(len(latencies) * .5) - 1], 2),
            "p95_ms": round(latencies[math.ceil(len(latencies) * .95) - 1], 2),
            "scope": "short warmed HTTP work-list check on synthetic data; not a production capacity result",
        }
        save(root / "verification.json", report)
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-gib", type=float, default=1)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    asyncio.run(main(parser.parse_args()))
