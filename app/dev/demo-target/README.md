# Demo target

A tiny, local web server for trying OptiFuzz without touching anything real. It serves a small fake
website with directories, files and a set of routes that answer with **different status codes and
body sizes** (200, 301, 302, 403, 404 with custom pages, 500), at several directory levels, so
every part of the app (Results, FUZZ TREE, filters, status colours, redirects, job states) has
something to show.

It listens on **`127.0.0.1:8910` only** (never on your network) and serves static, fake content
(`.env` holds `demo_not_real`, the "database dump" is two lines of text). Nothing is executed.

## Start it

From the repository root, with any Python 3.9 or newer (on some Linux distributions the command is
`python3`):

```bash
python app/dev/demo-target/demo_server.py
```

The server keeps running in that terminal (leave it open). To check that it is up, open
<http://127.0.0.1:8910/> in a browser.

## Stop it

When you are done, press `Ctrl+C` in the terminal where it is running.

## What is on it

Static files and directories (status 200 unless noted; directories answer `301` to their trailing
slash):

```text
/                      index.html, login.html, dashboard.html, config.html, test.html,
                       admin.html, api.html, backup.html, robots.txt, readme.txt, phpinfo.php,
                       .env, server-status
/admin/                index.html, users.html, settings.html, logs.html
/admin/panel/          index.html, stats.html
/api/v1/               status.html, users.html, orders.html, health.html
/api/v2/               users.html, orders.html, health.html
/backup/               site-backup.sql, db.sql.bak
/static/css/           style.css, admin.css
/static/js/            app.js, main.js, vendor.js
/images/               logo.png
/uploads/              readme.txt, avatars/default.txt
/wp-admin/             index.html, login.html, options.html, plugins/index.html
```

Routes that deliberately answer something else, so status handling can be checked by eye:

| Path | Status | Notes |
|---|---|---|
| `/forbidden`, `/forbidden-secret`, `/forbidden-vault` | 403 | three different body sizes |
| `/missing-page`, `/old-report.pdf`, `/legacy-api` | 404 | custom 404 pages of different sizes (dirsearch's same-size-404 detection) |
| `/moved`, `/old-login` | 302 | to `/index.html` and `/login.html` |
| `/old-port-service` | 302 | to the same host on another port (`127.0.0.1:9090`) |
| `/external-docs` | 302 | to another host (`https://example.com/docs`); leave "follow redirects" off |
| `/crash`, `/broken-export` | 500 | |
| `/admin/secret`, `/admin/export` | 403 | inside a subdirectory |
| `/admin/old` | 302 | to `/admin/panel/index.html` |
| `/api/v1/internal` | 403 | |
| `/api/v1/crash`, `/api/v2/debug` | 500 | |
| `/api/v2/beta` | 404 | custom page |
| `/static/private`, `/wp-admin/staging` | 403 | |
| `/backup/old` | 302 | to `/backup/db.sql.bak` |

## Try the app against it

The repository ships one example wordlist, `wordlists/test_wordlist.txt`. It contains every name
above plus plenty of common words that do **not** exist, so you see both hits and misses.

1. Start the demo server (above) and OptiFuzz.
2. Create a **Project** (any name) and, inside it, a **Target**: base URL `http://127.0.0.1` and,
   in the separate **Port** field next to it, `8910` (do not type the port into the URL).
3. **JOBS tab -> New Fuzz**: path `/`, wordlist `test_wordlist.txt`, extensions
   `html,php,txt,sql,bak` and, under *Advanced options*, turn on **Force extensions** (without it
   dirsearch only appends extensions to wordlist entries that contain `%EXT%`). Click **Fuzz**.
4. Watch the job run in the table (progress, rate, results). Open **RESULTS**: hits are coloured
   by status code, redirects show their destination, and new entries carry a **NEW** badge until
   you have seen them.
5. Go deeper: in RESULTS or **TREE**, use the lightning icon on `/admin/`, `/api/` or `/wp-admin/`
   to fuzz that path with the same wordlist. The children (`/api/v1/`, `/api/v2/`,
   `/admin/panel/`, ...) fill in the tree.
6. Try the job controls: start another job on `/` with a low thread count (1-2), then **Pause**,
   **Resume** and **Stop** it. Pausing resumes from where it stopped, not from the start.
7. See a **failed** job: start one with `--this-flag-does-not-exist` in *Advanced options ->
   Custom options*. It ends as FAILED and the info button on its row shows why. (A stopped job
   and a failed job are different states.)
8. Use the TREE: mark a path as interesting, add a note to a path, add a path by hand.
9. Click **Export target (HTML)** for a single, self-contained report you can open offline.

To reset the demo, delete the Project in the app; nothing about the demo server itself is stored.
