import asyncio
from types import SimpleNamespace

from app.services.tasks import TaskService


class FakeDB:
    async def flush(self):
        return None


def test_task_update_can_explicitly_clear_error_and_result():
    task = SimpleNamespace(
        status="complete",
        result_data={"code": "no_changes"},
        error_log="old error",
        updated_at=None,
    )
    asyncio.run(TaskService(FakeDB()).update_task(task, result=None, error=None))
    assert task.result_data is None
    assert task.error_log is None
