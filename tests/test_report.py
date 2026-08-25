"""§9: форматы отчёта."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from mdlink.engine import Engine, ScanOptions
from mdlink.report import render
from mdlink.textutil import truncate_middle


@pytest.fixture
def report(sample: Path):
    return Engine(ScanOptions(path=sample, root=sample, check_external=False)).run()


def test_pretty_has_no_ansi_without_color(report):
    out = render("pretty", report, color=False)
    assert "\x1b" not in out
    assert "BROKEN" in out and "Файлов:" in out


def test_pretty_uses_ansi_with_color(report):
    assert "\x1b" in render("pretty", report, color=True)


def test_pretty_groups_by_file_and_shows_suggestions(report):
    out = render("pretty", report, color=False)
    assert "docs/guide/setup.md" in out
    assert "↳ возможно:" in out


def test_pretty_hides_ok_by_default(report):
    assert "./docs/index.md" not in render("pretty", report, color=False)
    assert "./docs/index.md" in render("pretty", report, color=False, show_all=True)


def test_pretty_quiet_is_summary_only(report):
    out = render("pretty", report, color=False, quiet=True)
    assert "docs/guide/setup.md" not in out
    assert "Файлов:" in out


def test_json_schema(report):
    payload = json.loads(render("json", report))
    assert payload["version"] == "1.0"
    assert Path(payload["root"]).is_absolute()
    assert payload["started_at"].endswith("Z")
    assert set(payload["summary"]) == {"files", "links", "unique_urls",
                                       "ok", "broken", "warning", "skipped"}
    assert all(not Path(r["file"]).is_absolute() for r in payload["results"])
    assert all("/" in r["file"] or "\\" not in r["file"] for r in payload["results"])


def test_json_excludes_ok_unless_all(report):
    assert all(r["status"] != "OK" for r in json.loads(render("json", report))["results"])
    assert any(r["status"] == "OK"
               for r in json.loads(render("json", report, show_all=True))["results"])


def test_markdown_is_github_table(report):
    out = render("markdown", report)
    assert out.startswith("## mdlink")
    assert "|---:|---|---|---|---|" in out
    assert "\x1b" not in out


def test_markdown_escapes_pipes():
    from mdlink.report.markdown_out import _escape

    assert _escape("a|b") == "a\\|b"


def test_junit_structure(report):
    suite = ET.fromstring(render("junit", report))
    assert suite.tag == "testsuite"
    assert int(suite.attrib["tests"]) == len(suite.findall("testcase"))
    assert suite.findall(".//failure")


def test_truncate_middle_keeps_host_and_tail():
    out = truncate_middle("https://example.com/a/b/c/d/e/page.html", 24)
    assert len(out) == 24
    assert out.startswith("https://")
    assert out.endswith("page.html")
    assert "…" in out


def test_truncate_middle_noop_when_short():
    assert truncate_middle("https://x.dev", 40) == "https://x.dev"


def test_display_path_prefix_for_repo_mode(sample: Path):
    opts = ScanOptions(path=sample, root=sample, check_external=False,
                       path_prefix="owner/name@main:")
    rep = Engine(opts).run()
    payload = json.loads(render("json", rep))
    assert payload["results"][0]["file"].startswith("owner/name@main:")


def test_pretty_does_not_interpret_link_text_as_rich_markup(sample: Path):
    """`[текст][nosuchref]` — это данные, а не теги rich: строка не должна
    потерять часть содержимого при рендере."""
    report = Engine(ScanOptions(path=sample / "edge" / "refs.md", root=sample,
                                check_external=False)).run()
    out = render("pretty", report, color=False)
    assert "[текст][nosuchref]" in out
    assert "nosuchref" in out


def test_pretty_survives_square_brackets_in_paths(tmp_path: Path):
    directory = tmp_path / "[draft]"
    directory.mkdir()
    (directory / "a.md").write_text("[нет](./missing.md)\n", encoding="utf-8")
    report = Engine(ScanOptions(path=tmp_path, root=tmp_path, check_external=False)).run()
    out = render("pretty", report, color=False)
    assert "[draft]/a.md" in out
