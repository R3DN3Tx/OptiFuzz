# Dependencies OptiFuzz is coupled to

`app/requirements.txt` pins every version to the one the test suite passes on. Most of the code only uses the public
interface of those packages, and a version bump of those is just "run the tests". This page lists the places where
OptiFuzz depends on something **beyond** the public interface (an internal name, an output format, a command-line
option), because those are the ones that can break silently when a version changes.

When you change a pin: read the matching section, run the tests it names, then do the checklist at the end.
`app/tests/test_dependency_canaries.py` fails with a message naming what moved for most of the items below.

## dirsearch (pinned to a commit in `requirements.txt`)

dirsearch is run as a separate process (`python -c <launcher>`); OptiFuzz never imports it for scanning.

| What OptiFuzz relies on | Where | If it changes |
|---|---|---|
| The command-line options it sends: `-u -w -t -e -f -i -x -H -O -o`, `--no-color --full-url --config --log --http-method --cookie --user-agent --follow-redirects --timeout --proxy --max-rate --delay` | `backend/engine/dirsearch.py` (`build_command`) | A renamed or removed option makes every job fail at start. Canary: `test_dirsearch_still_accepts_every_option_optifuzz_sends` |
| **Launcher patch 1:** the log formatter censors every query value (`?q=<redacted>`). The launcher replaces `lib.core.logger.QUERY_VALUE_PATTERN` and `BARE_QUERY_COMPONENT_PATTERN` with patterns that never match. Credentials in URLs and the proxy password stay censored | `DirsearchEngine._launch_args` | Without it OptiFuzz cannot tell which word an auto-pause happened on, nor find its place when the progress bar is silent. If the names change the patch is skipped (jobs still run) and those two features degrade. Canaries: `test_the_names_inside_dirsearch_that_the_launcher_patches_still_exist`, `test_the_launcher_really_changes_what_dirsearch_logs_and_sends` |
| **Launcher patch 2:** `lib.core.settings.DEFAULT_HEADERS` is a dict the launcher pops from (*Advanced options -> Default headers*) | `DirsearchEngine._launch_args` | Same canaries. If it is no longer a dict the launcher fails and so does every job |
| `dirsearch.dirsearch.main` is the entry point | `DirsearchEngine._launch_args` | Same canaries |
| Live output: result lines `[hh:mm:ss] 200 - 1KB - url -> redirect` and the progress bar `NN% index/length rate/s job:a/b errors:n` | `backend/parser/stdout_parser.py` (`RESULT_LINE_RE`, `PROGRESS_RE`) | Results or progress stop appearing, or a paused job restarts from 0. Tests: `tests/parser`. The progress bar is drawn only for requests that are not hits: the request log is the fallback |
| The request log format `"GET url" status - sizeB` (one line per response, hits and filtered ones included) | `backend/job_manager/dirsearch_log.py` | Auto-pause on filtered responses and the fallback position stop working. Canary: `test_dirsearch_request_log_format_is_still_the_one_the_parsers_read` |
| Rotation of that log (`log-file-size`, one backup file) | `backend/engine/optifuzz_dirsearch_config*.ini`, `remove_dirsearch_logs` | The log of a job can be `dirsearch.log` plus `dirsearch.log.1`; both are deleted when a run segment ends |
| Two config files derived from dirsearch's own `config.ini`: `default-extensions` blank, `skip-on-status` blank (its `429` default ends the whole scan before auto-pause can see it), `log-file-size` set | `backend/engine/optifuzz_dirsearch_config.ini`, `..._default_ext.ini` | Diff both against the new `config.ini`: new keys, renamed sections, changed defaults |
| The plain and JSON reports written with `-O plain,json -o <base>.{extension}` | `backend/services/results_service.py` | Results stop loading after a run |
| How dirsearch expands a wordlist (extensions, `-f`, `%EXT%`, de-duplication, calibration probes) | `backend/job_manager/wordlist_resolver.py` | The resume position and the request totals drift. Tests: `tests/job_manager/test_wordlist_resolver.py` |
| It stops at once on `SIGKILL` and ignores `SIGTERM` on Linux (it opens its pause menu) | `backend/platform_compat.py` | Pause and Stop hang on Linux |

## pywebview (desktop window, `app/run_desktop.py`)

| What OptiFuzz relies on | If it changes |
|---|---|
| `window.native` (the WinForms form, on Windows) to style the title bar and read the window handle. It must only be touched from the UI thread | The title bar loses its dark style or the app hangs on start |
| `webview.platforms.gtk` and its `BrowserView` (Linux) for the page zoom | The zoom setting stops working on Linux |
| `create_file_dialog` with `FileDialog.FOLDER` / `FileDialog.SAVE`, and the `js_api` object | Choose folder and the export Save As window |

Canary: `test_the_desktop_window_library_still_has_what_run_desktop_calls` (source lookup only; it never opens a window).
Check the rest by hand on Windows and Linux: nothing in the test suite opens the real window.

## Other packages

| Package | Used for | Note |
|---|---|---|
| fastapi, uvicorn | the local API and its server (`uvicorn.Config` / `uvicorn.Server` in `run_desktop.py`) | Covered by the API tests |
| psutil | process liveness and the process tree | Covered by the job manager tests |
| pywinpty (Windows only) | an old way to attach dirsearch to a pseudo-console | **Not used any more** (`uses_pty = False` in every engine); only `backend/job_manager/pty_runner.py` imports it. Candidate for removal |

## Checklist when you change a pinned version

1. Change the pin in `app/requirements.txt` (never `pip install -U`), reinstall.
2. Run the whole suite on Windows and on Linux. Read the failing canary messages first.
3. For dirsearch: diff its `config.ini` against `backend/engine/optifuzz_dirsearch_config*.ini` and read its changelog for
   the options and output formats in the table above.
4. Real run against the demo target (`app/dev/demo-target`): a fuzz with `FUZZ` in the path and one in the query,
   an auto-pause on a status, Pause then Resume, a job with *Default headers* switched off.
5. Real window, by hand, on Windows and Linux (open, export, Choose folder, update restart) when pywebview changed.
6. Record the new pin and anything you had to adapt in `app/docs/DEVELOPMENT_PLAN.md`.
