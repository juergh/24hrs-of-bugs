#!/usr/bin/env python3
#
# Render the 24hrs-of-bugs JSON store into static HTML reports for the 24 hour,
# 7 day, 30 day and 90 day windows.
#
# This is the "renderer" half of 24hrs-of-bugs; populating the store from
# Launchpad is done separately by fetch-bugs.py. It performs no network access.

import logging
import os
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from datetime import datetime, timedelta, timezone
from html import escape

from common import WINDOWS, Store, iso, parse_iso

log = logging.getLogger("render-html")

PAGE_CSS = """
:root { --orange: #E95420; --aubergine: #772953; --ink: #111; --muted: #666; }
* { box-sizing: border-box; }
body {
  margin: 0; color: var(--ink);
  font-family: Ubuntu, "Ubuntu", -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: #fff; line-height: 1.5;
}
header.k-head {
  background: var(--aubergine); color: #fff; padding: 1rem 2rem;
  display: flex; align-items: center; gap: 0.75rem;
}
header.k-head .k-logo {
  width: 1.75rem; height: 1.75rem; background: var(--orange);
  border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; color: #fff; font-weight: 700; flex: none;
}
header.k-head h1 { font-size: 1.1rem; font-weight: 400; margin: 0; }
main { padding: 1.5rem 2rem 3rem; max-width: 1200px; margin: 0 auto; }
h2 { font-weight: 300; font-size: 1.6rem; margin: 0 0 0.25rem; }
.k-sub { color: var(--muted); margin: 0 0 1.5rem; font-size: 0.9rem; }
nav.k-tabs { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0 1.5rem; }
nav.k-tabs a {
  text-decoration: none; color: var(--ink); padding: 0.4rem 0.9rem;
  border: 1px solid #cdcdcd; border-radius: 2px; font-size: 0.9rem;
}
nav.k-tabs a.is-active { background: var(--orange); color: #fff; border-color: var(--orange); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
thead th {
  text-align: left; border-bottom: 2px solid var(--orange);
  padding: 0.5rem 0.6rem; font-weight: 500;
}
tbody td { padding: 0.5rem 0.6rem; border-bottom: 1px solid #e5e5e5; vertical-align: top; }
tbody tr:hover { background: #faf7f8; }
td.k-id a { color: var(--orange); text-decoration: none; font-variant-numeric: tabular-nums; }
td.k-id a:hover { text-decoration: underline; }
.k-targets { color: var(--muted); font-size: 0.82rem; }
.k-empty { color: var(--muted); padding: 2rem 0; }
.k-cards { display: flex; flex-wrap: wrap; gap: 1rem; margin: 1.5rem 0; }
.k-card {
  flex: 1 1 10rem; border: 1px solid #cdcdcd; border-radius: 2px;
  padding: 1rem 1.25rem; text-decoration: none; color: var(--ink);
}
.k-card:hover { border-color: var(--orange); }
.k-card .k-count { font-size: 2rem; font-weight: 300; color: var(--orange); display: block; }
.k-card .k-label { color: var(--muted); font-size: 0.9rem; }
footer { color: var(--muted); font-size: 0.8rem; padding: 1.5rem 2rem; border-top: 1px solid #e5e5e5; }
""".strip()

LP_BUG_URL = "https://bugs.launchpad.net/bugs/"


def _head(title):
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{PAGE_CSS}\n</style>\n</head>\n<body>\n"
        "<header class=\"k-head\"><span class=\"k-logo\">U</span>"
        "<h1>Ubuntu kernel &mdash; newly opened bugs</h1></header>\n"
    )


def _tabs(active):
    items = ['<a href="index.html"%s>Overview</a>' % (' class="is-active"' if active == "index" else "")]
    for slug, label, _ in WINDOWS:
        cls = ' class="is-active"' if active == slug else ""
        items.append(f'<a href="{slug}.html"{cls}>{escape(label.replace("last ", ""))}</a>')
    return '<nav class="k-tabs">\n  ' + "\n  ".join(items) + "\n</nav>\n"


def _footer(now):
    return (
        "</main>\n<footer>Generated "
        f"{escape(now.strftime('%Y-%m-%d %H:%M UTC'))} &middot; "
        "data from <a href=\"https://bugs.launchpad.net/\">Launchpad</a></footer>\n"
        "</body>\n</html>\n"
    )


