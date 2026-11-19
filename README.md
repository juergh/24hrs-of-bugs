# 24hrs-of-bugs

Scan Launchpad for newly opened bugs filed against Ubuntu kernel targets and
render static HTML reports for the last **24 hours**, **7 days**, **30 days**
and **90 days**.

This is a rewrite of the original `kteam-tools/stable/24hrs-of-bugs` email
reporter. It runs as a time-triggered [Forgejo](https://forgejo.org/) action
and publishes the rendered pages to GitHub Pages.

## Branches

- **`tools`** (this branch) — the tooling: `24hrs-of-bugs.py`, the vendored
  `cktools_data` package, and `requirements.txt`.
- **`main`** — the generated output and persistent state: `index.html` plus one
  page per window, `last-run.json`, `bugs/<id>.json`, the Forgejo workflow and
  `.nojekyll`.

The workflow (on `main`) checks the `tools` branch out as a git worktree, runs
the tool against the `main` working tree, commits the refreshed pages and
state, and pushes `main` to both Forgejo and the GitHub Pages repository.

## How it works

1. Read `last-run.json`. If it is missing, seed the store from
   `now - 90 days`; otherwise scan incrementally from
   `last_run - 15 minutes` (the fudge factor absorbs clock skew and
   Launchpad's eventual consistency).
2. For every unique kernel target (`KernelTargets.all`), run
   `searchTasks(status=..., modified_since=since)` with retries.
3. Upsert one JSON file per bug, keyed by bug id. A bug raised against several
   targets is stored **once**, recording each target's task creation date.
   Re-seen bugs have their title/series/importance/status/assignee refreshed.
4. When run with Launchpad credentials, bugs with a task created in the last 24
   hours are tagged `kernel-daily-bug` (and `linux-firmware-daily-bug` for
   firmware packages).
5. Prune bug files whose newest target task is older than 90 days.
6. Advance `last-run.json` **only if every target scanned successfully**, so a
   partial failure is recovered on the next run.
7. Render all pages purely from the JSON store.

A bug appears on a window page if **any** of its target tasks was created
within that window; windows are cumulative (24h ⊆ 7d ⊆ 30d ⊆ 90d).

## Usage

```sh
pip install -r requirements.txt

# Incremental/seed scan + render into the current directory
./24hrs-of-bugs.py --output /path/to/main-checkout

# Re-render only, no Launchpad access
./24hrs-of-bugs.py --output /path/to/main-checkout --no-scan
```

Key options:

- `--output DIR` — root for state (`bugs/`, `last-run.json`) and rendered HTML.
- `--state-dir DIR` — keep state separate from the HTML output (testing).
- `--credentials-file FILE` — Launchpad credentials; required for tagging.
- `--no-tag` — never tag bugs even with credentials.
- `--no-scan` — skip Launchpad and only re-render from the store.
- `--seed-days N` / `--fudge-minutes N` — tune the first-run and overlap windows.
- `--datetime 'YYYY-MM-DD HH:MM'` — treat a fixed UTC time as "now" (testing).

## Notes

- Status fields are a snapshot from when a bug was last seen by a scan.
  Incremental runs refresh recently-modified bugs, but a bug untouched since
  ingest keeps its last-seen status.
- `cktools_data` is vendored; see `VENDORED.md` for provenance and how to refresh.
