"""Automated backup job — scheduled by admin API."""
import logging, os, subprocess, tempfile
from datetime import datetime, timezone, timedelta
from app.config import settings
from app.services.redis_client import get_redis

logger = logging.getLogger(__name__)

def run_auto_backup(interval: int = 24):
    """Create a backup tarball and re-schedule. Keeps last 7 backups."""
    logger.info("Starting auto-backup")
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(settings.app_config_root, "backups")
        os.makedirs(backup_dir, exist_ok=True)

        # DB dump
        db_url = settings.database_url.replace("+asyncpg", "")
        tmpdir = tempfile.mkdtemp()
        db_file = os.path.join(tmpdir, "db.sql")
        subprocess.run(["pg_dump", db_url, "-f", db_file], capture_output=True, timeout=300, check=False)

        # Tar configs + db
        out_file = os.path.join(backup_dir, f"auto-backup-{ts}.tar.gz")
        subprocess.run([
            "tar", "-czf", out_file,
            "-C", settings.app_config_root, ".",
            "-C", settings.gallerydl_config_root, ".",
            db_file,
        ], capture_output=True, timeout=300, check=False)

        # Keep last 7
        backups = sorted([f for f in os.listdir(backup_dir) if f.startswith("auto-backup-")], reverse=True)
        for old in backups[7:]:
            os.unlink(os.path.join(backup_dir, old))

        logger.info("Auto-backup created: %s (%d bytes)", out_file, os.path.getsize(out_file))
    except Exception as e:
        logger.error("Auto-backup failed: %s", e)

    # Re-schedule
    try:
        from rq import Queue
        Queue(name="scheduled", connection=get_redis()).enqueue_in(
            timedelta(hours=interval), "app.jobs.backup.run_auto_backup",
            interval=interval, job_timeout=3600)
    except Exception as e:
        logger.error("Failed to re-schedule auto-backup: %s", e)
