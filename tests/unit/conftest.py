"""Unit-test conftest: auto-patch services that connect to external infra on init."""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _mock_minio_services():
    """Prevent RosbagDatabaseService and ModelDatabaseService from connecting to MinIO."""
    with patch('packages.topomap_dbs.client.RosbagDatabaseService') as mock_rosbag, \
         patch('packages.topomap_dbs.client.ModelDatabaseService') as mock_model:
        mock_rosbag.return_value = MagicMock()
        mock_model.return_value = MagicMock()
        yield


@pytest.fixture(autouse=True)
def _current_event_loop_for_sync_tests(request):
    """Sync tests that build a mission-dispatch `Robot` need a current event loop:
    Robot.__init__ schedules its run() task with asyncio.get_event_loop(). pytest-asyncio
    closes its loops and clears the current one after every async test, so in a full run a
    sync test that happens to follow an async one has none (RuntimeError on 3.12+) and
    passes only in isolation. Async tests run on pytest-asyncio's own loop; skip them."""
    import asyncio
    import inspect
    if inspect.iscoroutinefunction(getattr(request.function, "__wrapped__", request.function)) \
            or inspect.iscoroutinefunction(request.function):
        yield
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        if not loop.is_closed():   # a test (e.g. starlette's TestClient) may have closed it
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()
        asyncio.set_event_loop(None)
