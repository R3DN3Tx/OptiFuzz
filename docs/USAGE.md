# OptiFuzz user guide

The complete reference for everything OptiFuzz does, screen by screen. For installation, updating and
a short overview, see the [README](../README.md).

> Authorised testing only: use OptiFuzz against systems you own or have explicit permission to test
> (see the [legal disclaimer](../README.md#legal-disclaimer)).

## Contents

| I want to... | Go to |
|---|---|
| Understand how the app is organised | [1. Concepts and screens](#1-concepts-and-screens) |
| Run my first fuzz | [2. Getting started](#2-getting-started) |
| Create, rename or delete projects and targets | [3. Projects](#3-projects) · [4. Targets](#4-targets) |
| Start a fuzz and pick options | [5. Starting a fuzz](#5-starting-a-fuzz-the-new-fuzz-form) |
| Follow, pause, resume or stop jobs | [6. The JOBS tab](#6-the-jobs-tab) |
| Understand auto-pause and stopped-early jobs | [7. Auto-pause and early stops](#7-auto-pause-and-early-stops) |
| Browse and filter what was found | [8. The RESULTS tab](#8-the-results-tab) |
| Curate the paths that matter | [9. The TREE tab](#9-the-tree-tab) |
| Fuzz deeper, with or without recursion | [10. Fuzzing deeper](#10-fuzzing-deeper) |
| Share findings | [11. Exporting](#11-exporting) |
| Change an app setting | [12. Settings](#12-settings) |
| Find my files on disk | [13. Where your data lives](#13-where-your-data-lives) |
| Look something up quickly | [14. Quick reference](#14-quick-reference) |

---

## 1. Concepts and screens

OptiFuzz organises work in three levels. Everything a target produces stays isolated from every other
target.

```text
Project            a client, an engagement, a lab...
 └─ Target         one host (a base URL and port)
     ├─ JOBS       the fuzzes you started, with live progress
     ├─ RESULTS    everything dirsearch found, grouped by directory
     └─ TREE       the paths you chose to keep, with stars and notes
```

| Screen | How to get there | What it is for |
|---|---|---|
| **Projects** | The start page | Create, search, rename and delete projects |
| **Targets** | Click a project | Add targets, set the project's outputs mirror, export every target |
| **Target workspace** | Click a target | The **JOBS**, **RESULTS** and **TREE** tabs, plus the export buttons |
| **Settings** | Gear icon in the header | App-wide options (browser, file names, wordlists folder, port, zoom, updates) |

The header shows a breadcrumb (`Projects / project / target / TAB`; every part but the last is a
link) and the installed version. The window is always dark: there is no light theme.

---

## 2. Getting started

1. Start OptiFuzz (Start menu entry on Windows, application menu entry on Linux).
2. On **Projects**, type a name and click **+ New Project**; click the project.
3. Fill in a **Target**: name, base URL (`https://target.example`) and, if needed, a port; click
   **+ New Target**; click the target.
4. On **JOBS**, leave the path as `/`, pick a wordlist, optionally type extensions (`php,html`) and
   click **Fuzz**.
5. Watch the job run. When hits appear, open **RESULTS**.
6. From a result, use the lightning icon to fuzz inside it, or the tree icon to add it to the **TREE**.
7. In the **TREE**, star what looks interesting and write a note.
8. Click **Export target (HTML)** for a report.

**Wordlists.** The repository ships one example list, `wordlists/test_wordlist.txt`. For real work use
your own: copy your lists into the `wordlists` folder of OptiFuzz, or point OptiFuzz at a folder you
already have (for example SecLists) in [Settings → Wordlists folder](#wordlists-folder).

**Trying it without a real target.** `dev/demo-target/` is a tiny fake website for practising every
feature safely. Start it with `python dev/demo-target/demo_server.py`, then create a target with base
URL `http://127.0.0.1` and port `8910`. Its [walkthrough](../dev/demo-target/README.md) lists what each
path returns and what to try.

---

## 3. Projects

| Action | How |
|---|---|
| Create | Type a name in the field, click **+ New Project** |
| Open | Click the card |
| Search | The search box filters cards by name |
| Rename | Card **⋯** menu → **Rename**, edit, press Enter |
| Delete | Card **⋯** menu → **Delete** (removes the project with all its targets, jobs and results) |

A small **orange dot** on a card means something in it is fuzzing right now; hover it to see each
running job with its target, path and percentage. It updates live.

---

## 4. Targets

### Adding a target

| Field | Meaning |
|---|---|
| **Target name** | A label, for example `api.example.com`. Used in file names and reports |
| **Base URL** | Scheme and host, for example `https://api.example.com/`. A sub-path is kept |
| **Port** | Optional. Empty means the scheme's default: **443** for `https`, **80** for `http`. The port is added to the URL for you, and left out when it is the default |

Click **+ New Target** to create it.

### The Targets page

| Element | What it does |
|---|---|
| Target card | Click to open the workspace. The orange dot works as on projects. **⋯** menu → **Rename** / **Delete** |
| Search | Filters by target name or URL |
| **Outputs mirror** | Optional folder where a copy of every job's raw `.txt` output is saved, one subfolder per target. Type the path or use the folder button. Already finished jobs are copied too. Empty disables it. The original files are never moved. See [Outputs mirror](#outputs-mirror) |
| **Export all targets** | Downloads a report of every target in the project. See [Exporting](#11-exporting) |

---

## 5. Starting a fuzz: the New Fuzz form

The form is at the top of **JOBS**. The same form opens under any row of **RESULTS** or **TREE** when
you click the lightning icon, pre-filled with that path.

![The JOBS tab with fuzzes in every state](images/jobs.png)

### Main fields

| Field | Meaning |
|---|---|
| **Path** | Where to fuzz. `/` fuzzes the root; `/api/` fuzzes inside `/api/`. A path without trailing slash is stored with one, because dirsearch always fuzzes a directory. A `?query` is kept and sent with every request |
| **Wordlist** | The list to use; see [the picker](#the-wordlist-picker) |
| **Extensions** | Comma-separated, for example `php,html`. Empty means none |
| **threads** | Number of parallel requests (default 5) |
| **Fuzz** | Starts the job (it queues if the target is already at its concurrency limit) |

### The wordlist picker

- Opens a searchable dropdown of the [wordlists folder](#wordlists-folder); subfolders are groups
  that expand and collapse. Typing in the search box lists matching files from every folder.
- The **star** next to a file makes it the default wordlist for **new** targets (one at a time).
- The initial choice is, in order: this target's own last choice, the starred list, the last list you
  picked, then the bundled example.

### Advanced options

Open **Advanced options** under the form.

| Option | What it does | Default |
|---|---|---|
| **Force extensions (-f)** | For every wordlist word, also tries `word/` and `word.<ext>` for each extension. Without it, extensions are only appended to wordlist lines that contain `%EXT%` | On |
| **Authenticated** | Adds `__AUTH` to the file name of the job's output, to tell authenticated runs apart. It does not change any request | Off |
| **Auto-pause on status codes** | Comma-separated codes that pause the job as soon as one is seen. See [section 7](#7-auto-pause-and-early-stops) | `429,503` |
| **Concurrent fuzzes** | How many jobs of this target run at once; the rest wait in the queue. Applies immediately and only to this target | 2 |
| **Extra headers (-H)** | One header per row, written `Name: value`. A value may contain colons (`Authorization: Bearer abc:def`). Long values wrap while you edit. **+ Add header** adds a row, **×** removes it | none |
| **Custom options** | Any other dirsearch flag, written exactly as on its command line (`--max-rate 50 --timeout 10 --proxy 127.0.0.1:8080`). Appended after everything else | empty |
| **Default headers** | dirsearch adds `User-Agent`, `Accept`, `Accept-Encoding`, `Keep-Alive` and `Cache-Control` to every request, and `-H` can only override them. Untick one to stop sending it (for example `Keep-Alive`, which servers answering over HTTP/2 reject). The summary shows how many are off | all sent |

### What is remembered

Everything on the form except the path is saved per target as its **default options**, automatically,
even if you never click **Fuzz**. Every new job and every inline launcher of that target starts from
them. A job also keeps the exact options it ran with, whatever you change later.

---

## 6. The JOBS tab

### Job table

| Column | Meaning |
|---|---|
| **Status** | Current state (see below), plus tags such as `⚠ auto` or `⚠ stopped early` |
| **ⓘ** | Opens a panel with the options this job ran with |
| **Actions** | Resume, Pause and Stop, as they apply |
| **Path** | The path that was fuzzed (click to open it in your browser) |
| **Progress** | Percentage and `(done/total)` requests, live |
| **Results** | Number of findings; click to jump to them in RESULTS. Identical findings are counted once |
| **Rate** | Requests per second while running; the average once the job has stopped |
| **Errors** | Request errors, accumulated across the whole job (pauses and resumes do not reset it) |
| **Elapsed** | Running time. It stops while the job is paused and continues when it resumes |
| 🗑 | Deletes a finished job together with its results and output file. Disabled while the job is active or paused |

### Statuses

| Status | Meaning |
|---|---|
| **QUEUED** | Waiting for a free slot (the target's concurrency limit) |
| **RUNNING** | dirsearch is running |
| **PAUSED** | Stopped at a checkpoint and resumable. By you, by [auto-pause](#7-auto-pause-and-early-stops), because dirsearch quit early, or because the app was closed |
| **COMPLETED** | The whole wordlist was requested |
| **STOPPED** | You stopped it. Results found so far are kept; it cannot be resumed |
| **FAILED** | It could not run or crashed. The reason is one click away (ⓘ next to the status); it can be resumed once the cause is gone |

### Pause, Resume, Stop

| Button | What it does |
|---|---|
| **⏸ Pause** | Cuts the run short and keeps the exact position |
| **▶ Resume** | Continues from the checkpoint, not from the start. Only the few requests that were in flight are repeated. Also works on a FAILED job. On an auto-paused job it has a gear, see [section 7](#choosing-what-resume-does) |
| **Stop** | Ends the job for good (STOPPED) |

Progress is checkpointed about every 15 seconds. If the app closes or crashes, jobs that were running
come back as **PAUSED** (tagged `⚠ auto`) and can be resumed. Several jobs can run at once; when a
job finishes, the next queued one starts.

A target that is unreachable when the job starts makes it **FAILED** with the reason *Unreachable
host*.

### The job's options panel (ⓘ)

A one-row table with what this job used:

| Column | Content |
|---|---|
| **Wordlist** | The list used |
| **Extensions** | The extensions |
| **Headers** | The extra headers, and `query*` when the path had a query string |
| **Options** | `threads`, `force-ext`, `AUTH`, `auto pause`, method, proxy, disabled default headers and custom options, in that order |
| **Date** | When it started |

The same table appears under the **Fuzzed** tag in RESULTS and TREE, with one row per job that fuzzed a
path (adding the status and the number of results).

### Output files

Each job keeps dirsearch's own raw output as a readable **`.txt`** in the target's `outputs` folder, named
from a [template](#output-filenames) (default `dirsearch_{target}_{port}_{path}`) plus the job id, with
`__AUTH` added when marked authenticated. OptiFuzz reads the results from dirsearch's JSON report beside
it. Deleting the job deletes both.

---

## 7. Auto-pause and early stops

### Auto-pause on status codes

A target that starts answering `429` (rate limited) or `503` is telling you to slow down; a WAF that
spots the fuzzing may start answering `403` to everything. List those codes in **Auto-pause on status
codes** and the job pauses on the first one, with the reason in the `⚠ auto` tag (hover it to see which
code).

| Detail | Behaviour |
|---|---|
| dirsearch's own `skip-on-status = 429` | Turned off. By default dirsearch silently ends the whole scan at the first `429`; OptiFuzz pauses instead |
| Responses dirsearch hides | Detected too. dirsearch drops some responses from its output (for example the wildcard `403` page its auto-calibration filters out), but OptiFuzz reads them from dirsearch's request log while the job runs |
| Default codes | `429,503`. Empty disables auto-pause |
| After a pause | You decide when to resume, once you have checked whether the target is down or blocking you |

### Choosing what Resume does

When a job auto-paused on a request, the **▶ Resume** button has a **gear** attached on its right.
Click the gear to open the option:

| Setting | Resume behaviour |
|---|---|
| ☑ **Repeat the word that paused it** (default) | The word that triggered the pause is requested again. Use it when you suspect the answer was a block |
| ☐ unticked | The job continues past that word without repeating it. Use it when that word really answers `403` and you were not blocked |

Unticked, the gear shows an orange dot and the Resume tooltip says the word is skipped. The choice is
remembered for that job. The job keeps pausing on every code you armed either way, so you can check each
one by hand. The gear only appears on jobs auto-paused with the version that introduced it (1.1.5 or
newer); older paused jobs resume as before.

### Jobs that stop early

A job is never reported as **COMPLETED** when dirsearch quit before the end of the wordlist. It is
**PAUSED** at the position it really reached, tagged `⚠ stopped early`; click the **ⓘ** next to the tag
to read why, then **Resume**.

| Cause | Message you get |
|---|---|
| Too many request errors in a row (dirsearch gives up after 75) | The message, plus how many requests failed and the most common error (for example `7x ReadTimeout`) |
| dirsearch's own time limit for the target | The message dirsearch printed |
| Any other error that aborted the run (for example a connection reset while reading a response) | That error line |
| Progress ended well short of its own total | `it ended at N of M without saying why` |

---

## 8. The RESULTS tab

Everything dirsearch found for the target, grouped by directory like a tree and updated live while jobs
run.

![Results grouped by directory, with status codes and NEW markers](images/results.png)

### Columns

| Column | Content |
|---|---|
| **Status** | HTTP status, colour-coded by class (table below) |
| **Size** | Response size, as dirsearch reports it |
| *(icons)* | ⚡ **Fuzz this path** and the tree icon, see [Row actions](#row-actions) |
| **Path** | The path, indented under its parent directory with expand/collapse arrows. Next to a folder, `(10)` is how many direct children it has, and `(10 · 3 hidden)` how many of them your [hide rules](#hide-rules) hide |
| **Redirect** | The redirect destination, plus the **NEW** badge |

| Status colour | Class |
|---|---|
| Purple | 1xx |
| Green | 2xx |
| Cyan | 3xx |
| Yellow | 401 and 403 |
| Grey | other 4xx |
| Orange | 429 |
| Red | 5xx |

### Redirects

A redirect to the **same host** shows just its path. One that leaves the target (another host or port)
is shown as the full URL in **orange**: worth a second look. Hover or click it for the full address; a
long redirect wraps without hiding the **NEW** badge.

### NEW badge

**NEW** marks every finding you have not seen yet, so a re-run shows only what changed for you. A
finding is marked as seen when it was actually shown on screen while you were on the tab: rows you
scrolled past or left folded stay NEW. Findings arriving while you watch are highlighted until you leave.

### The Fuzzed tag

A **Fuzzed** tag next to a path means it was already fuzzed from there. Click it to open the history:
a table with one row per job (status, wordlist, extensions, headers, options, number of results, date).
Click a result count to jump to those results. A path and the same path with a trailing slash count as
one place; the tag goes on its main row.

### Row actions

| Action | How |
|---|---|
| Open in your browser | Click the path (or a redirect link). The browser is set in [Settings](#browser-for-opening-results) |
| Copy the full path | Right-click the path (also in JOBS and TREE) |
| Fuzz inside it | ⚡ opens the New Fuzz form under the row, pre-filled. Click it again to close |
| Add to the TREE / remove | Tree icon. Removing asks first, because notes on that path are lost |
| Fold or unfold a directory | The arrow beside it. The state is remembered |

### Search, filters and hide rules

Everything here is remembered **per target**, including after a restart.

| Control | What it does |
|---|---|
| **Search path…** | Shows only paths containing the text |
| **Filters ▾ → Fuzzed** | Only paths that have been fuzzed |
| **Filters ▾ → Status** | Only these codes, comma-separated (`200,301`). Each code counts in the badge |
| **Hide rules ▾** | Rules that permanently cut noise. The badge shows how many are active |

#### Hide rules

A result is hidden when it matches **all** the fields of at least one rule. Fill at least one field and
click **+ Hide**; remove a rule with its **✕**.

| Field | Matches | Example |
|---|---|---|
| **Status** | An exact status code | `404` |
| **Size** | An exact size | `3KB` |
| **Parent path** | Results below this directory | `/admin/` |
| *(depth)* | How many levels below the parent the rule reaches: `1` (default) is direct children, `0` is every descendant | `0` |

Typical use: hide all `404` results of 190 KB that a custom error page produces, or everything under a
noisy `/static/` directory. Exports carry the rules that are active when you export.

---

## 9. The TREE tab

The tree is **yours**: it is **not** filled automatically. It holds the paths you decided matter, so it
stays small and readable. The root `/` is always there and cannot be removed.

![The tree with notes and interesting markers](images/tree.png)

### Adding paths

| How | Detail |
|---|---|
| **+ Add path** | Type a path (`/api/internal`) and press Enter or click the button |
| Tree icon in RESULTS | Promotes a result, keeping its status and size |

Parent directories that were never added appear as placeholders, so the structure is always visible.
Starring, noting or removing a placeholder turns it into a real node.

### Columns and controls

| Element | What it does |
|---|---|
| **Status**, **Size** | From the result it was added from, empty for paths added by hand |
| ★ **Mark interesting** | Toggles the star. Works on any path |
| ⚡ **Fuzz this path** | Opens the New Fuzz form under the row |
| **Path** | Click to open it in your browser. Folders fold and unfold, and the state is remembered |
| **Fuzzed** tag | Shows the wordlist and extensions that were used; click for the full history table |
| **Note** | Type in the row; it saves when you click away. It grows as you write |
| 🗑 | Removes the path and the nodes under it (asks first). It never deletes jobs or results |

### Search and filters

| Control | What it does |
|---|---|
| **Search path…** | Shows only paths containing the text |
| **Filters ▾ → Interesting** | Only starred paths |
| **Filters ▾ → Fuzzed** | Only paths that were fuzzed |
| **Export URLs (TXT)** | See [Exporting](#11-exporting) |

---

## 10. Fuzzing deeper

OptiFuzz is built for manual, step-by-step exploration: it **never recurses on its own**. When a result
looks worth exploring, you start that fuzz yourself from the result or from the tree, so every request
is a decision, and each exploration is its own job with its own options.

dirsearch has its own recursion flags (`-r`, `-R`, ...). You can pass them in **Custom options**, and the
job records everything it finds, including nested paths. They are not officially supported: the progress
counters and Pause/Resume are designed around a single pass over the wordlist, so with recursion the
progress only reflects the first level and a resumed job cannot restore dirsearch's recursion queue.

---

## 11. Exporting

| Export | Where | What you get |
|---|---|---|
| **Export target (HTML)** | Top right of the workspace, every tab | One self-contained `.html` with the target's jobs, results and tree. It works offline with no backend and stays interactive: search, filters and hide rules keep working. The Results hide rules active at that moment are already applied |
| **Export URLs (TXT)** | TREE tab, beside the button above | A plain `.txt` with the absolute URL of every path in the tree, one per line, ready for scanners and crawlers |
| **Export all targets** | Targets page | Every target of the project. The small arrow chooses the format; the choice is remembered and the file is only downloaded when you click the main button: **separate reports (.zip)** (one HTML per target) or **combined report (.html)** (one page with navigation between targets). These exports show everything, unfiltered |
| **Outputs mirror** | Targets page | Not a download: a folder that keeps a copy of every job's raw `.txt` as it finishes. See [Outputs mirror](#outputs-mirror) |

---

## 12. Settings

Open them with the gear in the header. Each entry has its own page; the arrow goes back.

| Setting | What it does |
|---|---|
| [Browser for opening results](#browser-for-opening-results) | Which browser opens a URL when you click it |
| [Output filenames](#output-filenames) | Naming pattern of each job's output file |
| [Default extensions](#default-extensions-force-extensions) | What *Force extensions* uses when no extensions are typed |
| [Wordlists folder](#wordlists-folder) | The folder the wordlist picker reads |
| [Desktop app port](#desktop-app-port) | Local port the app listens on |
| [Display zoom](#display-zoom-linux) | Makes the interface bigger or smaller (Linux) |
| [Updates](#updates) | Check for a new version and update in place |

### Browser for opening results

Detected browsers are listed; pick one, or type the path of an executable and click **Use**. If nothing
is detected, set a custom path.

### Output filenames

The pattern of the raw output file of each job. Placeholders: `{target}` (target name), `{port}` and
`{path}` (the fuzzed path, slashes become underscores). A live example is shown while you edit. It
applies to jobs created from then on.

### Default extensions (Force extensions)

With **Force extensions** on and an empty Extensions field, dirsearch silently falls back to a hidden
default. This setting controls it.

| Choice | Result |
|---|---|
| Leave it blank | dirsearch's own default, like a plain dirsearch run |
| Tick **Use no extensions instead** | None |
| Type your own list | That list |

### Wordlists folder

The folder the wordlist picker browses. Empty means the `wordlists` folder of OptiFuzz. Type a path or
use the folder button. Its subfolders show up as groups. OptiFuzz only reads from it.

### Desktop app port

The local port OptiFuzz listens on (on `127.0.0.1` only). Default **58947**; empty restores it. It
takes effect the next time you start OptiFuzz.

### Display zoom (Linux)

Makes everything bigger or smaller, from **50** to **300** percent, applied immediately and remembered.
Empty means automatic. On Windows the app follows the system display scaling by itself.

### Updates

**Check for updates** compares with the repository you installed from. If there is a newer version,
**Update now** downloads it, refreshes the dependencies and **restarts the app by itself**. Running and
queued jobs are paused first (resume them afterwards). Your projects, targets, jobs and wordlists are
kept, since they live outside the program folder. It works on any copy installed with `git clone`; if
you edited tracked files yourself it refuses to overwrite them.

### Outputs mirror

Not in the Settings menu: it is set per project, on its Targets page. See [Targets](#the-targets-page).

---

## 13. Where your data lives

| What | Where |
|---|---|
| Data root | `%APPDATA%\OptiFuzz` on Windows, `~/.local/share/OptiFuzz` on Linux (override with the `OPTIFUZZ_WORKSPACE_ROOT` environment variable) |
| A target | `projects/<project>/targets/<target>/` under the data root |
| Its jobs | `jobs/<job id>.json`, plus the job's working files |
| Raw outputs | `outputs/` (the `.txt` and the JSON report of every job) |
| Tree, hide rules, seen markers | `tree.json`, `hide_rules.json`, `results_seen.json` in the target folder |

Everything is plain JSON plus dirsearch's untouched output, written atomically, so it survives a crash.

| Environment variable | Effect |
|---|---|
| `OPTIFUZZ_WORKSPACE_ROOT` | Use another data folder |
| `OPTIFUZZ_UI_SCALE` | Linux: force a display zoom factor (for example `1.25`) |

---

## 14. Quick reference

### Buttons and icons

| Icon or button | Meaning |
|---|---|
| ⚡ | Fuzz this path |
| Tree icon | Add to / remove from the TREE |
| ★ | Mark interesting (TREE); default wordlist (picker) |
| ⓘ | The options this job ran with, or the reason it stopped |
| ⚙ on Resume | Choose whether Resume repeats the word that paused the job |
| 🗑 | Delete |
| ▸ / ▾ | Fold or unfold a directory |
| Orange dot on a card | Something is fuzzing in it |
| NEW | Not seen yet |
| Fuzzed | Already fuzzed from here; click for history |
| `⚠ auto` | Paused by auto-pause (or the app closed) |
| `⚠ stopped early` | dirsearch quit before the end of the wordlist |

### Mouse and keyboard

| Action | Result |
|---|---|
| Click a path or redirect | Opens it in your browser |
| Right-click a path (JOBS, RESULTS or TREE) | Copy the full path |
| Enter in the TREE path field | Adds the path |
| Click outside a menu or panel | Closes it |

### Custom options examples

| Goal | Flags |
|---|---|
| Limit the rate | `--max-rate 50` |
| Longer timeout | `--timeout 10` |
| Go through Burp or another proxy | `--proxy 127.0.0.1:8080` |
| Different HTTP method | `-m POST` |

Any dirsearch flag works; see its own documentation.

---

*Back to the [README](../README.md).*
