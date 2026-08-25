"""§4: обход ФС, include/exclude, .gitignore, симлинки, лимит размера."""

from __future__ import annotations

import os
from pathlib import Path

from mdlink.discovery import DEFAULT_EXCLUDE, discover, read_markdown, relpath


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def names(tmp_path: Path, **kw) -> set[str]:
    found = discover(tmp_path, tmp_path, **kw)
    return {relpath(f, tmp_path) for f in found.files}


def test_recursive_markdown_discovery(tmp_path: Path):
    build(tmp_path, {"a.md": "", "docs/b.md": "", "docs/c.markdown": "",
                     "docs/d.txt": "", "e.MD": ""})
    assert names(tmp_path) == {"a.md", "docs/b.md", "docs/c.markdown"}


def test_default_excludes(tmp_path: Path):
    build(tmp_path, {"a.md": "", "node_modules/pkg/readme.md": "",
                     "dist/x.md": "", ".git/y.md": "", "venv/z.md": ""})
    assert names(tmp_path) == {"a.md"}


def test_custom_include(tmp_path: Path):
    build(tmp_path, {"README.md": "", "docs/a.md": ""})
    assert names(tmp_path, include=["docs/**/*.md"]) == {"docs/a.md"}


def test_custom_exclude(tmp_path: Path):
    build(tmp_path, {"README.md": "", "CHANGELOG.md": ""})
    assert names(tmp_path, exclude=["**/CHANGELOG.md"]) == {"README.md"}


def test_gitignore_is_respected(tmp_path: Path):
    build(tmp_path, {"a.md": "", "secret/b.md": "", ".gitignore": "secret/\n"})
    assert names(tmp_path) == {"a.md"}
    assert names(tmp_path, use_gitignore=False) == {"a.md", "secret/b.md"}


def test_nested_gitignore_overrides_parent(tmp_path: Path):
    build(tmp_path, {
        ".gitignore": "*.md\n",
        "a.md": "",
        "docs/.gitignore": "!keep.md\n",
        "docs/keep.md": "",
        "docs/drop.md": "",
    })
    assert names(tmp_path) == {"docs/keep.md"}


def test_directory_symlinks_not_followed_by_default(tmp_path: Path):
    build(tmp_path, {"real/a.md": ""})
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    assert names(tmp_path) == {"real/a.md"}


def test_followed_symlink_dir_is_scanned_once(tmp_path: Path):
    """§4.1 п.5: множество посещённых (st_dev, st_ino) не даёт обойти одну и ту
    же директорию дважды — файл попадает в отчёт ровно один раз."""
    build(tmp_path, {"real/a.md": ""})
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    found = names(tmp_path, follow_symlinks=True)
    assert found in ({"real/a.md"}, {"alias/a.md"})


def test_symlink_cycle_does_not_hang(tmp_path: Path):
    build(tmp_path, {"a.md": ""})
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert names(tmp_path, follow_symlinks=True) == {"a.md"}


def test_file_symlinks_are_followed(tmp_path: Path):
    build(tmp_path, {"real.md": "# r\n"})
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    assert names(tmp_path) == {"real.md", "alias.md"}


def test_oversized_file_is_reported(tmp_path: Path):
    big = tmp_path / "big.md"
    big.write_text("x" * 200, encoding="utf-8")
    found = discover(tmp_path, tmp_path, max_size=100)
    assert found.files == []
    assert found.oversized == [big]


def test_single_file_path(tmp_path: Path):
    build(tmp_path, {"a.md": "", "b.md": ""})
    found = discover(tmp_path / "a.md", tmp_path)
    assert [p.name for p in found.files] == ["a.md"]


def test_read_markdown_handles_bad_bytes(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_bytes(b"\xff\xfe# \xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82\n")
    text = read_markdown(p)
    assert "Привет" in text            # errors="replace", а не падение


def test_relpath_is_posix_and_relative(tmp_path: Path):
    assert relpath(tmp_path / "a" / "b.md", tmp_path) == "a/b.md"


def test_default_exclude_list_matches_spec():
    assert "**/node_modules/**" in DEFAULT_EXCLUDE
    assert "**/.git/**" in DEFAULT_EXCLUDE
    assert "**/target/**" in DEFAULT_EXCLUDE
