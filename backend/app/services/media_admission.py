"""Memory and lock requirements shared by media selection and recovery."""
from __future__ import annotations


def media_memory_bytes(snapshot: dict, workload: str) -> int:
    from app.services.resource_pressure import RESOURCE_PROFILES

    profile = ((snapshot.get('budget') or {}).get('profiles') or {}).get(workload) or {}
    base = RESOURCE_PROFILES[workload].memory_reservation_bytes
    try:
        return max(0, int(profile.get('memory_reservation_bytes', base)))
    except (ValueError, TypeError):
        return int(base)


def media_reservation_details(snapshot: dict, workload: str) -> tuple[int, int]:
    from app.services.heavy_io import resource_reservation_details

    return resource_reservation_details(snapshot, workload)


def media_profile_admissible(snapshot: dict, workload: str, leases: dict) -> bool:
    from app.services.resource_pressure import profile_slice_limits

    if leases.get('error') or not profile_slice_limits(snapshot, workload).allowed:
        return False
    requested, capacity = media_reservation_details(snapshot, workload)
    return (int(leases.get('reserved_bytes') or 0) + requested <= capacity
            and media_local_slot_available(workload))


def media_capacity_signature(snapshot: dict) -> tuple[int, int]:
    """Publish admissible profile changes using only the shared snapshot."""
    from app.services.resource_pressure import profile_slice_limits

    try:
        capacity = max(0, int(snapshot['memory']['available_bytes'])
                       - int(snapshot['controller']['hard_limits']['memory_available_bytes'])
                       - int(((snapshot.get('budget') or {}).get('reservation') or {}).get('reserved_bytes') or 0))
    except (KeyError, TypeError, ValueError):
        return (0, 0)
    return tuple(
        int(capacity >= media_memory_bytes(snapshot, workload))
        if profile_slice_limits(snapshot, workload).allowed else 0
        for workload in ('image_derive', 'video_derive')
    )


def media_local_slot_available(workload: str) -> bool:
    from app.services.heavy_io import local_lock_for_workload

    lock = local_lock_for_workload(workload)
    try:
        return lock is None or lock.try_acquire()
    finally:
        if lock is not None:
            lock.release()
