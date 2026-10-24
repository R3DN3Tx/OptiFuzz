"""Smaller safeguards: single instance, busy ports, embedded report data, leftover scans, error lines."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.job_manager.manager import JobManager, _end_leftover_scan
from backend.services import instance_lock, notice_service, port_service
from backend.services.export_service import _embeddable_json

APP_DIR = str(Path(__file__).resolve().parents[1])
TRY_LOCK = "from backend.services import instance_lock; print(instance_lock.acquire())"


def _try_lock_in_another_process(workspace: Path) -> str:
    env = dict(os.environ, OPTIFUZZ_WORKSPACE_ROOT=str(workspace))
    done = subprocess.run([sys.executable, "-c", TRY_LOCK], env=env, capture_output=True, text=True, timeout=60, cwd=APP_DIR)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# ---- single instance ---------------------------------------------------------------------------------------

def test_a_second_process_cannot_take_the_same_data_folder(workspace_root: Path) -> None:
    assert instance_lock.acquire() is True
    try:
        assert instance_lock.owner_pid() == os.getpid()
        assert _try_lock_in_another_process(workspace_root) == "False"
    finally:
        instance_lock.release()
    # Once the first copy is gone the folder is free again.
    assert _try_lock_in_another_process(workspace_root) == "True"


def test_another_data_folder_is_not_blocked(workspace_root: Path, tmp_path: Path) -> None:
    assert instance_lock.acquire() is True
    try:
        assert _try_lock_in_another_process(tmp_path / "other") == "True"
    finally:
        instance_lock.release()


# ---- ports -------------------------------------------------------------------------------------------------

def _busy_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock, sock.getsockname()[1]


def test_a_free_port_is_used_as_is_and_clears_any_notice() -> None:
    notice_service.set_notice("port", "old")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert port_service.pick_port(free, chosen_by_user=False) == free
    assert notice_service.list_notices() == []


def test_a_busy_port_is_replaced_by_a_nearby_uncommon_one_and_a_notice_says_so() -> None:
    sock, busy = _busy_port()
    try:
        chosen = port_service.pick_port(busy, chosen_by_user=True)
    finally:
        sock.close()
    assert chosen != busy and chosen not in port_service.COMMON_PORTS and chosen >= 1024
    notice = notice_service.list_notices()[0]
    assert notice["id"] == "port" and str(busy) in notice["message"] and str(chosen) in notice["message"]
    assert "you chose" in notice["message"]
    notice_service.clear_notice("port")


def test_the_message_names_the_default_port_when_the_user_chose_none() -> None:
    sock, busy = _busy_port()
    try:
        port_service.pick_port(busy, chosen_by_user=False)
    finally:
        sock.close()
    assert "default port" in notice_service.list_notices()[0]["message"]
    notice_service.clear_notice("port")


def test_the_fallback_skips_commonly_used_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(port_service, "is_free", lambda port: True)
    assert 3000 in port_service.COMMON_PORTS and 3001 in port_service.COMMON_PORTS
    assert port_service.alternative_for(2999) == 3002  # 3000 and 3001 are commonly used, so they are skipped


# ---- text embedded in exported reports ---------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "</script><img src=x onerror=alert(1)>", "<!--<script>", "<!-- x", "a & b", "<script>", chr(0x2028) + chr(0x2029),
])
def test_embedded_json_can_never_close_or_open_anything_in_the_page(hostile: str) -> None:
    data = {"name": hostile, "nested": [hostile]}
    text = _embeddable_json(data)
    assert "<" not in text and ">" not in text and "&" not in text
    assert chr(0x2028) not in text and chr(0x2029) not in text
    assert json.loads(text) == data  # the page reads back exactly what was stored


# ---- leftover scans ------------------------------------------------------------------------------------------

def test_a_leftover_scan_is_ended_but_an_unrelated_process_with_that_pid_is_not() -> None:
    sleeper = "import time; time.sleep(60)"
    scan = subprocess.Popen([sys.executable, "-c", sleeper, "dirsearch"])
    unrelated = subprocess.Popen([sys.executable, "-c", sleeper])
    try:
        _end_leftover_scan(unrelated.pid)
        time.sleep(0.3)
        assert unrelated.poll() is None, "a process that is not dirsearch must never be ended"
        _end_leftover_scan(scan.pid)
        scan.wait(timeout=10)
        assert scan.poll() is not None
    finally:
        for process in (scan, unrelated):
            if process.poll() is None:
                process.kill()
    _end_leftover_scan(None)
    _end_leftover_scan(99999999)  # no such process: nothing happens


# ---- why a job failed ----------------------------------------------------------------------------------------

def test_the_reason_for_a_failed_start_is_dirsearchs_own_last_line() -> None:
    escape = chr(27)
    tail = f"banner\r\nTarget: http://x/\r\n{escape}[31m--timeout must be finite and greater than zero{escape}[0m\r\n"
    assert JobManager._last_output_line(tail) == "--timeout must be finite and greater than zero"
    assert JobManager._last_output_line("") is None
    assert JobManager._last_output_line("[12:00:00] 200 -  1KB - /a\n") is None  # a result is not an error
