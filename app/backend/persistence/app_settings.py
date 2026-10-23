from __future__ import annotations

from typing import Any

from backend.persistence.atomic import read_json, write_json_atomic
from backend.persistence.paths import workspace_root

# Small app-wide UI preferences (currently just the browser override) --
# distinct from workspace.json's job-related global config and
# from any per-Project/Target data. One flat JSON file is enough for this.


def _settings_file():
    return workspace_root() / "app_settings.json"


def load_settings() -> dict[str, Any]:
    path = _settings_file()
    if not path.is_file():
        return {}
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict[str, Any]) -> None:
    write_json_atomic(_settings_file(), data)


def get_browser_path() -> str | None:
    return load_settings().get("browser_path")


def set_browser_path(path: str | None) -> None:
    settings = load_settings()
    if path:
        settings["browser_path"] = path
    else:
        settings.pop("browser_path", None)
    save_settings(settings)


# No {path} by default: a long fuzzed path makes a file name Windows cannot always zip and unzip (see
# output_naming). A job id is always appended after the template, so two jobs never share a file.
DEFAULT_OUTPUT_FILENAME_TEMPLATE = "dirsearch_{target}_{port}"


def get_configured_output_filename_template() -> str | None:
    """The template the user saved, or None when the default is in use."""
    return load_settings().get("output_filename_template") or None


def get_output_filename_template() -> str:
    return get_configured_output_filename_template() or DEFAULT_OUTPUT_FILENAME_TEMPLATE


def set_output_filename_template(value: str) -> None:
    settings = load_settings()
    if value and value.strip():
        settings["output_filename_template"] = value.strip()
    else:
        settings.pop("output_filename_template", None)
    save_settings(settings)


# Custom desktop-app port (2026-09-24, explicit request). Lives here, not
# in run_desktop.py, so backend/api/settings.py (which serves the Settings
# UI's GET, showing this as the input's placeholder) can see it without
# importing run_desktop.py itself -- that module does `import webview` at
# top level, a desktop-only dependency this backend package otherwise never
# needs (tests, the plain-browser-tab dev flow). run_desktop.py imports
# DEFAULT_PORT and get_custom_port() from here instead of defining its own.
#
# get_custom_port() is read directly by run_desktop.py before uvicorn.Config()
# even exists (see DEFAULT_PORT's own comment below for why a STABLE port
# across restarts matters for localStorage) to decide which port to try
# first instead of always DEFAULT_PORT. None/absent means "no override, use
# the built-in default" -- the Settings UI shows that default as its
# input's placeholder for exactly this reason, never as a stored value here.
# Takes effect on the NEXT restart only -- the already-running instance
# stays bound to whatever port it started on.
#
# A stable, dedicated-feeling default -- tried first on every launch (or
# whatever custom_port overrides it to) so the app's own origin
# (http://127.0.0.1:<port>) stays the SAME across restarts. Real, reported
# bug this fixes: the frontend's localStorage (starred/last-used wordlist,
# collapsed-group memory, etc.) is partitioned by origin INCLUDING the port
# -- run_desktop.py's own stable WebView2 profile directory only keeps that
# partition's on-disk files around across restarts, it does nothing if
# every restart is a *different* partition to begin with, which a random
# _free_port() call every time guaranteed. run_desktop.py falls back to a
# real free port if this one is ever taken (another instance already
# running, or an unrelated process/service using it) -- localStorage simply
# won't carry over from before in that one edge case, better than never
# working at all.
DEFAULT_PORT = 58947


def get_custom_port() -> int | None:
    return load_settings().get("custom_port")


def set_custom_port(port: int | None) -> None:
    settings = load_settings()
    if port:
        settings["custom_port"] = port
    else:
        settings.pop("custom_port", None)
    save_settings(settings)


# Folder the New Fuzz wordlist picker browses. None/absent means "the
# wordlists/ folder inside the program" (wordlist_service.WORDLISTS_DIR); the
# Settings UI shows that default as the input's placeholder, never as a stored
# value here, so moving the program folder never leaves a stale path behind.
def get_wordlists_dir() -> str | None:
    return load_settings().get("wordlists_dir")


def set_wordlists_dir(path: str | None) -> None:
    settings = load_settings()
    if path:
        settings["wordlists_dir"] = path
    else:
        settings.pop("wordlists_dir", None)
    save_settings(settings)


