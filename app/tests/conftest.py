from pathlib import Path

import pytest


@pytest.fixture()
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points OPTIFUZZ_WORKSPACE_ROOT at a fresh tmp_path so tests never touch
    the real user workspace, and never see another test's data."""
    monkeypatch.setenv("OPTIFUZZ_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path
