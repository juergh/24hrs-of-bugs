# 24hrs-of-bugs

Static dashboard tracking newly opened Ubuntu kernel bugs filed on Launchpad across the last **24 hours**, **7 days**, **30 days**, and **90 days**.

This project is a modern replacement for the legacy `kteam-tools/stable/24hrs-of-bugs` email reporter. It runs automatically via Forgejo Actions and publishes the generated reports to GitHub Pages.

## Branches

This repository maintains a two-branch architecture:

- **`main`** (this branch) — Published static site and persistent state:
  - `index.html`, `24-hours.html`, `7-days.html`, `30-days.html`, `90-days.html`: Rendered report pages.
  - `bugs/*.json`: Per-bug JSON cache storing metadata and target creation dates.
  - `last-run.json`: Timestamp of the last successful full scan.
  - `.forgejo/workflows/`: Automated daily fetch and render pipeline.
- **`tools`** — Tooling and Python source code:
  - `fetch-bugs.py`: Queries Launchpad for kernel bug updates and maintains the JSON store.
  - `render-html.py`: Generates the static HTML reports from the JSON store.
  - `common.py`: Shared store definitions, window configurations, and date helpers.
  - `cktools_data/`: Vendored kernel targets and series metadata from `kteam-tools`.
  - `tests/`: Test suite for store and report rendering logic.

## How it works

1. **Scheduled Scan**: A daily Forgejo Action (01:00 UTC) checks out `main` and mounts the `tools` branch as a git worktree.
2. **Fetch**: `fetch-bugs.py` scans Launchpad incrementally since the last run (with a 15-minute overlap to absorb clock skew) across all Ubuntu kernel targets, tagging newly opened bugs.
3. **Render**: `render-html.py` rebuilds all static HTML pages purely from the local JSON store (no network requests).
4. **Publish**: State changes and generated HTML files are committed back to `main` and pushed to GitHub Pages.

## Developing & Testing

To work on the fetcher, renderer, or tests, switch to the `tools` branch:

```sh
git checkout tools
pip install -r requirements.txt
pytest
```

Refer to the `README.md` on the `tools` branch for CLI parameters and local execution instructions.
