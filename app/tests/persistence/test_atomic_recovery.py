import json
from pathlib import Path

import pytest

from backend.persistence.atomic import (
    DamagedFileError,
    backup_path,
    load_model,
    quarantine,
    read_json,
    restore_backup,
    write_json_atomic,
)


def test_every_save_keeps_the_previous_good_copy(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    write_json_atomic(path, {"n": 1})
    assert not backup_path(path).exists()  # nothing to back up the first time

    write_json_atomic(path, {"n": 2})
    assert read_json(path) == {"n": 2}
    assert json.loads(backup_path(path).read_text(encoding="utf-8")) == {"n": 1}


def test_a_damaged_file_is_never_copied_over_the_good_backup(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    write_json_atomic(path, {"n": 1})
    write_json_atomic(path, {"n": 2})  # backup now holds n=1
    path.write_text("{ not json", encoding="utf-8")

    write_json_atomic(path, {"n": 3})  # the damaged content must not become the backup
    assert json.loads(backup_path(path).read_text(encoding="utf-8")) == {"n": 1}


def test_reading_a_damaged_file_raises_damaged_file_error_with_the_reason(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text('{"a": ', encoding="utf-8")

    with pytest.raises(DamagedFileError) as raised:
        read_json(path)
    assert raised.value.path == path
    assert "not valid JSON" in raised.value.reason
    assert isinstance(raised.value, ValueError)  # code that already catches ValueError keeps working


def test_a_file_that_parses_but_lacks_fields_is_damaged_too(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text('{"id": "x"}', encoding="utf-8")

    with pytest.raises(DamagedFileError):
        load_model(path, lambda data: (data["id"], data["name"]))


def test_restore_backup_puts_the_good_copy_back_and_keeps_the_damaged_one(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    write_json_atomic(path, {"n": 1})
    write_json_atomic(path, {"n": 2})
    path.write_text("garbage", encoding="utf-8")

    assert restore_backup(path) is True
    assert read_json(path) == {"n": 1}
    kept = [p for p in tmp_path.iterdir() if ".damaged-" in p.name]
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "garbage"


def test_restore_backup_without_a_usable_backup_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text("garbage", encoding="utf-8")

    assert restore_backup(path) is False
    assert path.read_text(encoding="utf-8") == "garbage"


def test_quarantine_renames_and_never_deletes(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text("garbage", encoding="utf-8")

    kept = quarantine(path)
    assert kept is not None and kept.read_text(encoding="utf-8") == "garbage"
    assert not path.exists()
