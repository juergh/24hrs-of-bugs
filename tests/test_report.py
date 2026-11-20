import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import common


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, "..", filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load("render_html", "render-html.py")
fetch = _load("fetch_bugs", "fetch-bugs.py")

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)


def _target(name, days_ago):
    return {
        "name": name,
        "type": "package",
        "date_created": common.iso(NOW - timedelta(days=days_ago)),
    }


def _bug(bug_id, *targets):
    return {
        "id": bug_id,
        "title": f"bug {bug_id}",
        "series": "noble",
        "importance": "Medium",
        "status": "New",
        "assignee": "unassigned",
        "targets": list(targets),
    }


def test_iso_roundtrip():
    assert common.parse_iso(common.iso(NOW)) == NOW


def test_window_membership_uses_newest_target():
    # Bug opened long ago but a target task was added 2 hours ago.
    record = _bug(1, _target("linux", 40), _target("linux-aws", 0))
    assert render._bug_in_window(record, NOW, 1) is not None  # 24h
    assert render._bug_in_window(record, NOW, 90) is not None


def test_window_membership_excludes_old():
    record = _bug(2, _target("linux", 40))
    assert render._bug_in_window(record, NOW, 1) is None
    assert render._bug_in_window(record, NOW, 7) is None
    assert render._bug_in_window(record, NOW, 30) is None
    assert render._bug_in_window(record, NOW, 90) is not None


def test_window_bugs_sorted_newest_first():
    bugs = [
        _bug(1, _target("linux", 5)),
        _bug(2, _target("linux", 1)),
        _bug(3, _target("linux", 3)),
    ]
    rows = render._window_bugs(bugs, NOW, 7)
    assert [r[1]["id"] for r in rows] == [2, 3, 1]


def test_cumulative_windows():
    bugs = [
        _bug(1, _target("linux", 0)),
        _bug(2, _target("linux", 5)),
        _bug(3, _target("linux", 20)),
        _bug(4, _target("linux", 60)),
    ]
    assert len(render._window_bugs(bugs, NOW, 1)) == 1
    assert len(render._window_bugs(bugs, NOW, 7)) == 2
    assert len(render._window_bugs(bugs, NOW, 30)) == 3
    assert len(render._window_bugs(bugs, NOW, 90)) == 4


def test_store_upsert_and_prune(tmp_path):
    store = common.Store(str(tmp_path))
    assert store.read_last_run() is None

    store.save_bug(_bug(10, _target("linux", 0)))
    store.save_bug(_bug(11, _target("linux", 100)))

    loaded = store.load_bug(10)
    assert loaded is not None
    assert loaded["title"] == "bug 10"

    # Emulate the pruner.
    cutoff = NOW - timedelta(days=common.RETENTION_DAYS)
    for record in list(store.all_bugs()):
        newest = max(common.parse_iso(t["date_created"]) for t in record["targets"])
        if newest < cutoff:
            store.remove_bug(record["id"])

    assert store.load_bug(10) is not None
    assert store.load_bug(11) is None


def test_last_run_roundtrip(tmp_path):
    store = common.Store(str(tmp_path))
    store.write_last_run(NOW)
    assert store.read_last_run() == NOW


def test_retry_gives_up(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("flaky")

    with pytest.raises(fetch.RetryExhausted):
        fetch.retry("boom", boom)
    assert calls["n"] == fetch.RETRY_ATTEMPTS


def test_retry_succeeds_after_failures(monkeypatch):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flaky")
        return "ok"

    assert fetch.retry("flaky", flaky) == "ok"
    assert calls["n"] == 3


def test_render_from_store(tmp_path):
    store = common.Store(str(tmp_path))
    bug10 = _bug(10, _target("linux", 0))
    bug10["series"] = ""
    store.save_bug(bug10)
    store.save_bug(_bug(11, _target("linux", 20)))
    render.render(store, NOW, str(tmp_path))

    for slug, _, _ in common.WINDOWS:
        assert os.path.exists(os.path.join(tmp_path, f"{slug}.html"))
    index = open(os.path.join(tmp_path, "index.html")).read()
    assert "Newly opened kernel bugs" in index

    day = open(os.path.join(tmp_path, "24-hours.html")).read()
    assert "/bugs/10" in day
    assert "/bugs/11" not in day
    assert "<td>&mdash;</td>" in day
    assert "&amp;mdash;" not in day
    assert 'data-series=""' in day
    assert 'data-targets="linux"' in day
    assert 'id="k-pkg-search"' in day
    assert 'id="k-filter-clear"' in day
    assert 'data-series="none"' in day
    assert 'id="k-bugs-table"' in day
    assert "applyFilters" in day
