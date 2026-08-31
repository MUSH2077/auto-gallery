"""Fail-closed tmpfs storage for short-lived personal download credentials."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from app.config import settings

_TMPFS_FILESYSTEM_TYPES = frozenset({"tmpfs", "ramfs"})
_AUTH_FILE_RE = re.compile(r"^auth-(?P<pid>\d+)-(?P<ticks>\d+)-.+\.json$")


class PersonalAuthStorageError(RuntimeError):
    """The worker cannot prove that credential storage is nonpersistent."""


def _unescape_mount_path(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _filesystem_type(path: Path) -> str | None:
    """Return the filesystem type for the longest matching Linux mount."""

    resolved = path.resolve()
    best: tuple[int, str] | None = None
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in mountinfo.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_unescape_mount_path(fields[4]))
            fs_type = fields[separator + 1]
            resolved.relative_to(mount_point)
        except (ValueError, IndexError):
            continue
        candidate = (len(mount_point.parts), fs_type)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else None


def _durable_roots() -> tuple[Path, ...]:
    return tuple(
        Path(value).resolve()
        for value in (
            settings.gallerydl_config_root,
            settings.app_config_root,
            settings.download_root,
            settings.library_root,
        )
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def prepare_personal_auth_root(root: str | Path | None = None) -> Path:
    """Create and validate the worker-only tmpfs credential directory."""

    configured = Path(root or settings.personal_auth_tmp_root)
    absolute = configured.absolute()
    if configured.exists() and configured.is_symlink():
        raise PersonalAuthStorageError("personal authentication root cannot be a symlink")
    resolved = configured.resolve()
    if resolved != absolute:
        raise PersonalAuthStorageError(
            "personal authentication root cannot traverse symbolic links"
        )
    for durable_root in _durable_roots():
        if _is_within(resolved, durable_root) or _is_within(durable_root, resolved):
            raise PersonalAuthStorageError(
                "personal authentication root overlaps durable application storage"
            )
    try:
        resolved.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(resolved, 0o700)
        directory_stat = resolved.stat()
    except OSError as exc:
        raise PersonalAuthStorageError(
            "personal authentication tmpfs is unavailable"
        ) from exc
    if directory_stat.st_uid != os.geteuid():
        raise PersonalAuthStorageError(
            "personal authentication tmpfs is not owned by this worker"
        )
    if _filesystem_type(resolved) not in _TMPFS_FILESYSTEM_TYPES:
        raise PersonalAuthStorageError(
            "personal authentication root must be mounted as tmpfs"
        )
    return resolved


def _process_start_ticks(pid: int) -> int | None:
    """Read Linux process start ticks, which disambiguate recycled PIDs."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields_after_command = raw[raw.rindex(")") + 2 :].split()
        return int(fields_after_command[19])
    except (OSError, ValueError, IndexError):
        return None


def sweep_abandoned_personal_auth_configs(root: str | Path | None = None) -> int:
    """Delete crash debris while preserving files owned by a live process."""

    prepared = prepare_personal_auth_root(root)
    removed = 0
    for candidate in prepared.glob("auth-*.json"):
        match = _AUTH_FILE_RE.fullmatch(candidate.name)
        live = False
        if match is not None:
            pid = int(match.group("pid"))
            ticks = int(match.group("ticks"))
            live = _process_start_ticks(pid) == ticks
        if live:
            continue
        try:
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PersonalAuthStorageError(
                "abandoned personal authentication config could not be removed"
            ) from exc
    return removed


def write_personal_auth_config(
    root: str | Path | None,
    *,
    job_id: str,
    payload: Mapping,
) -> Path:
    """Atomically create a mode-0600 JSON config under the validated tmpfs."""

    prepared = prepare_personal_auth_root(root)
    sweep_abandoned_personal_auth_configs(prepared)
    start_ticks = _process_start_ticks(os.getpid())
    if start_ticks is None:
        raise PersonalAuthStorageError(
            "worker process identity is unavailable for credential cleanup"
        )
    fd = -1
    path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=f"auth-{os.getpid()}-{start_ticks}-{job_id}-",
            suffix=".json",
            dir=prepared,
        )
        path = Path(raw_path)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, ensure_ascii=False)
        os.chmod(path, 0o600)
        return path
    except Exception:
        if fd >= 0:
            os.close(fd)
        if path is not None:
            remove_personal_auth_config(path)
        raise


def remove_personal_auth_config(path: str | Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "PersonalAuthStorageError",
    "prepare_personal_auth_root",
    "remove_personal_auth_config",
    "sweep_abandoned_personal_auth_configs",
    "write_personal_auth_config",
]
