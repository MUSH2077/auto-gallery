import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def test_worker_bootstrap_prepares_forks_without_parent_db_connections():
    # A fresh interpreter retains the real cold mapper state; only RQ's
    # Redis bookkeeping is replaced for this isolated bootstrap/fork check.
    source = textwrap.dedent("""
        import asyncio,json,os
        from unittest.mock import patch
        from sqlalchemy import event,select,func
        from sqlalchemy.orm import Mapper
        from app.database import engine,async_session
        from app.models import Asset
        from app.services.resource_aware_worker import ResourceAwareWorker

        configured=[]
        connections=[]
        event.listen(Mapper,'before_configured',lambda:configured.append(True))
        event.listen(engine.sync_engine,'do_connect',lambda *args:connections.append(True))
        worker=object.__new__(ResourceAwareWorker)
        with patch('rq.Worker.bootstrap'):
            worker.bootstrap()
        assert len(configured)==1
        assert connections==[]
        results=[]
        for _ in range(2):
            read_fd,write_fd=os.pipe()
            child=os.fork()
            if child==0:
                os.close(read_fd)
                async def check():
                    async with async_session() as db:
                        await db.execute(select(Asset.id).limit(0))
                        backend_pid=await db.scalar(select(func.pg_backend_pid()))
                    await engine.dispose()
                    return {'backend_pid':backend_pid,'configured':len(configured),
                            'connections':len(connections)}
                try:
                    value=asyncio.run(check())
                    os.write(write_fd,json.dumps(value).encode())
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            os.close(write_fd)
            data=os.read(read_fd,4096);os.close(read_fd)
            _,status=os.waitpid(child,0)
            assert status==0
            results.append(json.loads(data))
        assert all(r['configured']==1 and r['connections']==1 for r in results)
        assert results[0]['backend_pid']!=results[1]['backend_pid']
        assert connections==[]
        print(json.dumps(results))
    """)
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 2


def test_scheduler_burst_releases_lock_when_promotion_fails(monkeypatch):
    from app.services import resource_aware_scheduler
    from app.services.resource_aware_worker import ResourceAwareWorker

    calls = []
    class Scheduler:
        def __init__(self, queues, **kwargs):
            self.acquired_locks = {"operations"}
            assert kwargs["logging_level"] == 20
        def acquire_locks(self): calls.append("acquire")
        def register_birth(self): calls.append("birth")
        def enqueue_scheduled_jobs(self):
            calls.append("promote")
            raise RuntimeError("promotion unavailable")
        def release_locks(self): calls.append("release")
        def register_death(self): calls.append("death")

    monkeypatch.setattr(resource_aware_scheduler, "ResourceAwareScheduler", Scheduler)
    worker = SimpleNamespace(queues=["operations"], connection=object(), serializer=None,
                             log=SimpleNamespace(level=20))
    with pytest.raises(RuntimeError, match="promotion unavailable"):
        ResourceAwareWorker._start_scheduler(worker, burst=True, logging_level=None)
    assert calls == ["acquire", "birth", "promote", "release", "death"]


@pytest.mark.asyncio
async def test_commit_payload_retains_response_schema_with_deferred_loading(monkeypatch):
    from app.services.curation import CurationService

    stamp = datetime.now(timezone.utc)
    commit = SimpleNamespace(id=uuid4(), parent_commit_id=None, actor_type="system", actor_id=None,
        message="Group equivalent assets", trigger="asset-dedup", dedupe_key=None,
        occurred_at=stamp, reverts_commit_id=None, status="active", stats={"changes":1},
        extra_metadata={"origin":"dedup"}, created_at=stamp, updated_at=stamp)
    service = CurationService(SimpleNamespace(get=AsyncMock(return_value=commit)))
    monkeypatch.setattr(service, "_changes_for_commit", AsyncMock(return_value=[]))
    payload = await service.commit_payload(commit.id)
    from app.schemas.curation import CurationCommitRead
    assert isinstance(payload, CurationCommitRead)
    assert payload.model_dump(mode="json")["id"] == str(commit.id)
    assert payload.metadata == {"origin":"dedup"}
    assert payload.revertible is True


@pytest.mark.asyncio
async def test_obsolete_scan_failure_keeps_attempt_fence(monkeypatch):
    from app.jobs import asset_dedup
    from app.services.operations import AdminOperationAttemptRejected

    def unexpected_session():
        raise AssertionError("Obsolete scan must not change task state")
    monkeypatch.setattr(asset_dedup, "async_session", unexpected_session)
    assert await asset_dedup._fail_scan_operation(uuid4(), "obsolete-scan", AdminOperationAttemptRejected("rotated")) is None


def test_worker_preload_freeze_preserves_collection_of_new_task_objects():
    source = textwrap.dedent("""
        import gc,json,os,weakref
        from unittest.mock import patch
        from app.services.resource_aware_worker import ResourceAwareWorker

        gc.enable()
        worker=object.__new__(ResourceAwareWorker)
        with patch('rq.Worker.__init__'):
            worker.__init__()
        frozen_before=gc.get_freeze_count()
        with patch('rq.Worker.bootstrap'):
            worker.bootstrap()
        frozen=gc.get_freeze_count()
        assert frozen>frozen_before
        assert gc.isenabled()
        class TaskObject:
            pass
        def collect_task_cycle():
            value=TaskObject();value.self=value
            reference=weakref.ref(value)
            del value
            gc.collect()
            assert reference() is None
            assert gc.isenabled()
            assert 0 < gc.get_freeze_count() <= frozen
        collect_task_cycle()
        read_fd,write_fd=os.pipe()
        child=os.fork()
        if child==0:
            os.close(read_fd)
            try:
                for _ in range(3):collect_task_cycle()
                os.write(write_fd,json.dumps({'collected':True}).encode())
                os._exit(0)
            except BaseException:os._exit(1)
        os.close(write_fd)
        data=os.read(read_fd,4096);os.close(read_fd)
        _,status=os.waitpid(child,0)
        assert status==0
        for _ in range(3):collect_task_cycle()
        print(json.dumps({'child':json.loads(data),'parent_collected':True}))
    """)
    result=subprocess.run([sys.executable,"-c",source],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'child':{'collected':True},'parent_collected':True}
