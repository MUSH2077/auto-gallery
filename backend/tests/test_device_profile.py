from app.services.device_profile import GIB, classify_device_profile, resolve_device_profile


def test_device_profile_capability_boundaries():
    assert classify_device_profile(8 * GIB, 8) == "compact"
    assert classify_device_profile(16 * GIB, 8) == "standard"
    assert classify_device_profile(32 * GIB, 8) == "performance"
    assert classify_device_profile(32 * GIB, 4) == "compact"


def test_device_profile_unknown_capability_falls_back_to_compact():
    assert classify_device_profile(None, 8) == "compact"
    assert classify_device_profile(16 * GIB, None) == "compact"


def test_device_profile_override_is_deterministic():
    profile = resolve_device_profile(
        "performance",
        memory_total_bytes=8 * GIB,
        cpu_count=2,
    )
    assert profile.name == "performance"
    assert profile.source == "configured"
    assert profile.scheduler_publish_limit == 50
    assert profile.download_queue_limit == 200
    assert profile.download_concurrency_limit == 2


def test_standard_profile_matches_sixteen_gib_nas_envelope():
    profile = resolve_device_profile(
        "auto",
        memory_total_bytes=16 * GIB,
        cpu_count=8,
    )
    assert profile.name == "standard"
    assert profile.scheduler_publish_limit == 25
    assert profile.download_queue_limit == 100
    assert profile.download_concurrency_limit == 1
