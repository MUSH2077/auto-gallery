import json
import signal
import subprocess
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
import worker_entrypoint as entry
from app.services import download_startup as startup


@pytest.mark.parametrize("db_value,argv,expected", [(4, "2", 4), (None, "2", 2), (None, None, 3)])
def test_startup_keeps_db_cli_default_precedence(monkeypatch, db_value, argv, expected):
    monkeypatch.setattr(entry, "_sweep_personal_auth_startup", lambda _: 2)
    monkeypatch.setattr(startup, "_db_download_concurrency", lambda: db_value)
    monkeypatch.setenv("DOWNLOAD_CONCURRENCY_CAP", "5")
    monkeypatch.setenv("DOWNLOAD_DEVICE_CONCURRENCY_LIMIT", "5")
    result = startup.resolve_startup(argv)
    assert result == {"removed_auth_configs": 2, "configured": expected, "cap": 5, "effective": expected}


def test_credential_failure_never_reads_db_or_returns_fallback(monkeypatch):
    def fail(_):
        raise RuntimeError("personal authentication root must be mounted as tmpfs")
    read = Mock()
    monkeypatch.setattr(entry, "_sweep_personal_auth_startup", fail)
    monkeypatch.setattr(startup, "_db_download_concurrency", read)
    with pytest.raises(RuntimeError, match="tmpfs"):
        startup.resolve_startup("2")
    read.assert_not_called()


def test_db_read_failure_retains_fallback(monkeypatch):
    import asyncpg
    from unittest.mock import AsyncMock

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=OSError("database unavailable")))
    assert startup._db_download_concurrency() is None


@pytest.mark.parametrize("outcome", ["success", "failed", "bad_json", "timeout", "stop"])
def test_parent_reaps_startup_helper_and_restores_signal_handlers(monkeypatch, outcome):
    old = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    class Child:
        returncode = None
        killed = False
        def communicate(self, timeout=None):
            if timeout is not None:
                if outcome == "timeout":
                    raise subprocess.TimeoutExpired("startup", timeout)
                if outcome == "stop":
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.returncode = -9 if self.killed else (1 if outcome == "failed" else 0)
            return ("invalid" if outcome == "bad_json" else json.dumps({"configured": 2}), "")
        def poll(self):
            return self.returncode
        def kill(self):
            self.killed = True
    child = Child()
    spawn = Mock(return_value=child)
    monkeypatch.setattr(entry.subprocess, "Popen", spawn)
    if outcome == "success":
        assert entry._download_startup("2") == {"configured": 2}
    else:
        error = {"failed": RuntimeError, "bad_json": ValueError, "timeout": subprocess.TimeoutExpired, "stop": SystemExit}[outcome]
        with pytest.raises(error):
            entry._download_startup("2")
    assert child.poll() is not None
    assert child.killed == (outcome in ("timeout", "stop"))
    assert {sig: signal.getsignal(sig) for sig in old} == old
    assert spawn.call_args.args[0][-1] == "2"


