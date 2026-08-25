"""§13.2 «Локальные пути», кейсы 8–15."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mdlink.checkers.local import LocalChecker, resolve_local, split_target
from mdlink.models import Link, LinkKind, Status


def link(raw: str, source: Path, **kw) -> Link:
    return Link(raw=raw, kind=LinkKind.LOCAL, source_file=source, line=1, column=1, **kw)


def check(sample: Path, rel_source: str, raw: str, **kw):
    checker = LocalChecker(sample, **kw)
    return checker.check(link(raw, sample / rel_source))


# --- 8–10: база резолвинга ---

def test_8_relative_link_resolves_from_source_dir(sample: Path):
    """docs/guide/setup.md + ../api.md → docs/api.md, а не от root и не от CWD."""
    target = resolve_local("../api.md", sample / "docs/guide/setup.md", sample)
    assert target == sample / "docs" / "api.md"
    assert check(sample, "docs/guide/setup.md", "../api.md").status is Status.OK


def test_9_root_absolute_link_resolves_from_project_root(sample: Path):
    target = resolve_local("/docs/api.md", sample / "docs/guide/setup.md", sample)
    assert target == sample / "docs" / "api.md"
    assert check(sample, "docs/guide/setup.md", "/docs/api.md").status is Status.OK


def test_9b_root_absolute_is_not_filesystem_root(sample: Path):
    target = resolve_local("/etc/passwd", sample / "README.md", sample)
    assert str(target).startswith(str(sample))


def test_10_result_is_independent_of_cwd(sample: Path, tmp_path: Path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    previous = Path.cwd()
    try:
        os.chdir(other)
        assert check(sample, "docs/guide/setup.md", "../api.md").status is Status.OK
    finally:
        os.chdir(previous)


def test_tilde_is_not_expanded(sample: Path):
    target = resolve_local("~/secrets.md", sample / "README.md", sample)
    assert target == sample / "~" / "secrets.md"


# --- 11: percent-encoding ---

def test_11_percent_encoded_spaces(sample: Path):
    res = check(sample, "docs/index.md", "./file%20with%20spaces.md")
    assert res.status is Status.OK


# --- 12: регистр ---

def test_12_case_mismatch_detected_even_on_case_insensitive_fs(sample: Path):
    res = check(sample, "docs/guide/setup.md", "./README.md")
    assert res.status is Status.BROKEN
    assert res.code == "case_mismatch"
    assert res.suggestion == "./ReadMe.md"


def test_12b_correct_case_is_ok(sample: Path):
    assert check(sample, "docs/guide/setup.md", "./ReadMe.md").status is Status.OK


def test_12c_case_mismatch_in_directory_component(sample: Path):
    res = check(sample, "README.md", "./DOCS/api.md")
    assert res.code == "case_mismatch"
    assert res.suggestion == "./docs/api.md"


# --- 13: выход за пределы root ---

def test_13_traversal_is_flagged(sample: Path):
    res = check(sample, "edge/traversal.md", "../../../../../../etc/passwd")
    assert res.status is Status.WARNING
    assert res.code == "outside_root"


# --- 14: директории ---

def test_14_directory_with_index_is_ok(sample: Path):
    assert check(sample, "README.md", "./docs/").status is Status.OK


def test_14b_directory_without_index_warns(sample: Path):
    res = check(sample, "README.md", "./edge/")
    assert res.status is Status.WARNING
    assert res.code == "link_to_directory"


def test_14c_no_dir_index_flag(sample: Path):
    res = check(sample, "README.md", "./docs/", dir_index=False)
    assert res.code == "link_to_directory"


# --- 15: симлинки ---

def test_15_broken_symlink(sample: Path):
    res = check(sample, "edge/symlinks.md", "./broken-symlink.md")
    assert res.status is Status.BROKEN
    assert res.code == "broken_symlink"


def test_15b_valid_symlink_is_ok(sample: Path):
    (sample / "edge" / "alias.md").symlink_to("./dup-target.md")
    assert check(sample, "edge/symlinks.md", "./alias.md").status is Status.OK


# --- прочее из §6 ---

def test_split_target_order():
    assert split_target("./docs/api.md?v=2#install") == ("./docs/api.md", "v=2", "install")
    assert split_target("#install") == ("", "", "install")
    assert split_target("./a.md#a?b") == ("./a.md", "", "a?b")


def test_query_is_ignored_for_local_links(sample: Path):
    assert check(sample, "docs/index.md", "./api.md?v=2").status is Status.OK


def test_empty_link(sample: Path):
    res = LocalChecker(sample).check(link("", sample / "edge/empty-link.md"))
    assert res.code == "empty_link"


def test_file_not_found_suggests_neighbour(sample: Path):
    res = check(sample, "README.md", "./docs/aip.md")
    assert res.code == "file_not_found"
    assert res.suggestion == "./docs/api.md"


def test_file_not_found_suggests_md_extension(sample: Path):
    res = check(sample, "docs/index.md", "./api")
    assert res.code == "file_not_found"
    assert res.suggestion == "./api.md"


def test_anchor_ok(sample: Path):
    res = LocalChecker(sample).check(
        Link(raw="../api.md#установка", kind=LinkKind.LOCAL,
             source_file=sample / "docs/guide/setup.md", line=1, column=1))
    assert res.status is Status.OK


def test_anchor_not_found_is_warning_with_suggestion(sample: Path):
    res = check(sample, "docs/guide/setup.md", "../api.md#Устоновка")
    assert res.status is Status.WARNING
    assert res.code == "anchor_not_found"
    assert res.suggestion == "#установка"


def test_anchor_only_checks_current_file(sample: Path):
    res = LocalChecker(sample).check(
        Link(raw="#ветки", kind=LinkKind.ANCHOR_ONLY,
             source_file=sample / "ru/Работа с Git.md", line=1, column=1))
    assert res.status is Status.OK


def test_anchor_check_can_be_disabled(sample: Path):
    res = check(sample, "docs/guide/setup.md", "../api.md#Устоновка", check_anchors=False)
    assert res.status is Status.OK


def test_duplicate_headings_get_numeric_suffix(sample: Path):
    assert check(sample, "docs/guide/setup.md", "../api.md#установка-1").status is Status.OK


def test_explicit_html_anchor(sample: Path):
    assert check(sample, "docs/guide/setup.md", "../api.md#legacy-anchor").status is Status.OK


def test_kramdown_custom_id(sample: Path):
    assert check(sample, "docs/guide/setup.md", "../api.md#custom-anchor").status is Status.OK


def test_file_url_is_warned(sample: Path, tmp_path: Path):
    target = sample / "docs" / "api.md"
    res = LocalChecker(sample).check(
        Link(raw=f"file://{target}", kind=LinkKind.FILE_URL,
             source_file=sample / "README.md", line=1, column=1))
    assert res.status is Status.WARNING
    assert res.code == "absolute_file_url"


def test_file_url_missing_is_broken(sample: Path):
    res = LocalChecker(sample).check(
        Link(raw="file:///definitely/not/here.md", kind=LinkKind.FILE_URL,
             source_file=sample / "README.md", line=1, column=1))
    assert res.code == "file_not_found"


def test_unencoded_space_merges_with_existence_check(sample: Path):
    """§5.3: пользователь должен узнать оба факта."""
    res = LocalChecker(sample).check(Link(
        raw="Работа с Git.md", kind=LinkKind.LOCAL,
        source_file=sample / "ru/readme.md", line=1, column=1,
        parse_code="unencoded_space", parse_detail="пробел не экранирован",
        parse_suggestion="<Работа с Git.md>"))
    assert res.status is Status.WARNING
    assert res.code == "unencoded_space"
    assert res.suggestion == "<Работа с Git.md>"


def test_unencoded_space_broken_target_stays_broken(sample: Path):
    res = LocalChecker(sample).check(Link(
        raw="Нет такого файла.md", kind=LinkKind.LOCAL,
        source_file=sample / "ru/readme.md", line=1, column=1,
        parse_code="unencoded_space", parse_detail="пробел не экранирован",
        parse_suggestion="<Нет такого файла.md>"))
    assert res.status is Status.BROKEN
    assert res.code == "file_not_found"
    assert "не экранирован" in res.detail


# --- wiki-ссылки (§5.5) ---

def test_wikilink_resolves_by_basename(sample: Path):
    res = LocalChecker(sample).check(Link(
        raw="Работа с Git", kind=LinkKind.LOCAL,
        source_file=sample / "ru/readme.md", line=1, column=1, wikilink=True))
    assert res.status is Status.OK


def test_wikilink_not_found(sample: Path):
    res = LocalChecker(sample).check(Link(
        raw="Нет такого", kind=LinkKind.LOCAL,
        source_file=sample / "ru/readme.md", line=1, column=1, wikilink=True))
    assert res.code == "wikilink_not_found"


def test_wikilink_ambiguous(sample: Path):
    res = LocalChecker(sample).check(Link(
        raw="index", kind=LinkKind.LOCAL,
        source_file=sample / "ru/readme.md", line=1, column=1, wikilink=True))
    assert res.status is Status.WARNING
    assert res.code == "wikilink_ambiguous"


def test_permission_denied(sample: Path):
    locked = sample / "locked"
    locked.mkdir()
    (locked / "a.md").write_text("# a\n", encoding="utf-8")
    os.chmod(locked, 0o000)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("тест бессмыслен под root")
        res = check(sample, "README.md", "./locked/a.md")
        assert res.code == "permission_denied"
    finally:
        os.chmod(locked, 0o755)
