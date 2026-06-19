"""SQLite file index — replaces rglob filesystem scans with O(log n) queries.

Located at {DOWNLOAD_ROOT}/.file-index.sqlite3. Tracks every file in
/downloads/ and /library/ with source, creator_dir, work_id, and import status.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    storage_root TEXT NOT NULL,
    source TEXT NOT NULL,
    creator_dir TEXT NOT NULL,
    work_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER,
    download_job_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    import_status TEXT NOT NULL DEFAULT 'new',
    import_job_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_index_work
    ON file_index(source, creator_dir, work_id);
CREATE INDEX IF NOT EXISTS idx_file_index_status
    ON file_index(import_status, first_seen_at);
CREATE INDEX IF NOT EXISTS idx_file_index_type
    ON file_index(file_type, import_status);
CREATE INDEX IF NOT EXISTS idx_file_index_job
    ON file_index(download_job_id, import_status);
"""

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip"}


def _classify_file_type(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith(".json"):
        return "metadata_json"
    ext = os.path.splitext(lower)[1]
    if ext in IMG_EXTS:
        return "image"
    if ext == ".zip":
        return "archive"
    if lower.endswith(".webp"):
        return "thumbnail"
    return "unknown"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class FileIndex:
    """Thread-safe SQLite file index for downloads and library storage."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def upsert(self, file_path, storage_root, source, creator_dir,
               work_id, file_name, file_type="", file_size=0,
               download_job_id="", import_status="new") -> None:
        now = _now_iso()
        if not file_type:
            file_type = _classify_file_type(file_name)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """INSERT INTO file_index
                       (file_path, storage_root, source, creator_dir, work_id,
                        file_name, file_type, file_size, download_job_id,
                        first_seen_at, last_seen_at, import_status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(file_path) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        file_size = COALESCE(excluded.file_size, file_index.file_size),
                        download_job_id = COALESCE(excluded.download_job_id, file_index.download_job_id)""",
                    (file_path, storage_root, source, creator_dir, work_id,
                     file_name, file_type, file_size or 0, download_job_id or "",
                     now, now, import_status),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_imported(self, file_path, import_job_id="") -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "UPDATE file_index SET import_status='done', import_job_id=?, last_seen_at=? WHERE file_path=?",
                    (import_job_id, _now_iso(), file_path),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_importing(self, file_path, import_job_id) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "UPDATE file_index SET import_status='importing', import_job_id=?, last_seen_at=? WHERE file_path=? AND import_status='new'",
                    (import_job_id, _now_iso(), file_path),
                )
                conn.commit()
            finally:
                conn.close()

    def get_new_metadata_jsons(self, source, download_job_id="") -> list[str]:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    """SELECT file_path FROM file_index
                       WHERE file_type='metadata_json' AND import_status='new'
                       AND source=? AND (?='' OR download_job_id=?)
                       ORDER BY first_seen_at""",
                    (source, download_job_id, download_job_id),
                )
                return [row[0] for row in cur.fetchall()]
            finally:
                conn.close()

    def count_new_artifacts(self, source, download_job_id) -> tuple:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(
                    "SELECT file_path, file_type FROM file_index WHERE download_job_id=? AND import_status='new'",
                    (download_job_id,),
                )
                rows = cur.fetchall()
                json_count = sum(1 for _, ft in rows if ft == "metadata_json")
                img_count = sum(1 for _, ft in rows if ft == "image")
                json_paths = [p for p, ft in rows if ft == "metadata_json"]
                return json_count, img_count, json_paths
            finally:
                conn.close()

    def bootstrap_if_empty(self, download_root, library_root) -> int:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                count = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
            finally:
                conn.close()
        if count > 0:
            return 0
        logger.info("Bootstrapping file index from %s and %s ...", download_root, library_root)
        total = 0
        for root_label, root_path in [("downloads", download_root), ("library", library_root)]:
            root = Path(root_path)
            if not root.exists():
                continue
            for fpath in root.rglob("*"):
                if not fpath.is_file():
                    continue
                rel = str(fpath.relative_to(root))
                parts = rel.split("/")
                if len(parts) < 3:
                    continue
                source = parts[0]
                creator_dir = parts[1]
                work_id = parts[2]
                import_status = "done" if root_label == "library" else "new"
                self.upsert(
                    file_path=rel, storage_root=root_label,
                    source=source, creator_dir=creator_dir,
                    work_id=work_id, file_name=fpath.name,
                    file_type=_classify_file_type(fpath.name),
                    file_size=fpath.stat().st_size,
                    import_status=import_status,
                )
                total += 1
                if total % 1000 == 0:
                    logger.info("Bootstrapped %d files ...", total)
        logger.info("File index bootstrap complete: %d files indexed", total)
        return total

    def vacuum(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
