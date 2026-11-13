"""Supervisor imports stay lean while RQ workhorses retain state publication."""
import json
import subprocess
import sys
import textwrap
import pytest

@pytest.mark.parametrize(('queue','count','extra','expected'),[
    ('operations',4,'',4),('imports',2,'maintenance',3),('discovery',1,'',1),
])
def test_supervisor_can_start_and_stop_without_task_or_orm_imports(queue,count,extra,expected):
    source=textwrap.dedent("""
        import contextlib,importlib.abc,io,json,os,signal,sys
        from unittest.mock import patch
        forbidden=[]
        class RejectBusinessImports(importlib.abc.MetaPathFinder):
            def find_spec(self,fullname,*args):
                if any(fullname==name or fullname.startswith(name+'.') for name in
                       ('sqlalchemy','app.models','app.database','app.services.tasks','app.services.heavy_io')):
                    forbidden.append(fullname);raise AssertionError(fullname)
        sys.meta_path.insert(0,RejectBusinessImports())
        import worker_entrypoint as entry
        handlers={};children=[]
        class Process:
            def __init__(self,command,env=None):
                self.pid=100+len(children);self.returncode=None;self.command=command;children.append(self)
            def poll(self):return self.returncode
            def terminate(self):self.returncode=0
            def wait(self,timeout=None):return self.returncode
            def kill(self):raise AssertionError('graceful stop should suffice')
        def stop(_):handlers[signal.SIGTERM](signal.SIGTERM,None)
        queue,count,extra,expected=sys.argv[1:];expected=int(expected)
        os.environ['WORKER_EXTRA_QUEUES']=extra
        sys.argv=['worker_entrypoint.py',queue,count,'--with-scheduler']
        with patch.object(entry.subprocess,'Popen',Process), \
             patch.object(entry.signal,'signal',lambda n,fn:handlers.update({n:fn})), \
             patch.object(entry.time,'sleep',stop), \
             patch.object(entry,'_publish_supervisor_status'),contextlib.redirect_stdout(io.StringIO()):
            entry.main()
        assert len(children)==expected
        assert all(child.returncode==0 for child in children)
        assert forbidden==[],forbidden
        print(json.dumps({'children':len(children),'forbidden':forbidden}))
    """)
    result=subprocess.run([sys.executable,'-c',source,queue,str(count),extra,str(expected)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['children']==expected


def test_actual_rq_initialization_registers_callback_inherited_by_workhorse():
    # A fresh interpreter models exec(rq) followed by RQ's fork, independently
    # of any supervisor callback or imports in the pytest interpreter.
    source=textwrap.dedent("""
        import asyncio,json,os
        from unittest.mock import patch
        from uuid import uuid4
        from app.database import async_session,engine
        from app.models import TaskRun
        from app.services.tasks import TaskService,update_task_resource_state
        from app.services import heavy_io
        from app.services.resource_aware_worker import ResourceAwareWorker
        task_id=uuid4()
        async def prepare():
            async with async_session() as db:
                await TaskService(db).create_task(task_id=task_id,kind='admin',operation_type='memory-test',title='callback fork',status='enqueued',queue_name='operations',meta={})
                await db.commit()
            await engine.dispose()
        asyncio.run(prepare())
        heavy_io.register_resource_state_callback(None)
        with patch('rq.Worker.__init__'), patch('rq.Worker.bootstrap'):
            worker=object.__new__(ResourceAwareWorker)
            ResourceAwareWorker.__init__(worker)
            worker.bootstrap()
        assert heavy_io._resource_state_callback is update_task_resource_state
        child=os.fork()
        if child==0:
            async def publish():
                await heavy_io.set_resource_state(str(task_id),'waiting','memory_pressure',workload='image_derive')
                await engine.dispose()
            try:asyncio.run(publish());os._exit(0)
            except BaseException:os._exit(1)
        _,status=os.waitpid(child,0)
        assert status==0,status
        async def verify():
            async with async_session() as db:
                task=await db.get(TaskRun,task_id)
                assert task.resource_state=='waiting'
                assert task.resource_reason=='memory_pressure'
                await db.delete(task);await db.commit()
            await engine.dispose()
        asyncio.run(verify())
        print(json.dumps({'callback_in_child':True,'persisted_reason':'memory_pressure'}))
    """)
    result=subprocess.run([sys.executable,'-c',source],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['callback_in_child'] is True
