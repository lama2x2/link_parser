"""Сквозной прогон по tests/fixtures/sample-project (§13.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mdlink.engine import Engine, ScanOptions
from mdlink.models import Status


@pytest.fixture
def results(sample: Path):
    report = Engine(ScanOptions(path=sample, root=sample, check_external=False)).run()
    return report, {(r.link.source_file.relative_to(sample).as_posix(), r.link.line): r
                    for r in report.results}


def code_at(index, file: str, line: int) -> str:
    return index[(file, line)].code


def test_every_expected_broken_case_is_found(results):
    _report, index = results
    expected = {
        ("README.md", 8): "file_not_found",
        ("docs/guide/setup.md", 5): "file_not_found",
        ("docs/guide/setup.md", 6): "case_mismatch",
        ("edge/empty-link.md", 3): "empty_link",
        ("edge/html.md", 5): "file_not_found",
        ("edge/refs.md", 5): "undefined_reference",
        ("ru/readme.md", 5): "case_mismatch",
        ("ru/readme.md", 8): "case_mismatch",
        ("edge/symlinks.md", 3): "broken_symlink",
    }
    for key, code in expected.items():
        assert index[key].code == code, f"{key}: {index[key].code}"
        assert index[key].status is Status.BROKEN


def test_every_expected_warning_is_found(results):
    _report, index = results
    expected = {
        ("README.md", 7): "anchor_not_found",
        ("README.md", 10): "link_to_directory",
        ("docs/guide/setup.md", 8): "anchor_not_found",
        ("edge/traversal.md", 3): "outside_root",
        ("ru/readme.md", 7): "unencoded_space",
    }
    for key, code in expected.items():
        assert index[key].code == code, f"{key}: {index[key].code}"
        assert index[key].status is Status.WARNING


def test_code_blocks_contribute_no_links(results):
    _report, index = results
    lines = [k[1] for k in index if k[0] == "edge/code-blocks.md"]
    assert lines == [17]                       # только «реальная» ссылка


def test_private_hosts_are_skipped(sample: Path):
    """Внешние проверки включены, но в сеть утилита не ходит: все хосты приватные."""
    target = sample / "ru" / "Доступы.md"
    report = Engine(ScanOptions(path=target, root=sample)).run()
    codes = {r.code for r in report.results}
    assert codes == {"private_host", "unsupported_scheme"}
    assert report.requests_made == 0           # ни одного сетевого запроса


def test_summary_counts_are_consistent(results):
    report, _index = results
    counts = report.counts
    assert sum(counts.values()) == len(report.results)
    assert counts["broken"] >= 9


def test_wikilinks_auto_enable_with_obsidian_dir(sample: Path):
    """§5.5: наличие .obsidian/ включает разбор [[...]]."""
    from mdlink.parser import MarkdownParser

    (sample / ".obsidian").mkdir()
    (sample / "ru" / "wiki.md").write_text(
        "[[Работа с Git]] и [[Нет такого]]\n", encoding="utf-8")
    links = MarkdownParser(wikilinks=True).parse(
        (sample / "ru" / "wiki.md").read_text(encoding="utf-8"),
        sample / "ru" / "wiki.md")
    assert [l.raw for l in links] == ["Работа с Git", "Нет такого"]

    report = Engine(ScanOptions(path=sample, root=sample, check_external=False,
                                wikilinks=True)).run()
    codes = {r.code for r in report.results if r.link.wikilink}
    assert "wikilink_not_found" in codes
