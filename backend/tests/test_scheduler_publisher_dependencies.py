"""Task publishing preserves real RQ identities without importing handlers."""
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.integration
def test_scheduler_publishers_preserve_rq_jobs_without_loading_handlers():
    source = textwrap.dedent("""
        import importlib.abc
        import inspect
        import os
        import sys
        import uuid
        from datetime import UTC, datetime
        from redis import Redis
        from rq.job import Job
        from rq import Queue as RQQueue

        connection = Redis.from_url(os.environ['REDIS_URL'])
        assert connection.connection_pool.connection_kwargs['db'] == 15
        prefix = 't' + uuid.uuid4().hex[:30]
        class RejectTaskImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                if fullname == 'app.jobs' or fullname.startswith('app.jobs.'):
                    raise AssertionError('Publisher imported task handlers: ' + fullname)

        finder = RejectTaskImports()
        sys.meta_path.insert(0, finder)
        from app.services.scheduler_loop import ensure_next_subscription_scan
        from app.services.pixiv_ranking_scheduler import ensure_pixiv_ranking_sync, schedule_pixiv_heat_expiry
        from app.services.work_heat_queue import request_work_heat_recompute
        from app.services import scheduler_loop, pixiv_ranking_scheduler, work_heat_queue
        scheduler_loop.SCHEDULER_QUEUE = prefix + '-scheduled'
        scheduler_loop.SCHEDULER_JOB_PREFIX = prefix + '-subscription-'
        scheduler_loop.SCHEDULER_STATE_KEY = prefix + ':state'
        scheduler_loop.SCHEDULER_ENSURE_LOCK_KEY = prefix + ':lock'
        ranking_id = pixiv_ranking_scheduler.pixiv_ranking_job_id
        expiry_id = pixiv_ranking_scheduler.pixiv_heat_expiry_job_id
        pixiv_ranking_scheduler.pixiv_ranking_job_id = lambda day: prefix + '-' + ranking_id(day)
        pixiv_ranking_scheduler.pixiv_heat_expiry_job_id = lambda at: prefix + '-' + expiry_id(at)
        def isolated_queue(*, name, connection):
            assert name in {'scheduled', 'maintenance'}
            return RQQueue(name=prefix + '-' + name, connection=connection)
        pixiv_ranking_scheduler.Queue = isolated_queue
        work_heat_queue.Queue = isolated_queue
        now = datetime(2030, 1, 2, 4, tzinfo=UTC)
        scan = ensure_next_subscription_scan(60, redis_client=connection)
        ranking = ensure_pixiv_ranking_sync(now=now, redis_client=connection)
        expiry = schedule_pixiv_heat_expiry(now, now=now, redis_client=connection)
        heat = request_work_heat_recompute({prefix}, redis_client=connection)
        assert heat['created'] == 1
        expected = [
            (scan['job_id'], 'app.jobs.subscription_sync.sync_subscriptions', prefix + '-scheduled', ()),
            (ranking['job_id'], 'app.jobs.pixiv_ranking_sync.sync_pixiv_rankings', prefix + '-scheduled', ('2030-01-01',)),
            (expiry['job_id'], 'app.jobs.work_heat.expire_pixiv_heat', prefix + '-scheduled', ()),
            ('work-heat-' + prefix, 'app.jobs.work_heat.run_work_heat_recompute', prefix + '-maintenance', (prefix,)),
        ]
        jobs = []
        for identity, function, queue, args in expected:
            job = Job.fetch(identity, connection=connection)
            assert job.func_name == function, (identity, job.func_name)
            assert job.origin == queue
            assert job.args == args
            jobs.append(job)
        assert jobs[1].retries_left == 3
        assert jobs[2].retries_left == 3
        assert jobs[3].retries_left == 3
        repeated = ensure_next_subscription_scan(60, redis_client=connection)
        assert repeated['created'] is False and repeated['job_id'] == scan['job_id']
        sys.meta_path.remove(finder)
        for job in jobs:
            inspect.signature(job.func).bind(*job.args, **job.kwargs)
        for job in jobs:
            job.delete()
        for name in ['scheduled', 'maintenance']:
            RQQueue(name=prefix + '-' + name, connection=connection).delete()
        keys = list(connection.scan_iter(match='*' + prefix + '*'))
        if keys:
            connection.delete(*keys)
        connection.close()
    """)
    result = subprocess.run([sys.executable, '-c', source], env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
