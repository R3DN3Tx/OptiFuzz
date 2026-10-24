# OptiFuzz user guide

Everything OptiFuzz does, screen by screen. Installation and updating are in the [README](../../README.md).

Use OptiFuzz only against systems you own or have explicit permission to test (see the [legal disclaimer](../../README.md#legal-disclaimer)).

## Contents

- [1. Overview](#1-overview)
  - [How work is organised](#how-work-is-organised)
  - [The screens](#the-screens)
  - [Moving around](#moving-around)
- [2. Getting started](#2-getting-started)
- [3. Projects and targets](#3-projects-and-targets)
  - [Projects](#projects)
  - [Targets](#targets)
- [4. Starting a fuzz](#4-starting-a-fuzz)
  - [Where to start one](#where-to-start-one)
  - [Main fields](#main-fields)
  - [The wordlist picker](#the-wordlist-picker)
  - [Advanced options](#advanced-options)
  - [What is remembered](#what-is-remembered)
- [5. Managing jobs](#5-managing-jobs)
  - [The job list](#the-job-list)
  - [Job states](#job-states)
  - [Pause, Resume, Stop and Delete](#pause-resume-stop-and-delete)
  - [Details a job was started with](#details-a-job-was-started-with)
    - [The Raw column](#the-raw-column)
  - [Queue and concurrency](#queue-and-concurrency)
  - [Output files](#output-files)
- [6. Auto-pause and early stops](#6-auto-pause-and-early-stops)
- [7. Results](#7-results)
  - [Reading the list](#reading-the-list)
  - [The NEW badge](#the-new-badge)
  - [The Fuzzed tag](#the-fuzzed-tag)
  - [Actions on a path](#actions-on-a-path)
  - [Search, filters and hide rules](#search-filters-and-hide-rules)
- [8. Tree](#8-tree)
- [9. Fuzzing deeper](#9-fuzzing-deeper)
- [10. Exporting](#10-exporting)
  - [Saving an export](#saving-an-export)
  - [Export target (HTML)](#export-target-html)
  - [Export URLs (TXT)](#export-urls-txt)
  - [Export all targets](#export-all-targets)
  - [Outputs mirror](#outputs-mirror)
- [11. Settings](#11-settings)
- [12. Where your data lives](#12-where-your-data-lives)

---

## 1. Overview

### How work is organised

OptiFuzz has three levels. Everything a target produces (jobs, results, tree, notes, reports) stays separate from every other target.

- **Project**: a client, an engagement or a lab.
- **Target**: one host inside a project, defined by a base URL and port.
- **Jobs, Results and Tree**: what you work with inside a target. Jobs are the fuzzes you started, Results is everything they found, and the Tree is the short list of paths you decided to keep.

### The screens

- **Projects**: the start page. Create, search, rename and delete projects.
- **Targets**: opened by clicking a project. Add targets to it, set the project's outputs mirror, and export all its targets at once.
- **Target workspace**: opened by clicking a target. It has the **JOBS**, **RESULTS** and **TREE** tabs and the buttons to export that single target.
- **Settings**: the gear icon at the top right of the header, next to the version number. It holds the app-wide options (see [Settings](#11-settings)).

### Moving around

The line at the top of every page shows where you are, for example `Projects / Demo Project / api.example.com / RESULTS`. Its parts are the project list (`Projects`), the project (`Demo Project`), the target (`api.example.com`) and the tab you are on (`RESULTS`). `Projects` and the project name are links: click `Projects` to go back to the list of projects, or the project name to go back to that project's targets page. The target name and the tab are only labels that tell you where you are; to change tab, use the tabs of the workspace.

Each page remembers its scroll position, so you can switch between JOBS, RESULTS and TREE and find each one where you left it.

---

## 2. Getting started

1. Start OptiFuzz from the Start menu (Windows) or the application menu (Linux).
2. On **Projects**, type a name, click **+ New Project**, then click the new project.
3. Fill in the target's name and base URL (for example `https://target.example`), click **+ New Target**, then click the target.
4. On **JOBS**, leave the path as `/`, pick a wordlist, optionally type extensions (for example `php,html`) and click **Fuzz**.
5. Watch the job run. When findings appear, open **RESULTS**.
6. On a result, click the fuzz icon <img src="images/icons/fuzz.svg" width="16" alt="fuzz icon"> to fuzz inside that path, or the tree icon <img src="images/icons/tree.svg" width="16" alt="tree icon"> to add it to the Tree.
7. In **TREE**, star what looks interesting and write a note about it.
8. Click **Export target (HTML)** when you want a report.

**Wordlists.** The repository includes one example list, `wordlists/test_wordlist.txt`. For real work, use your own: copy them into the `wordlists` folder of OptiFuzz, or point OptiFuzz at a folder you already have (SecLists, for example) in [Settings](#wordlists-folder).

**Trying it without a real target.** `app/dev/demo-target/` is a small fake website for practising every feature safely. Start it with `python app/dev/demo-target/demo_server.py`, then create a target with base URL `http://127.0.0.1` and port `8910`. Its [walkthrough](../dev/demo-target/README.md) explains what each path returns and what to try.

---

## 3. Projects and targets

### Projects

Type a name and click **+ New Project** to create one. Click a project card to open it, and use the search box above the cards to filter them by name.

To rename or delete a project, click the three dots (⋯) in the top-right corner of its card and choose **Rename** or **Delete**. After choosing Rename, type the new name where the old one was and press Enter. Deleting removes the project together with its targets, jobs and results.

A small orange dot on a card means something is fuzzing inside it right now. Hover the dot to see each running job with its target, path and progress. It updates live.

### Targets

Opening a project shows its targets page. To add a target, fill in:

- **Target name**: a label of your choice, for example `api.example.com`. It is used in file names and reports.
- **Base URL**: the scheme and host, for example `https://api.example.com/`.
- **Port** (optional): leave it empty to use the default of the scheme, 443 for `https` and 80 for `http`. The port is added to the URL for you, and omitted when it is the default one.

Then click **+ New Target**. Target cards work like project cards: click one to open its workspace, use the search box to filter by name or URL, use the three-dot menu to rename or delete it, and watch for the orange dot.

The targets page also has two project-wide tools, described in [Exporting](#10-exporting): the **Outputs mirror** and **Export all targets**.

---

## 4. Starting a fuzz

### Where to start one

The **New Fuzz** form is at the top of the JOBS tab.

You can also start a fuzz from a path you already found. In RESULTS and in TREE, each path has a fuzz icon <img src="images/icons/fuzz.svg" width="16" alt="fuzz icon">. Clicking it opens the same New Fuzz form directly below that row, with the path already filled in, so you can choose a wordlist and launch without leaving the tab. Click the icon again to close it.

### Main fields

- **Path**: where to fuzz. `/` fuzzes the whole site, `/api/` fuzzes inside `/api/`. A path typed without a trailing slash is saved with one, because dirsearch always fuzzes a directory. A query string is kept and sent with every request. Type it after the path: with `/search/?lang=en`, each word of the wordlist goes before the query, so dirsearch requests `/search/word?lang=en`.
- **Wordlist**: the list of words to try. See the picker below.
- **Extensions**: comma-separated, for example `php,html`. If you leave it empty, dirsearch does not use none: it falls back to its own default extensions (`php,asp,aspx,jsp,html,htm`), and the field says *Default extensions activated*. Hover it, or click inside it, to see the list in use. You can change that default in [Default extensions (Force extensions)](#default-extensions-force-extensions).
- **Threads**: how many requests run in parallel (default 5).
- **Fuzz**: starts the job. If the target is already running as many jobs as it is allowed, the new one waits in the queue.

### The wordlist picker

Click it to open a dropdown with the files of your [wordlists folder](#wordlists-folder). Subfolders appear as groups you can expand and collapse. Typing in the search box lists the matching files from every folder.

The star next to a file, <img src="images/icons/star.svg" width="16" alt="star icon">, makes it the default wordlist for every new target, in any project (only one list can be the default at a time). When you open the form, the list selected is the first that applies of: the last one chosen for this target, the starred default, the last list you picked in any project, and the example list.

### Advanced options

Click **Advanced options** under the form to see them.

- **Force extensions (-f)**: for every word, also tries `word/` and `word.<extension>` for each extension. Without it, extensions are only added to wordlist lines that contain `%EXT%`. On by default.
- **Authenticated**: adds `__AUTH` to the name of the job's output file so authenticated runs are easy to tell apart. It does not change any request. Off by default.
- **Auto-pause on status codes**: the status codes that make the job pause. You can list any codes you want, separated by commas (default `429,503`). Leave it empty to turn auto-pause off. See [Auto-pause and early stops](#6-auto-pause-and-early-stops).
- **Concurrent fuzzes**: how many jobs of this target run at the same time (default 2). It applies immediately and only to this target.
- **Extra headers (-H)**: one header per row, written as `Name: value`. The value can contain colons, as in `Authorization: Bearer abc:def`. **+ Add header** adds a row and **×** removes it.
- **Custom options**: any other dirsearch flag, written exactly as on the dirsearch command line, for example `--max-rate 50 --timeout 10 --proxy 127.0.0.1:8080`. It is added after everything else. This includes dirsearch's recursion flags, which OptiFuzz does not officially support: read [Fuzzing deeper](#9-fuzzing-deeper) before using them.
- **Default headers**: dirsearch adds `User-Agent`, `Accept`, `Accept-Encoding`, `Keep-Alive` and `Cache-Control` to every request, and `-H` can only override them. Untick a header to stop sending it (for example `Keep-Alive`, which servers answering over HTTP/2 reject). The title shows how many are turned off.

### What is remembered

Everything on the form except the path is saved for each target as its default options, automatically, even if you never click Fuzz. New jobs and the forms opened from Results and Tree start from them. Each job also keeps the exact options it ran with, whatever you change later.

---

## 5. Managing jobs

### The job list

Each job is a row of the **Running / Jobs** table:

- **Status**: the current state (see below), sometimes followed by a tag such as `⚠ auto` or `⚠ stopped early`.
- **Information icon** <img src="images/icons/info.svg" width="16" alt="info icon">: shows [the details the job was started with](#details-a-job-was-started-with).
- **Buttons**: Resume, Pause and Stop, depending on the state.
- **Path**: the path that was fuzzed. Click it to open it in your browser.
- **Progress**: percentage and `(done/total)` requests, updated live.
- **Results**: how many findings the job has. Click the number to go straight to the Results of that fuzzed path. Identical findings are counted once.
- **Rate**: requests per second while running, and the average once the job has stopped.
- **Errors**: failed requests, counted over the whole job. Pausing and resuming does not reset it.
- **Elapsed**: running time. It stops counting while the job is paused.
- **Trash icon** <img src="images/icons/trash.svg" width="16" alt="trash icon">: deletes a finished job with its results and output file. It does not remove any path from the Tree; the **Fuzzed** tag of the paths that job fuzzed just stops listing it.

### Job states

| State | Meaning |
|---|---|
| QUEUED | Waiting for a free slot: its target is already running as many jobs as its **Concurrent fuzzes** limit allows (see [Queue and concurrency](#queue-and-concurrency)) |
| RUNNING | dirsearch is running |
| PAUSED | Stopped at a checkpoint, ready to resume. Caused by you, by auto-pause, by dirsearch quitting early, or by closing the app |
| COMPLETED | The whole wordlist was requested |
| STOPPED | You stopped it. The findings so far are kept, but it cannot be resumed |
| FAILED | It could not run or it crashed. The reason is one click away, on the information button next to the status. It can be resumed manually once the cause is fixed |

### Pause, Resume, Stop and Delete

- **Pause** stops the run and remembers the exact position.
- **Resume** continues from that position, not from the start; only the few requests that were in flight are repeated. It works on paused and failed jobs. On a job that auto-paused it has an extra option, explained in [Choosing what Resume does](#choosing-what-resume-does).
- **Stop** ends the job for good.
- **Delete** (trash icon) removes a finished job together with its results and output file. It is disabled while the job is queued, running or paused.

Progress is saved about every 15 seconds. If the app closes or crashes, the jobs that were running come back as paused, with the `⚠ auto` tag, ready to resume. A target that cannot be reached when a job starts makes it fail with the reason *Unreachable host*.

### Details a job was started with

Click the information icon <img src="images/icons/info.svg" width="16" alt="info icon"> on a job to see the settings it was launched with: the **wordlist**, **extensions**, **headers** (and `query*` when the path had a query string), **options** (threads, force extensions, `AUTH`, auto-pause codes, HTTP method, proxy, disabled default headers and custom options), the **date** it started and, at the far right, the **Raw** column (see below).

The same table appears when you click a **Fuzzed** tag in Results or Tree, with one row per job that fuzzed that path. There it also has a **Status** column (the state of each job) and a **Results** column (how many findings it has). When a job had no extensions of its own, the **extensions** column shows the default list dirsearch used, marked `(default)`.

#### The Raw column

The last column of every details table, **Raw**, has an arrow pointing up and to the right <img src="images/icons/raw.svg" width="16" alt="raw file arrow icon"> on each row. It opens the raw `.txt` that dirsearch wrote for that job.

It opens the **copy kept in the [Outputs mirror](#outputs-mirror)**, not the original file. This is on purpose: the original raw files are what feed OptiFuzz (it reads the results from them), so opening them from here could end up modifying them by accident. The copy is the one to read, search or edit freely.

- **Hover** the arrow to see the full path of that copy. If the project has no Outputs mirror yet, the tooltip tells you that you need to set one. If the job is still running (or paused), its copy does not exist yet and the tooltip says so: it is made when the job finishes.
- **Click** the arrow to open the file in the program your system uses for `.txt` files.
- **Right-click** the arrow and choose **Copy path** to copy the full path of the copy, file name included. The hover text disappears while the menu is open, so it never covers it, and it shows again the next time you hover the arrow.

This works the same in every details table: the Jobs list, and the **Fuzzed** tags in Results and Tree.

### Queue and concurrency

A target runs at most as many jobs at once as its **Concurrent fuzzes** setting allows. The rest stay QUEUED and start as soon as a running job finishes, pauses or is stopped.

### Output files

Each job keeps dirsearch's own raw output as a readable `.txt` file in the target's `outputs` folder (see [Where your data lives](#12-where-your-data-lives)). The name follows the pattern set in [Output filenames](#output-filenames), followed by the job id, with `__AUTH` when the job is marked authenticated. OptiFuzz reads the results from dirsearch's JSON report next to it. Deleting the job deletes both.

If the project has an [Outputs mirror](#outputs-mirror), a copy of each `.txt` is also saved in the folder you chose, when the job finishes. Deleting the job removes that copy too. The **Raw** column of the job's details opens that copy (see [The Raw column](#the-raw-column)).

---

## 6. Auto-pause and early stops

### Auto-pause on status codes

A target that starts answering `429` (too many requests) or `503` is asking you to slow down, and a firewall that notices the fuzzing may start answering `403` to everything. Put the codes you care about in **Auto-pause on status codes** and the job pauses on the first response with one of them. The `⚠ auto` tag shows which code did it when you hover it.

- dirsearch normally ends the whole scan at the first `429`. OptiFuzz turns that off so the job pauses instead.
- It also catches responses dirsearch hides from its own output (for example the `403` page that its wildcard detection filters out), by reading dirsearch's request log while the job runs.
- After a pause, nothing resumes by itself: check whether the target is down or blocking you, then resume when you are ready.

### Choosing what Resume does

When a job auto-paused on a specific request, the **Resume** button has a gear attached on its right <img src="images/icons/gear.svg" width="16" alt="gear icon">. Click the gear to see one option:

- **Repeat the word that paused it** (ticked by default): the word that triggered the pause is requested again. Use it when you think the answer was a block.
- **Unticked**: the job continues after that word without repeating it. Use it when that word really answers `403` and you were not blocked.

The gear shows an orange dot while the option is unticked, and the Resume tooltip says the word will be skipped. The choice is remembered for that job. Either way the job keeps pausing on every code you chose, so you can check each one by hand. The gear only appears on jobs auto-paused with version 1.1.5 or later.

### Jobs that stop early

A job is never reported as COMPLETED when dirsearch quit before the end of its wordlist. It is PAUSED at the position it really reached and tagged `⚠ stopped early`. Click the information button next to the tag to read why, then resume. Typical reasons are:

- Too many request errors in a row (dirsearch gives up after 75). The message adds how many requests failed and the most common error, for example `7x ReadTimeout`.
- dirsearch's own time limit for the target.
- Any other error that aborted the run, such as a connection reset while reading a response.
- The job ended well short of its total without saying why.

---

## 7. Results

The Results tab lists everything dirsearch found for the target, grouped by directory and updated live while jobs run.

### Reading the list

Each row shows the **status** code, the **size** of the response, two action icons, the **path** and the **redirect** it points to. Paths are indented under their parent directory, and the arrow beside a directory folds or unfolds it (the state is remembered). Next to a directory, `(10)` is how many direct children it has, and `(10 · 3 hidden)` means three of them are hidden by your [hide rules](#hide-rules).

Status codes are coloured by class: purple for 1xx, green for 2xx, cyan for 3xx, yellow for 401 and 403, grey for the other 4xx, orange for 429 and red for 5xx.

A redirect inside the same host shows only its path. A redirect to another host or port is shown as the full URL in orange, because it leaves the target and deserves a look. Hover a redirect to see its full address, or click it to open it.

### The NEW badge

**NEW** marks the findings you have not seen yet, so after a new run you only look at what changed. A finding counts as seen once it was on screen while you were on the tab. Rows you never reached, such as those further down the list that you did not scroll to, or those inside a folded directory, stay NEW.

### The Fuzzed tag

When a path has already been fuzzed, a **Fuzzed** tag appears next to it automatically, both in Results and in Tree. Click the tag to unfold the details of the jobs that fuzzed it: status, wordlist, extensions, headers, options, number of results, date and the **Raw** arrow (see [The Raw column](#the-raw-column)). The number of results is a link to those results.

### Actions on a path

- **Open it**: click the path to open its URL in your browser (chosen in [Settings](#browser-for-opening-results)).
- **Copy it**: right-click any path to copy its full path, such as `/admin/panel/users`, even though the row only shows its last part (`users`), indented under its parents. This works in Results, Tree and the job list.
- **Fuzz inside it**: the fuzz icon <img src="images/icons/fuzz.svg" width="16" alt="fuzz icon"> opens the [New Fuzz form](#where-to-start-one) under the row.
- **Add it to the Tree**: the tree icon <img src="images/icons/tree.svg" width="16" alt="tree icon"> adds the path to the Tree, and removes it if it is already there (it asks first, since the notes of that path would be lost).

### Search, filters and hide rules

All of these are remembered for each target, even after restarting the app.

- **Search path…** shows only the paths that contain the text.
- **Filters** has two options: **Fuzzed**, to show only paths that were fuzzed, and **Status**, to show only the codes you list, separated by commas (`200,301`).
- **Hide rules** keep the rows you do not want to see out of the list. Nothing is deleted: remove a rule and those rows come back.

#### Hide rules

A hide rule has up to four fields: **Status**, **Size**, **Parent path** and **Depth**. Fill the ones you need and click **+ Hide**. A field you leave empty matches anything (it acts as a wildcard), and a result is hidden when it matches every field you filled in:

- **Status** `404` only: hides every 404, whatever its size or where it is.
- **Status** `404` and **Size** `3KB`: hides the 404 responses of exactly 3KB, wherever they are.
- **Parent path** `/static/` only: hides everything under `/static/`, whatever its status or size.
- **Status** `403`, **Size** `3KB` and **Parent path** `/admin/`: hides only the 403 responses of 3KB under `/admin/`.

**Depth** says how many levels below the parent path the rule reaches: `1` (the default) means only its direct children, and `0` means everything under it, however deep. It only matters when a **Parent path** is set; without one, the rule applies to the whole list and Depth is ignored.

Remove a rule with the ✕ on its chip. The number on the **Hide rules** button shows how many rules are active.

---

## 8. Tree

The Tree is the short list of paths you decided matter. It is **not filled automatically**: only what you add appears, so it stays readable. The root `/` is always there and cannot be removed.

### Adding paths

There are two ways to add a path:

- **By hand**: type the path in the **Add path** field, for example `/api/internal`, and press Enter or click **+ Add path**.
- **From Results**: click the tree icon <img src="images/icons/tree.svg" width="16" alt="tree icon"> on a result. It keeps that result's status and size.

If you add a deep path such as `/api/v2/internal`, its parents (`/api`, `/api/v2`) also appear as rows so you can always see where it sits. You can star them, write a note on them or remove them like any other path.

### Working with the tree

- **Star** <img src="images/icons/star.svg" width="16" alt="star icon">: marks a path as interesting. It turns yellow <img src="images/icons/star-filled.svg" width="16" alt="filled star icon"> when marked.
- **Fuzz icon** <img src="images/icons/fuzz.svg" width="16" alt="fuzz icon">: opens the New Fuzz form under the row, like in Results.
- **Path**: click it to open it in your browser, right-click it to copy its full path. Directories fold and unfold, and the state is remembered.
- **Fuzzed tag**: appears automatically on paths that were fuzzed, with the wordlist and extensions used. Click it to unfold the details of those jobs.
- **Note**: write directly in the row; it saves when you click elsewhere and grows as you type.
- **Trash icon** <img src="images/icons/trash.svg" width="16" alt="trash icon">: removes the path and any paths under it, after asking. It never deletes jobs or results.

**Search path…** and **Filters** work as in Results. Filters has **Interesting**, to show only starred paths, and **Fuzzed**. **Export URLs (TXT)** is described in [Exporting](#export-urls-txt).

---

## 9. Fuzzing deeper

OptiFuzz is made for manual, step-by-step exploration and **never recurses on its own**. When a result looks worth exploring, you start that fuzz yourself, from the result or from the tree, so every request is a decision. Each exploration is its own job with its own options.

dirsearch has its own recursion flags (`-r`, `-R` and others). You can write them in **Custom options**, and the job records everything it finds, nested paths included. However, **recursive options are not officially supported**: progress and Pause/Resume are designed for a single pass over the wordlist, so with recursion the progress only reflects the first level, and a resumed job cannot restore dirsearch's recursion queue.

---

## 10. Exporting

Exports let you hand your findings to someone else, or keep a snapshot, without needing OptiFuzz. The HTML reports are fully interactive: the search, the filters, the hide rules and the folding of directories all keep working inside the file, so whoever opens it can explore it the way you do. They need no internet and no backend.

### Saving an export

In the desktop app, clicking an export button opens a **Save As** window over OptiFuzz, starting in your Downloads folder. Once the file is saved, a small notice appears at the **top right, inside the OptiFuzz window**, with the file name and two buttons:

- **Open file**: opens the file. An HTML report opens in the browser chosen in [Settings](#browser-for-opening-results) and that browser is brought to the front, the same way the URLs you open from Results are. Any other file opens with the program your system uses for it.
- **Show in folder**: shows the file in your file manager.

The notice closes by itself after a few seconds, or with its ×. (Before this, the downloads popup of the web view could open outside the window, on another screen.) In a plain browser tab, the browser downloads the file as usual.

### Export target (HTML)

The **Export target (HTML)** button, at the top right of the target workspace, downloads one HTML file with that target's jobs, results and tree. The file opens with that target's saved hide rules, search and filters, and the reader can change them. The same button is available on every tab.

### Export URLs (TXT)

On the Tree tab, **Export URLs (TXT)** (next to the button above) downloads a plain text file with the absolute URL of every path in the tree, one per line. It is meant to be imported into other tools such as scanners and crawlers.

### Export all targets

On the targets page, **Export all targets** downloads a report of every target in the project at once. The small arrow on its right chooses the format, and the choice is remembered. Choosing a format only changes the button: the file is downloaded when you click the button itself.

- **Separate reports (.zip)**: a zip file with one HTML report per target.
- **Combined report (.html)**: one HTML page with every target, with navigation between them.

In both formats each target keeps its own saved hide rules, search and filters, exactly as in an individual export.

In every export, the job details have the **Raw** column too. A report is meant to be shared and cannot open files on another computer, so it never contains a path from your computer: hovering the arrow shows only the **name** of the raw file. Click the arrow, or right-click it, and choose **Copy filename** to copy that name.

### Outputs mirror

The outputs mirror keeps a second copy of every job's raw `.txt` output in a folder you choose, outside OptiFuzz. It is set per project, in the **Outputs mirror** section at the top of the project's targets page: type the folder path or pick it with the folder button <img src="images/icons/folder.svg" width="16" alt="folder icon">.

- Every job's output is copied there when the job finishes, in a subfolder named after its target.
- When you set the folder, the outputs of jobs that already finished are copied too.
- If you later change the folder, the files already mirrored move to the new one automatically.
- Deleting a job also removes its mirrored copy.
- Leave the field empty to turn the mirror off. Files already copied are left where they are; OptiFuzz does not delete them.

The original files inside OptiFuzz are never moved or changed, whatever you do here. The mirror is also what the **Raw** column of the job details opens, so that you never open the originals by accident (see [The Raw column](#the-raw-column)).

---

## 11. Settings

Click the gear icon at the top right to open the settings. Each entry opens its own page, and the back arrow returns to the list.

### Browser for opening results

Chooses the browser that opens a URL when you click a path or a redirect. The browsers found on your computer are listed: pick one. If yours is not listed, type the path of its executable and click **Use**.

### Output filenames

The pattern that names each job's raw output file. You can use `{target}` (the target's name), `{port}` and `{path}` (the fuzzed path, with slashes turned into underscores).

The default is `dirsearch_{target}_{port}`, shown in the field as a hint. Type your own pattern to replace it, or empty the field to go back to the default. OptiFuzz always adds the job's id at the end of the name so two jobs never overwrite each other's file. An example of the final name is shown below the field. The setting applies to jobs created after you save it, and it is kept when you close the app and when you update it.

**File names that are too long.** Adding `{path}` makes names as long as the path you fuzzed, and deep paths can produce very long names. On Windows, long names are a problem when you compress and move the files: a `.zip` made with the built-in tools can fail to extract at the destination, because Windows limits full paths to about 260 characters. Compressing and extracting with 7-Zip handles those names correctly. Linux has no such overall limit, so this mostly affects Windows. The safest choice is not to include `{path}` in the pattern, which is why the default leaves it out.

### Default extensions (Force extensions)

When a fuzz has no extensions typed, dirsearch does not use none: it falls back to its own built-in list (`php,asp,aspx,jsp,html,htm`). This setting decides what happens in that case, and it matters most with **Force extensions** on, where every word is also tried with each of those extensions.

**After a fresh installation nothing is configured, so dirsearch's own list is used.** The New Fuzz form reminds you with *Default extensions activated* in the Extensions field, and shows the list when you hover it or click inside it. A job's details also list the default extensions it used, marked `(default)`.

To change it:

- Leave the field empty to keep dirsearch's built-in list, exactly like running dirsearch by hand.
- Tick **Use no extensions instead** to use no extensions at all.
- Type your own comma-separated list to use it instead.

Extensions you type in a fuzz always take priority over this setting.

### Wordlists folder

The folder the wordlist picker reads. Leave it empty to use the `wordlists` folder that comes with OptiFuzz, or type the path of a folder you already have (or choose it with the folder button). Its subfolders appear as groups in the picker. OptiFuzz only reads from it and never changes it.

### Desktop app port

The local port the app listens on, always on `127.0.0.1`. The default is 58947; leave the field empty to use it. A change takes effect the next time you start OptiFuzz.

### Display zoom (Linux)

Makes the whole interface bigger or smaller, from 50 to 300 percent. It applies immediately and is remembered. Leave it empty for automatic. On Windows the app follows the display scaling of the system.

### Updates

**Check for updates** compares your copy with the repository you installed it from. If a newer version exists, **Update now** downloads it, refreshes the dependencies and restarts OptiFuzz by itself. Running and queued jobs are paused first, so resume them afterwards. Your projects, targets, jobs and wordlists are kept, because they are stored outside the program folder. It works on any copy installed with `git clone`, and it refuses to overwrite files you modified yourself.

---

## 12. Where your data lives

Your data is stored outside the program folder, so it survives updates. The data folder is:

- **Windows**: `%APPDATA%\OptiFuzz\workspace` (usually `C:\Users\<you>\AppData\Roaming\OptiFuzz\workspace`)
- **Linux**: `~/.local/share/OptiFuzz/workspace`

Everything below is inside that folder:

```text
workspace/
├── app_settings.json                      app-wide settings
└── projects/
    └── <project>/
        ├── project.json
        └── targets/
            └── <target>/
                ├── target.json
                ├── tree.json              the Tree
                ├── hide_rules.json        the Results hide rules
                ├── results_seen.json      what you have already seen (NEW badge)
                ├── outputs/               the raw .txt and JSON report of each job
                └── jobs/                  one file per job, plus its working files
```

Folder names for projects, targets and jobs are generated by OptiFuzz. Everything is stored as plain JSON and dirsearch's untouched output, written so that a crash cannot corrupt it.

---

*Back to the [README](../../README.md).*
