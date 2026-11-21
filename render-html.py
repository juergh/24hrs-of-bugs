#!/usr/bin/env python3
#
# Render the 24hrs-of-bugs JSON store into a single static HTML report.
#
# This is the "renderer" half of 24hrs-of-bugs; populating the store from
# Launchpad is done separately by fetch-bugs.py. It performs no network access.
#
# The store holds raw per-task facts only; all display-level aggregation
# (shared value vs "multiple", active vs inactive targets) is derived here.
# The report is one page containing every bug in the store; the time windows
# (24 hours, 7 days, 30 days, 90 days) are client-side date-range presets.

import logging
import os
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from datetime import UTC, datetime, timedelta
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from common import CLOSED_STATUSES, WINDOWS, Store, iso, parse_iso

log = logging.getLogger("render-html")

LP_BUG_URL = "https://bugs.launchpad.net/bugs/"

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_HERE, "templates")
STATIC_DIR = os.path.join(_HERE, "static")

_CLOSED = set(CLOSED_STATUSES)


def _load_assets() -> tuple[str, str]:
    with open(os.path.join(STATIC_DIR, "style.css"), encoding="utf-8") as fh:
        css = fh.read()
    with open(os.path.join(STATIC_DIR, "filter.js"), encoding="utf-8") as fh:
        js = fh.read()
    return css, js


def _bug_in_window(record: dict[str, Any], now: datetime, days: int) -> datetime | None:
    """Newest target-task creation date within the window, or None.

    A bug is in window W if ANY of its target tasks was created within W, so
    a bug retargeted onto a new kernel shows up in the fresh window. Windows
    are cumulative (24h ⊆ 7d ⊆ 30d ⊆ 90d).
    """
    cutoff = now - timedelta(days=days)
    hits = [parse_iso(t["date_created"]) for t in record["targets"]]
    hits = [h for h in hits if h >= cutoff]
    if not hits:
        return None
    return max(hits)


def _active_targets(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Tasks with ongoing work: not closed and the bug is not a duplicate."""
    if record.get("duplicate_of") is not None:
        return []
    return [t for t in record.get("targets", []) if t.get("status") not in _CLOSED]


def _field(pool: list[dict[str, Any]], key: str) -> dict[str, str]:
    """Aggregate one task field for brief mode.

    The shared value when every task in the pool agrees, else "multiple" with
    a tooltip listing the distinct values.
    """
    values = [str(t.get(key) or "") for t in pool]
    if values and all(v == values[0] for v in values):
        return {"value": values[0], "tooltip": ""}
    distinct = []
    for v in values:
        if v and v not in distinct:
            distinct.append(v)
    return {"value": "multiple", "tooltip": ", ".join(distinct)}


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    """Derive the brief-mode display fields from the raw per-task data."""
    targets = record.get("targets", [])
    pool = _active_targets(record) or targets
    brief: dict[str, Any] = {
        "importance": _field(pool, "importance"),
        "assignee": _field(pool, "assignee"),
        "series": _field(pool, "series"),
    }
    if record.get("duplicate_of") is not None:
        brief["status"] = {"value": "Duplicate", "tooltip": ""}
    else:
        brief["status"] = _field(pool, "status")
    return brief


def _display_targets(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Unique target names for the Targets column, flagging inactive ones.

    A name is inactive when the bug has no active task on it (the task was
    closed, invalidated or removed/retargeted).
    """
    active_names = {t["name"] for t in _active_targets(record)}
    by_name: dict[str, dict[str, Any]] = {}
    for t in record.get("targets", []):
        name = t["name"]
        inactive = name not in active_names
        if name not in by_name or not inactive:
            by_name[name] = {"name": name, "inactive": inactive}
    return [by_name[name] for name in sorted(by_name)]


def _detail_targets(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-task rows for verbose mode, with an active flag and display date."""
    duplicate = record.get("duplicate_of") is not None
    details = []
    for t in record.get("targets", []):
        details.append(
            {
                **t,
                "active": not duplicate and t.get("status") not in _CLOSED,
                "date_opened": parse_iso(t["date_created"]).strftime("%Y-%m-%d %H:%M"),
            }
        )
    return details


def _rows(bugs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Every bug in the store as a display row, newest qualifying task first."""
    retention = WINDOWS[-1][2]
    rows: list[dict[str, Any]] = []
    for record in bugs:
        newest = _bug_in_window(record, now, retention)
        if newest is None:
            continue
        targets = record.get("targets", [])
        rows.append(
            {
                "newest": newest,
                "opened_iso": iso(newest),
                "id": record["id"],
                "title": record.get("title", ""),
                "duplicate_of": record.get("duplicate_of"),
                "brief": _brief(record),
                "active": bool(_active_targets(record)),
                "series_attr": " ".join(sorted({t.get("series", "") for t in targets if t.get("series")})),
                "targets": _display_targets(record),
                "targets_attr": " ".join(sorted({t["name"] for t in targets})),
                "details": _detail_targets(record),
            }
        )
    rows.sort(key=lambda r: r["newest"], reverse=True)
    return rows


def _counts(rows: list[dict[str, Any]], now: datetime) -> dict[str, int]:
    """Initial per-window bug counts (the client recomputes them live)."""
    counts: dict[str, int] = {}
    for slug, _, days in WINDOWS:
        cutoff = now - timedelta(days=days)
        counts[slug] = sum(1 for r in rows if r["newest"] >= cutoff)
    return counts


def render(store: Store, now: datetime, output_dir: str) -> None:
    bugs = list(store.all_bugs())

    css, js = _load_assets()
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    generated_iso = iso(now)

    rows = _rows(bugs, now)
    series_list = sorted({s for r in rows for s in r["series_attr"].split()})
    has_unspecified_series = any(not r["series_attr"] for r in rows)

    html = env.get_template("index.html").render(
        rows=rows,
        counts=_counts(rows, now),
        windows=WINDOWS,
        series_list=series_list,
        has_unspecified_series=has_unspecified_series,
        lp_bug_url=LP_BUG_URL,
        generated_at=generated_at,
        generated_iso=generated_iso,
        css=css,
        js=js,
    )
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


def main() -> None:
    parser = ArgumentParser(
        description="Render the 24hrs-of-bugs JSON store into a static HTML report.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", default=".", help="Directory to write the rendered HTML into.")
    parser.add_argument("--state-dir", help="Directory holding the store (defaults to --output).")
    parser.add_argument("--datetime", help="Treat this UTC 'YYYY-MM-DD HH:MM' as 'now' (for testing).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.datetime:
        now = datetime.strptime(args.datetime + ":00+0000", "%Y-%m-%d %H:%M:%S%z")
    else:
        now = datetime.now(UTC)

    output_dir = os.path.abspath(args.output)
    state_dir = os.path.abspath(args.state_dir) if args.state_dir else output_dir
    os.makedirs(output_dir, exist_ok=True)

    store = Store(state_dir)
    render(store, now, output_dir)
    log.info("II: Rendered report into %s (generated %s)", output_dir, iso(now))


if __name__ == "__main__":
    main()
