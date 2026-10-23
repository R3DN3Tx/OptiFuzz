from __future__ import annotations

from collections.abc import Callable

from backend.persistence import app_settings
from backend.platform_compat import IS_WINDOWS

# Settings -> Display zoom. Linux only: on Windows the window follows the system display scaling
# by itself (WebView2), and changing the zoom there means touching the native window from another
# thread, which is not worth the risk. The window itself is not known to the backend, so
# run_desktop.py registers a function that applies a zoom to it (None = back to automatic).
MIN_ZOOM_PERCENT = 50
MAX_ZOOM_PERCENT = 300

_zoom_applier: Callable[[int | None], None] | None = None


def zoom_supported() -> bool:
    return not IS_WINDOWS


def register_zoom_applier(applier: Callable[[int | None], None]) -> None:
    global _zoom_applier
    _zoom_applier = applier


def set_zoom_percent(percent: int | None) -> None:
    """Stores the zoom and applies it to the open window right away. Raises ValueError when it is
    not available here or out of range."""
    if not zoom_supported():
        raise ValueError("Display zoom is only available on Linux.")
    if percent is not None and not MIN_ZOOM_PERCENT <= percent <= MAX_ZOOM_PERCENT:
        raise ValueError(f"Zoom must be between {MIN_ZOOM_PERCENT}% and {MAX_ZOOM_PERCENT}%.")
    app_settings.set_ui_zoom_percent(percent)
    if _zoom_applier is not None:
        _zoom_applier(percent)