def _bug_in_window(record, now, days):
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


def _window_bugs(bugs, now, days):
    """Bugs in the window as (newest qualifying date, record), newest first."""
    rows = []
    for record in bugs:
        newest = _bug_in_window(record, now, days)
        if newest is not None:
            rows.append((newest, record))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def render(store, now, output_dir):
    bugs = list(store.all_bugs())
    counts = {}

    for slug, label, days in WINDOWS:
        rows = _window_bugs(bugs, now, days)
        counts[slug] = len(rows)
        _write_window_page(os.path.join(output_dir, f"{slug}.html"), slug, label, rows, now)

    _write_index(os.path.join(output_dir, "index.html"), counts, now)


def _write_window_page(path, slug, label, rows, now):
    title = f"Kernel bugs opened in the {label}"
    out = [_head(title), "<main>\n", f"<h2>Bugs opened in the {escape(label)}</h2>\n"]
    out.append(f'<p class="k-sub">{len(rows)} bug(s) &middot; a bug raised against '
               "several kernel targets is listed once.</p>\n")
    out.append(_tabs(slug))

    if not rows:
        out.append('<p class="k-empty">No bugs opened in this window.</p>\n')
    else:
        out.append("<table>\n<thead>\n<tr>"
                   "<th>Bug</th><th>Title</th><th>Series</th>"
                   "<th>Importance</th><th>Status</th><th>Assignee</th><th>Targets</th>"
                   "</tr>\n</thead>\n<tbody>\n")
        for _, record in rows:
            targets = ", ".join(escape(t["name"]) for t in record["targets"])
            series = escape(record.get("series") or "") or "&mdash;"
            out.append(
                "<tr>"
                f'<td class="k-id"><a href="{LP_BUG_URL}{record["id"]}">#{record["id"]}</a></td>'
                f'<td><a href="{LP_BUG_URL}{record["id"]}" style="color:inherit;text-decoration:none">'
                f'{escape(record.get("title", ""))}</a></td>'
                f'<td>{series}</td>'
                f'<td>{escape(record.get("importance", ""))}</td>'
                f'<td>{escape(record.get("status", ""))}</td>'
                f'<td>{escape(record.get("assignee", ""))}</td>'
                f'<td class="k-targets">{targets}</td>'
                "</tr>\n"
            )
        out.append("</tbody>\n</table>\n")

    out.append(_footer(now))
    with open(path, "w") as fh:
        fh.write("".join(out))


def _write_index(path, counts, now):
    out = [_head("Ubuntu kernel newly opened bugs"), "<main>\n",
           "<h2>Newly opened kernel bugs</h2>\n",
           '<p class="k-sub">Bugs filed against Ubuntu kernel targets on Launchpad, '
           "bucketed by how recently they were opened.</p>\n"]
    out.append(_tabs("index"))
    out.append('<div class="k-cards">\n')
    for slug, label, _ in WINDOWS:
        out.append(
            f'<a class="k-card" href="{slug}.html">'
            f'<span class="k-count">{counts.get(slug, 0)}</span>'
            f'<span class="k-label">{escape(label)}</span></a>\n'
        )
    out.append("</div>\n")
    out.append(_footer(now))
    with open(path, "w") as fh:
        fh.write("".join(out))


def main():
    parser = ArgumentParser(
        description="Render the 24hrs-of-bugs JSON store into static HTML reports.",
        formatter_class=RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", default=".",
                        help="Directory to write the rendered HTML into.")
    parser.add_argument("--state-dir",
                        help="Directory holding the store (defaults to --output).")
    parser.add_argument("--datetime",
                        help="Treat this UTC 'YYYY-MM-DD HH:MM' as 'now' (for testing).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.datetime:
        now = datetime.strptime(args.datetime + ":00+0000", "%Y-%m-%d %H:%M:%S%z")
    else:
        now = datetime.now(timezone.utc)

    output_dir = os.path.abspath(args.output)
    state_dir = os.path.abspath(args.state_dir) if args.state_dir else output_dir
    os.makedirs(output_dir, exist_ok=True)

    store = Store(state_dir)
    render(store, now, output_dir)
    log.info("II: Rendered reports into %s (generated %s)", output_dir, iso(now))


if __name__ == "__main__":
    main()
