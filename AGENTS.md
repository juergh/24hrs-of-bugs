# Agent Instructions & Repository Guidelines

This document provides conventions, architecture rules, and development guidelines for automated agents working in this repository.

## Dual-Branch Architecture

This repository maintains two disconnected branch histories:

- **`tools`**: Contains all Python code, tests, templates, static assets, and vendored libraries. All tooling development, refactoring, and test execution must happen on this branch.
- **`main`**: The deployed production branch and persistent state. Contains:
  - Static HTML reports (`index.html`, `24-hours.html`, `7-days.html`, etc.).
  - Bug store cache (`bugs/<id>.json`) and scan checkpoint (`last-run.json`).
  - Forgejo CI workflow (`.forgejo/workflows/24hrs-of-bugs.yaml`).

Never commit Python tool sources directly to `main`. When testing HTML generation from `main`, mount `tools` as a temporary git worktree (`git worktree add tools tools`), render, and remove the worktree.

## Commit Message Guidelines

Every commit must follow these rules:

1. **Subject Line**:
   - Prefix with the subsystem or component: `render-html:`, `fetch-bugs:`, `tests:`, `workflow:`, `reports:`, or `common:`.
   - Use the imperative mood, concise and lowercase after the colon.
2. **Body**:
   - Focus strictly on the **why** (problem or motivation) and the **how** (architectural approach or solution).
   - Use natural language paragraphs. **Do not use bulleted lists**.
   - **Limit line length to 72 characters**.
   - Do not repeat details that can be learned directly from reading the diff.
   - Keep it concise without unnecessary filler.
3. **No Signed-off-by**:
   - **Never add a `Signed-off-by:` trailer**. Signing off commits is strictly reserved for human contributors.

## Templates & Front-End Invariants

- **Self-Contained Output**: The rendered HTML pages must remain 100% self-contained single files. `templates/base.html` must inline CSS from `static/style.css` and JavaScript from `static/filter.js`. Do not generate external asset links (`<link rel="stylesheet">` or `<script src="...">`) that require separate file deployment.
- **HTML Escaping**: Be vigilant about raw HTML entities. Do not pass string literals containing entities (e.g. `&mdash;` or `&middot;`) into escaping functions or auto-escaped template variables to prevent double-escaping (`&amp;mdash;`).
- **Jinja2 Settings**: Always configure Jinja2 with `trim_blocks=True` and `lstrip_blocks=True` to maintain clean whitespace in generated output.

## Bug Store & Fetcher Invariants

- **Atomic Writes**: Any writes to `bugs/<id>.json` or `last-run.json` must be atomic (write to a `.tmp` file and rename via `os.replace`).
- **Scan Failure Recovery**: Never advance `last-run.json` if any target fails during a scan. A partial scan must leave the timestamp intact so the next scheduled run re-scans the missing window.
- **Cumulative Windows**: A bug qualifies for a reporting window if *any* of its target tasks were created within that window (24h ⊆ 7d ⊆ 30d ⊆ 90d).

## Testing & Quality Assurance

- Always run `pytest` directly from the repository root when working on the `tools` branch.
- Run `mypy` directly from the repository root to verify static typing across all tooling and test files.
- Keep `pyproject.toml` and `sys.path` defenses intact so test collection never fails regardless of runner invocation.
- Do not modify files in `cktools_data/` directly; refer to `VENDORED.md` for instructions on refreshing from upstream `kteam-tools`.
