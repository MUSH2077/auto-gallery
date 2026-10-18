"""Real forkserver/RQ lifecycle checks in the isolated test Redis database."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from uuid import uuid4

import pytest


def inspect_preload(connection):
    import gc
    import threading
    from app.services import worker_preload

    sockets = []
    for fd in Path("/proc/self/fd").iterdir():
        try:
            if os.readlink(fd).startswith("socket:"):
                sockets.append(fd.name)
        except FileNotFoundError:
            pass
    # forkserver passes a control socket to each child. Inspect the template
    # itself separately, rather than mistaking that socket for a DB client.
    template_fds = []
    for fd in Path(f"/proc/{worker_preload.PRELOAD_PID}/fd").iterdir():
        try:
            template_fds.append(os.readlink(fd))
        except FileNotFoundError:
            pass
    connection.send({"template":worker_preload.PRELOAD_PID,"pid":os.getpid(),
        "threads":len(list(Path(f"/proc/{worker_preload.PRELOAD_PID}/task").iterdir())),
        "gc":gc.isenabled(),"frozen":gc.get_freeze_count(),
        "database_imported":"app.database" in sys.modules,
        "config_imported":"app.config" in sys.modules,
        "control_imported":"app.services.worker_control_preload" in sys.modules,
        "resource_callback_registered":bool(getattr(sys.modules.get("app.services.heavy_io"),"_resource_state_callback",None)),
        "lease_client_created":getattr(sys.modules.get("app.services.heavy_io"),"_lease_redis_client",None) is not None,
        "template_sockets":sum(p.startswith("socket:") for p in template_fds)})
    connection.close()


@pytest.mark.parametrize("preload_control", [False, True])
def test_template_is_inherited_and_contains_no_business_connections_or_threads(preload_control):
    code = textwrap.dedent("""
        import json,multiprocessing,os
        from tests.test_shared_worker_process import inspect_preload
        if __name__=='__main__':
            control=os.environ['P5_PRELOAD_CONTROL']=='1'
            modules=['app.services.worker_preload']
            if control:modules.append('app.services.worker_control_preload')
            multiprocessing.set_forkserver_preload(modules)
            context=multiprocessing.get_context('forkserver')
            rows=[]
            for _ in range(2):
                read,write=context.Pipe(False)
                process=context.Process(target=inspect_preload,args=(write,))
                process.start();write.close()
                assert read.poll(15)
                rows.append(read.recv());read.close();process.join(10)
                assert process.exitcode==0
            assert rows[0]['template']==rows[1]['template']
            assert all(row['template']!=row['pid'] and row['threads']==1 and row['gc'] and row['frozen']>0 for row in rows)
            assert all(not row['database_imported'] and row['config_imported']==control and row['control_imported']==control for row in rows)
            assert all(not row['resource_callback_registered'] and not row['lease_client_created'] for row in rows)
            assert all(row['template_sockets']==1 for row in rows) # forkserver listener only
            print(json.dumps(rows))
    """)
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'P5_PRELOAD_CONTROL':str(int(preload_control))},capture_output=True,text=True,timeout=40)
    assert result.returncode==0,result.stderr
    assert len(json.loads(result.stdout))==2


def test_shared_handles_track_actual_consumers_after_template_crash():
    code = textwrap.dedent("""
        import json,os,signal,time
        import redis
        from rq import Worker
        from app.services.shared_worker_process import SharedWorkerFactory
        if __name__=='__main__':
            factory=SharedWorkerFactory();connection=redis.Redis.from_url(os.environ['REDIS_URL'])
            handles=[]
            try:
                for _ in range(2):
                    handles.append(factory.start(['rq','worker','-w','app.services.resource_aware_worker.ResourceAwareWorker','--url',os.environ['REDIS_URL'],'p5-isolation-'+os.environ['P5_NAMESPACE']]))
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    workers=[w for w in Worker.all(connection=connection) if w.pid in [h.pid for h in handles]]
                    if len(workers)==2:break
                    time.sleep(.02)
                assert len(workers)==2
                assert all(h.poll() is None for h in handles)
                os.kill(factory.template_pid,signal.SIGKILL)
                deadline=time.monotonic()+5
                while factory.alive() and time.monotonic()<deadline:time.sleep(.02)
                assert not factory.alive()
                try:
                    factory.start(['rq','worker','unused'])
                    raise AssertionError('Must not create a second template generation')
                except RuntimeError:pass
                # Status-pipe failure must never hide still-running consumers.
                assert all(h.poll() is None for h in handles)
                for h in handles:h.terminate()
                for h in handles:h.wait(10)
                assert all(h.poll() is not None for h in handles)
                print('P5_HANDLE_CHECK_OK')
            finally:
                for h in handles:
                    if h.poll() is None:h.kill();h.wait(10)
                factory.close();connection.close()
    """)
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'P5_NAMESPACE':uuid4().hex},capture_output=True,text=True,timeout=50)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'P5_HANDLE_CHECK_OK' in result.stdout


@pytest.mark.parametrize(('queue','count','extra'),[('operations',4,''),('imports',2,'maintenance')])
@pytest.mark.parametrize('mode',['startup-stop','restart','restart-scheduler','restart-maintenance-scheduler','template-failure'])
def test_supervisor_restarts_consumers_and_stops_group_on_template_loss(tmp_path,mode,queue,count,extra):
    import redis
    from rq import Worker

    if mode=='restart-maintenance-scheduler' and not extra:
        pytest.skip('This service has no maintenance consumer')
    expected=count+bool(extra)
    connection=redis.Redis.from_url(os.environ['REDIS_URL'])
    initial={w.name for w in Worker.all(connection=connection)}
    log=(tmp_path/'supervisor.log').open('w+')
    process=subprocess.Popen([sys.executable,'worker_entrypoint.py',queue,str(count),'--with-scheduler'],
        env={**os.environ,'WORKER_SHARED_INITIALIZATION':'1','MEDIA_WORKER_CONCURRENCY':'4','WORKER_EXTRA_QUEUES':extra},
        stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    owned=[]
    def workers():
        return [w for w in Worker.all(connection=connection) if w.name not in initial and w.queues[0].name in {queue,extra}]
    def wait_for(predicate,timeout=25):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate():return
            if process.poll() is not None and mode!='template-failure':break
            time.sleep(.05)
        log.flush();pytest.fail('Lifecycle boundary failed:\n'+Path(log.name).read_text())
    try:
        if mode=='startup-stop':
            def template_started():
                children=Path(f'/proc/{process.pid}/task/{process.pid}/children')
                if not children.exists():return False
                return any('multiprocessing.forkserver' in Path(f'/proc/{pid}/cmdline').read_text() for pid in children.read_text().split() if Path(f'/proc/{pid}/cmdline').exists())
            wait_for(template_started)
            process.terminate();assert process.wait(20)==0
            return
        wait_for(lambda:len(workers())==expected)
        owned=workers();old_pids={w.pid for w in owned}
        parent_pid=int(Path(f'/proc/{next(iter(old_pids))}/stat').read_text().split(') ')[1].split()[1])
        if mode in {'restart', 'restart-scheduler', 'restart-maintenance-scheduler'}:
            def scheduler_children(pid):
                children=Path(f'/proc/{pid}/task/{pid}/children')
                return {int(v) for v in children.read_text().split()} if children.exists() else set()
            scheduler_queue='maintenance' if mode=='restart-maintenance-scheduler' else queue
            queue_pids={w.pid for w in owned if w.queues[0].name==scheduler_queue}
            wait_for(lambda:any(scheduler_children(pid) for pid in queue_pids))
            scheduler_owner=next(pid for pid in queue_pids if scheduler_children(pid))
            scheduler_pids=scheduler_children(scheduler_owner)
            victim=scheduler_owner if mode!='restart' else next(pid for pid in queue_pids if pid!=scheduler_owner)
            os.kill(victim,signal.SIGKILL)
            wait_for(lambda:len({w.pid for w in workers()}-old_pids)==1)
            # RQ's stale registration may persist until maintenance, but exactly
            # the configured live OS consumers must remain, with one replacement.
            live=[w for w in workers() if Path(f'/proc/{w.pid}').exists()]
            assert len(live)==expected
            assert sum(w.queues[0].name==queue for w in live)==count
            assert sum(w.queues[0].name=='maintenance' for w in live)==bool(extra)
            owned=workers();process.terminate()
            try:
                assert process.wait(20)==0
            except subprocess.TimeoutExpired:
                log.flush();pytest.fail('Restarted shared supervisor did not stop:\n'+Path(log.name).read_text())
            if mode in {'restart-scheduler','restart-maintenance-scheduler'}:
                wait_for(lambda:all(not Path(f'/proc/{pid}').exists() for pid in scheduler_pids),timeout=5)
        else:
            os.kill(parent_pid,signal.SIGKILL)
            assert process.wait(20)==1
        wait_for(lambda:all(not Path(f'/proc/{w.pid}').exists() for w in owned),timeout=5)
    finally:
        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(10)
        for worker in owned:
            connection.srem('rq:workers',worker.key);connection.delete(worker.key)
        log.close();connection.close()


@pytest.mark.parametrize('queues',[('operations',)*4,('imports','imports','maintenance')])
def test_shared_consumers_keep_connections_task_memory_pressure_and_retries_isolated(queues):
    code = textwrap.dedent("""
        import json,os,signal,time
        import redis
        from rq import Queue,Retry
        from datetime import timedelta
        from app.services.shared_worker_process import SharedWorkerFactory
        from app.services.resource_pressure import RESOURCE_CONTROL_CHANNEL
        from tests.shared_worker_fixture import isolated_task
        if __name__=='__main__':
            namespace=os.environ['P5_NAMESPACE'];connection=redis.Redis.from_url(os.environ['REDIS_URL'])
            queue_names=json.loads(os.environ['P5_QUEUES']);queue_map={name:Queue(name,connection=connection) for name in queue_names}
            factory=SharedWorkerFactory(environment={'P5_NAMESPACE':namespace,'MEDIA_WORKER_CONCURRENCY':'4','IMPORT_WORKER_CONCURRENCY':'2','WORKER_SHARED_INITIALIZATION':'1'},preload_control=queue_names[0]=='operations');handles=[];jobs=[]
            def wait_for(check):
                deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    if check():return
                    time.sleep(.02)
                raise AssertionError('Owned shared-worker lifecycle timed out')
            def enqueue(mode,profile='light',queue_name=None,delayed=False):
                queue=queue_map[queue_name or queue_names[0]]
                method=queue.enqueue_in if delayed else queue.enqueue
                args=(timedelta(seconds=1),isolated_task,namespace,mode) if delayed else (isolated_task,namespace,mode)
                job=method(*args,job_id=namespace+'-'+str(len(jobs)),meta={'resource_profile':profile},retry=Retry(max=1,interval=0))
                jobs.append(job);return job
            try:
                for i,name in enumerate(queue_names):
                    handles.append(factory.start(['rq','worker','-w','tests.shared_worker_fixture.SharedTestWorker','--url',os.environ['REDIS_URL'],*(['--with-scheduler'] if name not in queue_names[:i] else []),name]))
                wait_for(lambda:connection.hlen(namespace+':consumers')==len(queue_names))
                parents=[json.loads(v)for v in connection.hvals(namespace+':consumers')]
                assert len({v['redis_id']for v in parents})==len(queue_names)
                assert len({v['backend_pid']for v in parents})==len(queue_names)
                for name in queue_names:enqueue('held',queue_name=name)
                wait_for(lambda:connection.hlen(namespace+':started')==len(queue_names))
                rows=[json.loads(v)for v in connection.hvals(namespace+':started')]
                assert {v['parent']for v in rows}=={h.pid for h in handles}
                assert all(v['parent']==v['marker'] and v['pid']!=v['parent']for v in rows)
                assert len({v['backend_pid']for v in rows})==len(queue_names)
                connection.set(namespace+':release','1')
                wait_for(lambda:all(job.get_status(refresh=True)=='finished' for job in jobs))
                # Each queue retains its own scheduler, including maintenance.
                for name in queue_map:
                    delayed=enqueue('normal',queue_name=name,delayed=True)
                    wait_for(lambda:delayed.get_status(refresh=True)=='finished')
                connection.set(namespace+':pressure','1')
                pressured=enqueue('normal','import_db' if queue_names[0]=='imports' else 'image_derive')
                time.sleep(.3)
                assert not connection.hexists(namespace+':started',pressured.id)
                connection.delete(namespace+':pressure');connection.publish(RESOURCE_CONTROL_CHANNEL,'resume')
                wait_for(lambda:pressured.get_status(refresh=True)=='finished')
                retry=enqueue('retry');wait_for(lambda:retry.get_status(refresh=True)=='finished')
                assert int(connection.get(namespace+':attempt:'+retry.id))==2
                # A new workhorse must still inherit the original consumer marker.
                assert retry.return_value()['marker']==retry.return_value()['parent']
                connection.delete(namespace+':release')
                killed=enqueue('kill')
                wait_for(lambda:connection.hexists(namespace+':started',killed.id))
                pid=json.loads(connection.hget(namespace+':started',killed.id))['pid']
                os.kill(pid,signal.SIGKILL)
                connection.set(namespace+':release','1')
                wait_for(lambda:killed.get_status(refresh=True)=='finished')
                assert all(h.poll() is None for h in handles)
                print('P5_ISOLATION_RETRY_PRESSURE_OK')
            finally:
                connection.set(namespace+':release','1')
                for h in handles:h.terminate()
                for h in handles:
                    try:h.wait(10)
                    except Exception:h.kill();h.wait(10)
                for job in jobs:job.delete()
                keys=list(connection.scan_iter(match=namespace+'*'))
                if keys:connection.delete(*keys)
                factory.close();connection.close()
    """)
    result=subprocess.run([sys.executable,'-c',code],env={**os.environ,'P5_NAMESPACE':'p5-'+uuid4().hex,'P5_QUEUES':json.dumps(queues),'MEDIA_WORKER_CONCURRENCY':'1','IMPORT_WORKER_CONCURRENCY':'1'},capture_output=True,text=True,timeout=100)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'P5_ISOLATION_RETRY_PRESSURE_OK' in result.stdout


def test_scheduler_parent_loss_before_signal_installation_requests_rq_stop(monkeypatch):
    import ctypes
    from unittest.mock import Mock
    from app.services.shared_worker_process import follow_consumer_lifetime

    prctl=Mock(return_value=0)
    monkeypatch.setattr(ctypes, 'CDLL', Mock(return_value=type('Libc', (), {'prctl':prctl})()))
    monkeypatch.setattr(os, 'getppid', lambda: 1)
    sent=Mock()
    monkeypatch.setattr(os, 'kill', sent)
    follow_consumer_lifetime(999999999)
    prctl.assert_called_once_with(1, signal.SIGTERM, 0, 0, 0)
    sent.assert_called_once_with(os.getpid(), signal.SIGTERM)


def test_template_environment_is_restored_when_process_start_fails(monkeypatch):
    from types import SimpleNamespace
    from app.services.shared_worker_process import SharedWorkerFactory

    monkeypatch.setenv("MEDIA_WORKER_CONCURRENCY", "1")
    monkeypatch.delenv("P5_ONLY_IN_TEMPLATE", raising=False)
    factory = SharedWorkerFactory(environment={"MEDIA_WORKER_CONCURRENCY": "4", "P5_ONLY_IN_TEMPLATE": "yes"}, preload_control=True)
    def fail_start():
        assert os.environ["MEDIA_WORKER_CONCURRENCY"] == "4"
        assert os.environ["P5_ONLY_IN_TEMPLATE"] == "yes"
        raise OSError("Injected forkserver start failure")
    process = SimpleNamespace(start=fail_start, pid=None)
    monkeypatch.setattr(factory._context, "Process", lambda **_: process)
    with pytest.raises(OSError, match="Injected"):
        factory.start(["rq", "worker", "operations"])
    assert os.environ["MEDIA_WORKER_CONCURRENCY"] == "1"
    assert "P5_ONLY_IN_TEMPLATE" not in os.environ
    factory.close()
