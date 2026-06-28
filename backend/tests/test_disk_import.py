def test_reconcile_downloads_to_db_is_importable():
    # Contract smoke test: the service module exposes the entrypoint with the
    # documented signature. Behavioural coverage is via integration runs.
    from app.services.disk_import import reconcile_downloads_to_db
    import inspect

    sig = inspect.signature(reconcile_downloads_to_db)
    assert list(sig.parameters)[:2] == ["db", "options"]
