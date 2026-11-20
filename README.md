# 24hrs-of-bugs

Scan Launchpad for newly opened bugs filed against Ubuntu kernel targets and
render static HTML reports for the last **24 hours**, **7 days**, **30 days**
and **90 days**.

This is a rewrite of the original `kteam-tools/stable/24hrs-of-bugs` email
reporter. It runs as a time-triggered [Forgejo](https://forgejo.org/) action
and publishes the rendered pages to GitHub Pages.

## Components

The tooling is split into two scripts plus a shared module:

- **`fetch-bugs.py`** — the fetcher: scans Launchpad and updates the JSON store.
- **`render-html.py`** — the renderer: turns the JSON store into HTML. It does
  no network access.
- **`common.py`** — the shared store, timestamp helpers and window definitions.

## Branches

- **`tools`** (this branch) — the tooling: `fetch-bugs.py`, `render-html.py`,
  `common.py`, the vendored `cktools_data` package, and `requirements.txt`.
- **`main`** — the generated output and persistent state: `index.html` plus one
  page per window, `last-run.json`, `bugs/<id>.json`, the Forgejo workflow and
  `.nojekyll`.

The workflow (on `main`) checks the `tools` branch out as a git worktree, runs
the fetcher then the renderer against the `main` working tree, commits the
refreshed pages and state, and pushes `main` to both Forgejo and the GitHub
Pages repository.

## How it works

### Fetcher (`fetch-bugs.py`)

1. Read `last-run.json`. If it is missing, seed the store from
   `now - 90 days`; otherwise scan incrementally from
   `last_run - 15 minutes` (the fudge factor absorbs clock skew and
   Launchpad's eventual consistency).
2. For every unique kernel target (`KernelTargets.all`), run
   `searchTasks(status=..., modified_since=since, omit_duplicates=False)`
   with retries. Each bug found is reconciled once per run, no matter how
   many targets it was found through.
3. Reconcile one JSON file per bug, keyed by bug id: the bug's full
   `bug_tasks` list is re-examined and every task on a monitored target is
   recorded with its own `series` (empty for unversioned distribution tasks
   and projects), `status`, `importance`, `assignee` and `date_created`.
   Tasks that no longer exist on Launchpad (bug retargeted, task deleted) are
   preserved with the synthetic status `Removed` as an audit trail.
4. Duplicates: a bug with `duplicate_of` set is recorded with the master bug
   id; the renderer displays it with the artificial status `Duplicate`.
5. When run with Launchpad credentials, non-duplicate bugs with an active
   task created in the last 24 hours are tagged `kernel-daily-bug` (and
   `linux-firmware-daily-bug` for firmware packages).
6. Prune bug files whose newest target task is older than 90 days.
7. Advance `last-run.json` **only if every target scanned successfully**, so a
   partial failure is recovered on the next run.

### Renderer (`render-html.py`)

Renders all pages purely from the JSON store. A bug appears on a window page if
**any** of its target tasks was created within that window; windows are
cumulative (24h ⊆ 7d ⊆ 30d ⊆ 90d).

The store holds raw per-task facts only; the renderer derives the display:

- **Brief mode** (default): one row per bug. Status, importance, series and
  assignee show the shared value of the active tasks, or `multiple` (with a
  tooltip) when they differ. Inactive targets (closed, invalidated or
  `Removed`) are shown struck through.
- **Verbose mode**: each multi-task bug expands into a per-task detail table
  (target, series, status, importance, assignee, creation date).

## Usage

```sh
pip install -r requirements.txt

# Fetch new bugs into the store, then render the HTML alongside it
./fetch-bugs.py  --state-dir /path/to/main-checkout
./render-html.py --output    /path/to/main-checkout

# Re-render only, no Launchpad access
./render-html.py --output /path/to/main-checkout
```

`fetch-bugs.py` options:

- `--state-dir DIR` — directory for the store (`bugs/`, `last-run.json`).
- `--credentials-file FILE` — Launchpad credentials; required for tagging.
- `--no-tag` — never tag bugs even with credentials.
- `--seed-days N` / `--fudge-minutes N` — tune the first-run and overlap windows.
- `--datetime 'YYYY-MM-DD HH:MM'` — treat a fixed UTC time as "now" (testing).

`render-html.py` options:

- `--output DIR` — directory to write the HTML into.
- `--state-dir DIR` — store location if separate from `--output` (testing).
- `--datetime 'YYYY-MM-DD HH:MM'` — treat a fixed UTC time as "now" (testing).

## Notes

- Status fields are a snapshot from when a bug was last seen by a scan.
  Incremental runs refresh recently-modified bugs, but a bug untouched since
  ingest keeps its last-seen status.
- `cktools_data` is vendored; see `VENDORED.md` for provenance and how to refresh.
