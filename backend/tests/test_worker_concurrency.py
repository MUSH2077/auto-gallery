from worker_entrypoint import resolve_concurrency


def test_prefers_db_value_over_argv():
    assert resolve_concurrency(4, "3") == 4


def test_clamps_above_max():
    assert resolve_concurrency(9, "3") == 5


def test_clamps_below_min():
    assert resolve_concurrency(0, "3") == 1
    assert resolve_concurrency(-2, None) == 1


def test_falls_back_to_argv_when_db_missing():
    assert resolve_concurrency(None, "2") == 2


def test_falls_back_to_default_when_both_invalid():
    assert resolve_concurrency(None, None) == 3
    assert resolve_concurrency("x", "y") == 3