@pytest.mark.parametrize("invalid_storage", [False, True])
def test_download_supervisor_runs_real_helper_without_loading_orm(invalid_storage):
    import sys
    import textwrap
    code = textwrap.dedent("""
        import contextlib,importlib.abc,io,json,os,signal,subprocess,sys
        from pathlib import Path
        from unittest.mock import patch
        invalid=sys.argv[1]=='true'
        if invalid:
            os.environ['PERSONAL_AUTH_TMP_ROOT']='/app'
        else:
            root=Path(os.environ['PERSONAL_AUTH_TMP_ROOT'])
            root.mkdir(parents=True,exist_ok=True)
            (root/'auth-999999999-1-stale.json').write_text('{}')
        os.environ['DOWNLOAD_CONCURRENCY_CAP']='2'
        os.environ['DOWNLOAD_DEVICE_CONCURRENCY_LIMIT']='2'
        os.environ['WORKER_EXTRA_QUEUES']=''
        forbidden=[]
        class RejectBusinessImports(importlib.abc.MetaPathFinder):
            def find_spec(self,fullname,*args):
                if any(fullname==name or fullname.startswith(name+'.') for name in
                       ('sqlalchemy','app.models','app.database','app.config')):
                    forbidden.append(fullname);raise AssertionError(fullname)
        sys.meta_path.insert(0,RejectBusinessImports())
        import worker_entrypoint as entry
        real_popen=subprocess.Popen
        workers=[];handlers={}
        class Worker:
            def __init__(self,command,env=None):
                self.pid=100+len(workers);self.returncode=None
                assert env['DOWNLOAD_EXECUTION_CONCURRENCY']=='2'
                workers.append(self)
            def poll(self):return self.returncode
            def terminate(self):self.returncode=0
            def wait(self,timeout=None):return self.returncode
            def kill(self):raise AssertionError('Expected graceful stop')
        def spawn(command,**kwargs):
            return real_popen(command,**kwargs) if command[0]==sys.executable else Worker(command,**kwargs)
        def stop(_):handlers[signal.SIGTERM](signal.SIGTERM,None)
        def handler(sig,fn):
            old=handlers.get(sig);handlers[sig]=fn;return old
        sys.argv=['worker_entrypoint.py','downloads,downloads:pixiv','2','--with-scheduler']
        with patch.object(entry.subprocess,'Popen',spawn), \
             patch.object(entry.signal,'signal',handler), \
             patch.object(entry.time,'sleep',stop), \
             patch.object(entry,'_publish_supervisor_status'), \
             contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            if invalid:
                try:entry.main()
                except SystemExit as exc:assert exc.code==1
                else:raise AssertionError('Unsafe credential storage started workers')
            else:entry.main()
        assert forbidden==[]
        assert len(workers)==(0 if invalid else 2)
        assert all(w.returncode==0 for w in workers)
        print(json.dumps({'workers':len(workers),'no_parent_orm':True}))
    """)
    result = subprocess.run([sys.executable, "-c", code, str(invalid_storage).lower()], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["workers"] == (0 if invalid_storage else 2)


def test_database_timeout_cancels_query_and_closes_connection(monkeypatch, capsys):
    import asyncio
    import asyncpg
    from unittest.mock import AsyncMock

    cancelled = []
    async def blocked(*_args):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    connection = SimpleNamespace(fetchval=blocked, close=AsyncMock())
    monkeypatch.setattr(startup, "DB_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
    assert startup._db_download_concurrency() is None
    assert cancelled == [True]
    connection.close.assert_awaited_once_with(timeout=0.01)
    assert "TimeoutError" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.parametrize("value,expected", [
    (None, 3), ({}, 3), ([], 3), ({"download_concurrency": 4}, 4),
    ({"download_concurrency": "2"}, 2), ({"download_concurrency": None}, None),
    ({"download_concurrency": "bad"}, None), ({"download_concurrency": 0}, 0),
])
def test_startup_reads_real_postgres_and_preserves_missing_setting_default(value, expected):
    import asyncio
    import asyncpg
    from app.config import settings

    url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    assert url.split("?", 1)[0].endswith("_test")
    async def replace_setting(new_value, *, delete=False):
        connection = await asyncpg.connect(url)
        try:
            previous = await connection.fetchval(
                "SELECT value FROM system_settings WHERE key = 'download_defaults'"
            )
            if delete:
                await connection.execute("DELETE FROM system_settings WHERE key = 'download_defaults'")
            else:
                await connection.execute(
                    "INSERT INTO system_settings(key,value) VALUES ('download_defaults',$1::jsonb) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", new_value,
                )
            return previous
        finally:
            await connection.close()
    previous = asyncio.run(replace_setting(json.dumps(value), delete=value is None))
    try:
        assert startup._db_download_concurrency() == expected
    finally:
        asyncio.run(replace_setting(previous, delete=previous is None))


def test_first_startup_helper_reads_postgres_without_orm_imports():
    import os
    import sys
    import textwrap

    code = textwrap.dedent("""
        import importlib.abc,json,sys
        class RejectOrm(importlib.abc.MetaPathFinder):
            def find_spec(self,name,*args):
                if any(name==prefix or name.startswith(prefix+'.') for prefix in
                       ('sqlalchemy','app.models','app.database','app.services.settings')):
                    raise AssertionError(name)
        sys.meta_path.insert(0,RejectOrm())
        from app.services.download_startup import resolve_startup
        print(json.dumps(resolve_startup('2')))
    """)
    result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["effective"] >= 1
    assert "DB concurrency read failed" not in result.stderr
