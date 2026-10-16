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
    from app import database
    monkeypatch.setattr(database, "async_session", Mock(side_effect=OSError("database unavailable")))
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


def test_database_timeout_cancels_query_and_disposes_engine(monkeypatch, capsys):
    import asyncio
    from unittest.mock import AsyncMock
    from app import database
    from app.services import settings as settings_service

    cancelled = []
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    async def blocked(_db):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    dispose = AsyncMock()
    monkeypatch.setattr(startup, "DB_READ_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(database, "async_session", Session)
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(settings_service, "get_download_defaults", blocked)
    assert startup._db_download_concurrency() is None
    assert cancelled == [True]
    dispose.assert_awaited_once()
    assert "TimeoutError" in capsys.readouterr().err
