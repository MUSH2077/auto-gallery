"""Memory and lock requirements shared by media selection and recovery."""
from __future__ import annotations

PARALLEL_IMAGE_RESERVATION_BYTES = 768 * 1024 ** 2


def _media_slot_limit() -> int:
    from app.config import MAX_MEDIA_WORKER_CONCURRENCY, settings

    try:
        configured = int(settings.media_worker_concurrency)
    except (AttributeError, TypeError, ValueError):
        configured = 1
    return max(1, min(int(MAX_MEDIA_WORKER_CONCURRENCY), configured))


def media_memory_bytes(snapshot: dict, workload: str) -> int:
    from app.services.resource_pressure import RESOURCE_PROFILES

    profile = ((snapshot.get("budget") or {}).get("profiles") or {}).get(workload) or {}
    base = RESOURCE_PROFILES[workload].memory_reservation_bytes
    try:
        requested = max(0, int(profile.get("memory_reservation_bytes", base)))
    except (ValueError, TypeError):
        requested = int(base)
    if workload == "image_derive" and _media_slot_limit() > 1:
        requested = max(requested, PARALLEL_IMAGE_RESERVATION_BYTES)
    return requested


def media_reservation_details(snapshot: dict, workload: str) -> tuple[int, int]:
    from app.services.heavy_io import resource_reservation_details

    _, capacity = resource_reservation_details(snapshot, workload)
    return media_memory_bytes(snapshot, workload), capacity


def media_profile_admissible(snapshot: dict, workload: str, leases: dict) -> bool:
    from app.services.resource_pressure import profile_slice_limits
    from app.services.heavy_io import MEDIA_RESERVATION_KEYS

    if leases.get("error") or not profile_slice_limits(snapshot, workload).allowed:
        return False
    active_media = sum(
        bool(set(lease.get("keys") or ()) & set(MEDIA_RESERVATION_KEYS))
        for lease in leases.get("active", ())
    )
    if workload == "image_derive":
        if active_media >= _media_slot_limit():
            return False
    elif active_media:
        return False
    requested, capacity = media_reservation_details(snapshot, workload)
    return (
        int(leases.get("reserved_bytes") or 0) + requested <= capacity
        and media_local_slot_available(workload)
    )


def media_capacity_signature(snapshot: dict) -> tuple[int, int]:
    """Publish admissible profile changes using only the shared snapshot."""
    from app.services.resource_pressure import profile_slice_limits

    try:
        capacity = max(0, int(snapshot['memory']['available_bytes'])
                       - int(snapshot['controller']['hard_limits']['memory_available_bytes'])
                       - int(((snapshot.get('budget') or {}).get('reservation') or {}).get('reserved_bytes') or 0))
    except (KeyError, TypeError, ValueError):
        return (0, 0)
    image_slots = (
        (
            min(
                _media_slot_limit(),
                capacity // max(1, media_memory_bytes(snapshot, "image_derive")),
            )
            if _media_slot_limit() > 1
            else int(capacity >= media_memory_bytes(snapshot, "image_derive"))
        )
        if profile_slice_limits(snapshot, "image_derive").allowed
        else 0
    )
    video_slots = (
        int(capacity >= media_memory_bytes(snapshot, "video_derive"))
        if profile_slice_limits(snapshot, "video_derive").allowed
        else 0
    )
    return image_slots, video_slots


def media_local_slot_available(workload: str) -> bool:
    from app.services.heavy_io import (
        MEDIA_RESERVATION_KEYS,
        local_lock_for_workload,
        parallel_media_locks,
    )

    if workload != "image_derive":
        lock = local_lock_for_workload(workload)
        try:
            return lock is None or lock.try_acquire()
        finally:
            if lock is not None:
                lock.release()

    occupied = 0
    available = 0
    for slot in range(len(MEDIA_RESERVATION_KEYS)):
        locks = parallel_media_locks(slot)
        try:
            if locks.try_acquire():
                available += 1
            else:
                occupied += 1
        finally:
            locks.release()
    return available > 0 and occupied < _media_slot_limit()
