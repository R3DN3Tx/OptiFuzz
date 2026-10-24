from __future__ import annotations

import threading
from typing import Any

# Problems the user should know about that were found while the app started (a settings file that had to
# be restored, a port that was busy). Shown in Settings, with a red dot on the gear while any is unseen.
# A notice counts as seen once the user has been shown it (opened the Settings menu, or the page it is about),
# and then stops being listed: the dot goes away and the message is not repeated.
_lock = threading.Lock()
_notices: dict[str, dict[str, Any]] = {}


def set_notice(notice_id: str, message: str, **extra: Any) -> None:
    with _lock:
        _notices[notice_id] = {"id": notice_id, "message": message, "seen": False, **extra}


def clear_notice(notice_id: str) -> None:
    with _lock:
        _notices.pop(notice_id, None)


def mark_seen(notice_id: str) -> None:
    with _lock:
        if notice_id in _notices:
            _notices[notice_id]["seen"] = True


def list_notices(include_seen: bool = False) -> list[dict[str, Any]]:
    with _lock:
        return [dict(n) for n in _notices.values() if include_seen or not n["seen"]]
