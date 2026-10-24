<h1><img src="app/docs/images/readme_banner.png" alt="OptiFuzz: desktop workspace for web content discovery. Organized. Faster." width="520"></h1>

**A desktop workspace for web content discovery, built around [dirsearch](https://github.com/maurosoria/dirsearch).**

Organise every engagement by project and target, control long fuzzes (pause, resume, and auto-pause when a target rate-limits or blocks you) and turn the raw output into something you can work with: results grouped by directory, a tree you curate with stars and notes, and interactive reports you can share offline.

OptiFuzz runs on **Windows and Linux** and it is local-first: your data stays on your machine, with no telemetry and no cloud. It only goes online when you ask it to check for updates (see [Updating](#updating)).

> **Legal notice.** OptiFuzz is provided for educational purposes and for security testing of systems you own or have explicit authorisation to test. Using it against anything else may be illegal. The author accepts no responsibility for misuse. Read the full [legal disclaimer](#legal-disclaimer) before using it.

## Contents

- [Main features](#main-features)
- [Installation](#installation)
- [Updating](#updating)
- [Trying it safely: the demo target](#trying-it-safely-the-demo-target)
- [User guide](app/docs/USAGE.md)
- [Development and tests](#development-and-tests)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)
- [Legal disclaimer](#legal-disclaimer)
- [License](#license)

## Main features

The main features at a glance. **Everything OptiFuzz does, screen by screen, is explained in the complete documentation:**

> ### Full documentation
>
> **[User guide: all the features, options and screens →](app/docs/USAGE.md)**
>
> Getting started, every form field and option, job states, auto-pause, Results filters and hide rules, the tree, exports, settings and where your data lives.

- **Projects and targets.** One installation holds many projects, each with several targets (hosts). Everything a target produces stays isolated from every other target.
- **Fuzzing you control.** Start dirsearch jobs from a form with a wordlist, extensions, headers and any other dirsearch flag. If you type no extensions, the form tells you that dirsearch's default extensions are active (*Default extensions activated*) and lists them when you hover it. **Pause, resume and stop** at any time; a paused job continues from where it stopped, and an app crash does not lose a long run.
- **Auto-pause.** A job pauses when the target answers a status code you chose (for example `429` or a `403` block), and you decide whether Resume repeats that word or moves on.
- **Readable results.** Colour-coded status codes, a **NEW** badge on what you have not seen yet, search, filters and persistent hide rules to cut noise.
- **A tree you curate.** The FUZZ TREE is not filled automatically: you add the paths that matter, star the interesting ones, write notes, and fuzz any node deeper. OptiFuzz never recurses on its own.
- **Reports you can share.** A self-contained, interactive HTML report per target (it works offline), every target of a project at once, a plain list of tree URLs for other tools, and an optional mirror of dirsearch's raw output.

<p align="center"><img src="app/docs/images/jobs.png" alt="Jobs screen"></p>

<p align="center"><em>Jobs screen: every fuzz of a target with its status, live progress, rate and errors, and the buttons to pause, resume or stop it.</em></p>

<p align="center"><img src="app/docs/images/results.png" alt="Results screen"></p>

<p align="center"><em>Results screen: what dirsearch found, grouped by directory, with colour-coded status codes, the NEW badge and the search, filter and hide-rule controls.</em></p>

<p align="center"><img src="app/docs/images/tree.png" alt="Tree screen"></p>

<p align="center"><em>Tree screen: the paths you chose to keep, with interesting markers, notes and the wordlists already used on each one.</em></p>

## Installation

You need **Git** and **Python 3.11 or newer** (dirsearch requires 3.11; OptiFuzz is tested on 3.11, 3.12 and 3.13). dirsearch is installed automatically from its GitHub repository as a pinned dependency.

On Linux that means Ubuntu 24.04, Debian 12 or newer, Kali, Fedora or Arch. Ubuntu 22.04 ships Python 3.10 and is not supported unless you install a newer Python yourself.

```bash
git clone https://github.com/R3DN3Tx/OptiFuzz.git
cd OptiFuzz
```

### Windows 10 / 11

The window uses WebView2, which ships with current Windows.

```powershell
powershell -ExecutionPolicy Bypass -File .\app\scripts\windows\install-windows.ps1
```

The installer also adds an **OptiFuzz** shortcut to the Start menu: search for "OptiFuzz" there to launch it, or pin it to the taskbar. No administrator rights are needed. If you move the program folder, run the installer again to refresh the shortcut.

### Linux (Debian, Ubuntu, Kali and derivatives)

The window uses GTK 3 and WebKit2GTK 4.1 from your distribution.

```bash
./app/scripts/linux/install-linux.sh
```

Run it as your normal user, without `sudo` in front: it asks for `sudo` itself, only for the system packages, so the virtual environment and the menu entry stay yours. `install-linux.sh` installs everything the app needs: the window toolkit, `git`, the fonts for the icon glyphs, and then the Python environment. It does the system part with `sudo` using your package manager:

| Distribution | Packages it installs | Status |
|---|---|---|
| Debian, Ubuntu, Kali | `python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 git fonts-dejavu-core fonts-noto-core` | tested |
| Fedora | `python3 python3-gobject gtk3 webkit2gtk4.1 git dejavu-sans-fonts google-noto-sans-fonts` | not tested |
| Arch | `python python-gobject gtk3 webkit2gtk-4.1 git ttf-dejavu noto-fonts` | not tested |

On any other distribution, install the equivalents yourself (including the DejaVu and Noto fonts, which provide the icon glyphs such as the gear and the pause symbol) and run `install-linux.sh` again for the Python part. If you would rather not use `sudo`, install those packages by hand first: the script only installs what is missing.

The installer also adds an **OptiFuzz** entry to the application menu. If you move the program folder, run the installer again to refresh it.

### Run without installing a shortcut

If you would rather leave nothing outside the program folder (no Start menu or application menu entry), install with the shortcut turned off and launch OptiFuzz from the folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\app\scripts\windows\install-windows.ps1 -NoShortcut
powershell -ExecutionPolicy Bypass -File .\app\scripts\windows\run-windows.ps1
```
```bash
./app/scripts/linux/install-linux.sh --no-shortcut
./app/scripts/linux/run-linux.sh
```

The policy flag is needed because Windows blocks `.ps1` scripts by default. The program, its virtual environment and your data stay where they are; only the shortcut is skipped.

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\app\scripts\windows\uninstall-windows.ps1   # Windows
```
```bash
./app/scripts/linux/uninstall-linux.sh                                                  # Linux
```

This removes the menu shortcut and the virtual environment, and asks whether to delete your data (projects, results and settings, stored in `%APPDATA%\OptiFuzz` on Windows and `~/.local/share/OptiFuzz` on Linux); it keeps them by default. Pass `-RemoveData` (Windows) or `--remove-data` (Linux) to delete them without being asked. It does not remove the program folder, Python, Git or the Linux system packages.

## Updating

Open **Settings -> Updates** and click *Check for updates*; if there is a newer version, *Update now* downloads it and refreshes the dependencies, and then **OptiFuzz restarts by itself** (the window closes and opens again a few seconds later). Running and queued jobs are paused when you update (resume them after it is back). You do not need to download the repository again, and your projects, targets, jobs and wordlists are kept (your data is stored outside the program folder). It works on any copy installed with `git clone`; if you changed tracked files yourself, it refuses to overwrite them.

## Trying it safely: the demo target

`app/dev/demo-target/` is a tiny fake website that runs on `127.0.0.1:8910` and answers with a variety of status codes (200, 301, 302, 403, 404, 500, among others) at several directory levels. It exists so you can try every feature without touching a real system, and it is what the screenshots above were taken against.

```bash
python app/dev/demo-target/demo_server.py
```

Create a target with base URL `http://127.0.0.1` and port `8910` (in the separate *Port* field), fuzz it with `test_wordlist.txt`, and follow the guided walkthrough, including what each path returns and what to try (deeper fuzzing, pause and resume, a failed job, the tree, the export), in [`app/dev/demo-target/README.md`](app/dev/demo-target/README.md). For a first run, follow [Getting started](app/docs/USAGE.md#2-getting-started) in the user guide.

## Development and tests

The repository root only keeps this README, the license and `wordlists/` (where your own lists go). Everything else the program needs, which you do not have to touch once it is installed, is inside `app/`: the launcher (`app/run_desktop.py`), the backend, the frontend, the dependencies (`app/requirements.txt`, and `app/requirements-dev.txt` for the tests), the version (`app/VERSION`), the install and uninstall scripts (`app/scripts/`), the tests, the demo target and the documentation (`app/docs/`).

The test tools (`pytest`) are not installed with OptiFuzz: they are only needed to run the tests. Install them once, in the same virtual environment, and then run the suite:

```bash
.venv/bin/pip install -r app/requirements-dev.txt        # Windows: .venv\Scripts\pip install -r app\requirements-dev.txt
.venv/bin/python -m pytest app/tests                          # Windows: .venv\Scripts\python -m pytest app\tests
```

CI (`.github/workflows/ci.yml`) runs on every push, on Windows and Linux:

- Ubuntu 24.04 with the distribution's own Python (3.12) and the system GTK/WebKit packages: the full test suite, an end-to-end check against real dirsearch (`app/dev/ci_e2e.py`: complete run, Pause, Resume, Stop, a failed job) and a smoke test that starts the real desktop window under a virtual display (`app/dev/ci_smoke.py`), which also checks the window actually painted.
- Linux with Python 3.11 (the minimum) and 3.13, and Windows with Python 3.13: the suite plus the end-to-end check.

## Troubleshooting

- **Linux under WSL (WSLg).** Rendering is done in software, so scrolling long result lists can stutter. This comes from WSLg itself; a native Linux desktop with a GPU is expected to be much smoother (not verified). The window zooms automatically to match your Windows display scaling; set `OPTIFUZZ_UI_SCALE` (for example `1.25`) to override it.
- **On Linux, a fuzz fails with `AttributeError: 'typing.Union' object has no attribute '__module__'` (in `httpcore`).** An old copy of a library that comes with the distribution was used instead of the one OptiFuzz installs. Update from *Settings -> Updates*, or run `./app/scripts/linux/install-linux.sh` again: both now install OptiFuzz's own copies.
- **Text too small, or wide empty margins, on Linux.** The window uses your desktop's scaling, and the content is limited to a fixed width, so on a high-resolution screen at 100% it looks small and centred. Set a larger percentage in *Settings -> Display zoom*.
- **"Open in browser" does nothing.** Pick a browser in *Settings -> Browser for opening results*.
- **The window does not open.** Start it from a terminal to see errors: `.venv/bin/python app/run_desktop.py`.

## Credits

- [dirsearch](https://github.com/maurosoria/dirsearch) does the actual fuzzing. OptiFuzz does not bundle it; it is installed from its own repository and run as a separate process.
- [pywebview](https://pywebview.flowrl.com), [FastAPI](https://fastapi.tiangolo.com) and [uvicorn](https://www.uvicorn.org) for the application shell.
- Fonts bundled for Linux (SIL Open Font License 1.1): **Selawik** and **Cascadia Mono**, both by Microsoft. See [`app/frontend/fonts/`](app/frontend/fonts/).

## Legal disclaimer

OptiFuzz is a web content-discovery tool intended for **learning, research and authorised security assessments**: your own systems, lab and CTF environments, and engagements where the owner has given you explicit permission (ideally in writing, with a defined scope).

- Sending requests to a system you do not have permission to test can be a crime in many jurisdictions, under computer-misuse and cybercrime laws, whatever your intentions. Get permission first.
- You alone are responsible for how you use this software and for complying with every applicable law, contract, and the rules of any platform, bug-bounty programme or client engagement you work under.
- The author and contributors accept **no responsibility or liability** for any damage, data loss, service disruption, legal consequence or other harm resulting from the use or misuse of this software.
- Fuzzing sends a large number of requests and can degrade or take down a fragile target. Use the thread, rate and delay options, and test only when you are allowed to.
- The software is provided "as is", without warranty of any kind, express or implied (see sections 15 to 17 of the [license](LICENSE)).
- Nothing in this repository is legal advice.

By using OptiFuzz you accept these terms.

## License

OptiFuzz is free software, released under the **GNU General Public License, version 3** ([LICENSE](LICENSE)). Copyright (C) 2026 R3DN3Tx.

In plain words: you may use, study, modify and share it. If you distribute it, modified or not, or distribute a program that contains OptiFuzz code, you must keep the copyright and license notices and make the complete corresponding source code available under the same license. It cannot be turned into closed-source software.

Third-party components keep their own licenses: the bundled fonts are under the SIL Open Font License 1.1 (texts in [`app/frontend/fonts/`](app/frontend/fonts/)), and dirsearch is a separate program under its own license, installed from its repository and not redistributed here.
