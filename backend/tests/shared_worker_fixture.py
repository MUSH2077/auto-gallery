"""Controlled inputs around real RQ admission and forked task execution."""
import asyncio
import json
import os
import time
from app.services.resource_aware_worker import ResourceAwareWorker

CONSUMER_PID = None

async def backend_pid():
    from sqlalchemy import text
    from app.database import async_session, engine
    try:
        async with async_session() as session:
            return await session.scalar(text('select pg_backend_pid()'))
    finally:
        await engine.dispose()

class SharedTestWorker(ResourceAwareWorker):
    def __init__(self,*args,**kwargs):
        from app.services import resource_aware_worker as module
        connection=kwargs['connection'];namespace=os.environ['P5_NAMESPACE']
        module.get_resource_pressure_snapshot_sync=lambda **_: {
            'status':'paused' if connection.get(namespace+':pressure') else 'normal',
            'reasons':['test_pressure'] if connection.get(namespace+':pressure') else [],
            'memory':{'available_bytes':1024**3},
            'controller':{'hard_limits':{'memory_available_bytes':128*1024**2}}}
        super().__init__(*args,**kwargs)

    def _publish_pressure_state(self,snapshot):
        return {'hard_gate_active':False,'soft_scale':1.0,'cgroup_deltas':{}}

    def bootstrap(self,*args,**kwargs):
        global CONSUMER_PID
        CONSUMER_PID=os.getpid()
        from app.config import settings
        assert settings.media_worker_concurrency == 4
        assert settings.import_worker_concurrency == 2
        self.connection.hset(os.environ['P5_NAMESPACE']+':consumers',str(os.getpid()),json.dumps({
            'redis_id':self.connection.client_id(),'backend_pid':asyncio.run(backend_pid())}))
        return super().bootstrap(*args,**kwargs)


def isolated_task(namespace,mode='normal'):
    from rq import get_current_job
    global CONSUMER_PID
    connection=get_current_job().connection
    count=connection.incr(namespace+':attempt:'+get_current_job().id)
    if mode=='retry' and count==1:
        raise ValueError('Owned retry test')
    original=CONSUMER_PID
    CONSUMER_PID='only this workhorse changed'
    record={'pid':os.getpid(),'parent':os.getppid(),'marker':original,
        'redis_id':connection.client_id(),'backend_pid':asyncio.run(backend_pid())}
    connection.hset(namespace+':started',get_current_job().id,json.dumps(record))
    if mode in ('held','kill'):
        deadline=time.monotonic()+20
        while not connection.get(namespace+':release'):
            if time.monotonic()>deadline:raise TimeoutError('Owned barrier not released')
            time.sleep(.02)
    return record
