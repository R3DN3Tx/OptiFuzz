from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.engine.mock import MockDirsearchEngine
from backend.events.bus import EventBus
from backend.job_manager.manager import JobManager
from backend.main import app


@pytest.fixture()
def client(workspace_root: Path) -> TestClient:
    """A TestClient wired to a fresh MockDirsearchEngine-backed JobManager
    and its own EventBus, so API tests never touch the network or a shared
    global bus (docs/OPTIFUZZ_SPEC.md §58)."""
    app.state.job_manager = JobManager(MockDirsearchEngine(every=1), event_bus=EventBus())
    return TestClient(app)