# Display zoom of the desktop window, in percent (Linux only, see display_service). None/absent =
# automatic (what run_desktop.py works out from the desktop's own scaling), never stored as a value.
def get_ui_zoom_percent() -> int | None:
    return load_settings().get("ui_zoom_percent")


def set_ui_zoom_percent(percent: int | None) -> None:
    settings = load_settings()
    if percent:
        settings["ui_zoom_percent"] = percent
    else:
        settings.pop("ui_zoom_percent", None)
    save_settings(settings)


# dirsearch's own shipped config.ini value -- the Settings UI shows this as
# the field's own placeholder (see get_default_extensions_fallback's own
# docstring just below for why).
DIRSEARCH_DEFAULT_EXTENSIONS = "php,asp,aspx,jsp,html,htm"


# Real, reported bug (2026-09-25): dirsearch's own options.py does
# `opt.extensions = opt.extensions or config.safe_get("dictionary",
# "default-extensions", "")` -- its shipped config.ini sets that to
# "php,asp,aspx,jsp,html,htm". Whenever a fuzz's own `-e` is never passed at
# all (no extensions chosen for that job), dirsearch silently falls back to
# those six instead of genuinely "no extensions" -- invisible from
# OptiFuzz's own UI, and NOT fixable by passing `-e ""` explicitly (an empty
# string is just as falsy to Python's `or` as never passing the flag).
# `None`/absent (the key genuinely missing from the settings file) means
# "not configured -- leave dirsearch's own real default-extensions behavior
# alone, unmodified" -- the honest, matches-a-terminal-launch default, and
# the Settings UI shows dirsearch's own real value as that field's
# placeholder for exactly this reason. `""` (present, explicitly empty)
# means "use none, ever". A non-empty string is a custom comma-separated
# fallback list, used instead of dirsearch's own. See
# DirsearchEngine.build_command's own comment for how each of these three
# states actually changes the spawned command.
def get_default_extensions_fallback() -> str | None:
    settings = load_settings()
    if "default_extensions_fallback" not in settings:
        return None
    return settings["default_extensions_fallback"]


def set_default_extensions_fallback(value: str | None) -> None:
    settings = load_settings()
    if value is None:
        settings.pop("default_extensions_fallback", None)
    else:
        settings["default_extensions_fallback"] = value.strip()
    save_settings(settings)


# Small per-viewer UI preferences (last wordlist, which result/tree groups are
# collapsed, ...) -- moved here from the browser's localStorage (2026-10-06):
# localStorage is tied to the page origin, so a launch on another port lost
# all of them. Keys are the frontend's own (e.g. "collapsed:results/p/t").
def get_ui_prefs() -> dict[str, Any]:
    return load_settings().get("ui_prefs", {})


def set_ui_pref(key: str, value: Any) -> None:
    settings = load_settings()
    prefs = settings.setdefault("ui_prefs", {})
    if value is None:
        prefs.pop(key, None)
    else:
        prefs[key] = value
    save_settings(settings)


def get_target_concurrency(target_id: str) -> int | None:
    return load_settings().get("target_concurrency", {}).get(target_id)


def set_target_concurrency(target_id: str, value: int) -> None:
    settings = load_settings()
    settings.setdefault("target_concurrency", {})[target_id] = value
    save_settings(settings)


# Per-target New Fuzz defaults (explicit request, 2026-09-23): whatever a
# pentester configures in the New Fuzz form for a given target -- without
# ever actually launching a job -- is remembered and pre-fills that same
# form (the main New Fuzz section AND every inline "Fuzz this path"
# launcher from Results/TREE, since both share frontend/app.js's own
# buildNewFuzzForm) the next time it's opened for that target. Deliberately
# does NOT include the selected wordlist path/label -- that stays the
# existing APP-WIDE starred/last-used mechanism (frontend's own
# loadPref/savePref, already global by design) unless a target explicitly
# overrides it here, which the frontend treats as the highest-priority
# choice when present.
def get_target_fuzz_defaults(target_id: str) -> dict[str, Any] | None:
    return load_settings().get("target_fuzz_defaults", {}).get(target_id)


def set_target_fuzz_defaults(target_id: str, defaults: dict[str, Any]) -> None:
    settings = load_settings()
    settings.setdefault("target_fuzz_defaults", {})[target_id] = defaults
    save_settings(settings)
