import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Nothing a test does may reach the real user workspace. backend.main builds its JobManager (which reads
# the workspace) when it is imported, which can happen before any fixture runs, so the whole session starts
# on a throwaway workspace; each test then gets its own (workspace_root below, autouse).
_SESSION_WORKSPACE = tempfile.mkdtemp(prefix="optifuzz-tests-")
os.environ["OPTIFUZZ_WORKSPACE_ROOT"] = _SESSION_WORKSPACE
atexit.register(shutil.rmtree, _SESSION_WORKSPACE, ignore_errors=True)


@pytest.fixture(autouse=True)
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points OPTIFUZZ_WORKSPACE_ROOT at a fresh tmp_path so tests never touch
    the real user workspace, and never see another test's data."""
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path
