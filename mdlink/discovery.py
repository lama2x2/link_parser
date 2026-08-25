"""Обнаружение Markdown-файлов (§4).

Обход директории с учётом include/exclude-глобов, вложенных ``.gitignore``,
симлинков и защиты от циклов.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

DEFAULT_INCLUDE: tuple[str, ...] = ("**/*.md", "**/*.markdown")

DEFAULT_EXCLUDE: tuple[str, ...] = (
    "**/node_modules/**",
    "**/.git/**",
    "**/venv/**",
    "**/.venv/**",
    "**/dist/**",
    "**/build/**",
    "**/vendor/**",
    "**/.next/**",
    "**/target/**",
)

MAX_FILE_SIZE = 10 * 1024 * 1024  # §4.1 п.6

_PROBE = "__mdlink_probe__"


@dataclass
class Discovery:
    files: list[Path] = field(default_factory=list)
    oversized: list[Path] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)


def _spec(patterns) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitwildmatch", list(patterns))


def _read_gitignore(path: Path) -> pathspec.PathSpec | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return _spec(lines)


def _ignored(rel: str, stack: list[tuple[str, pathspec.PathSpec]]) -> bool:
    """Правила более глубокого ``.gitignore`` перекрывают вышележащие."""
    for base, spec in reversed(stack):
        sub = rel[len(base):].lstrip("/") if base else rel
        if not sub:
            continue
        res = spec.check_file(sub)
        if res.include is not None:
            return bool(res.include)
    return False


def discover(
    path: Path,
    root: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    use_gitignore: bool = True,
    follow_symlinks: bool = False,
    max_size: int = MAX_FILE_SIZE,
) -> Discovery:
    """Собрать список ``.md``-файлов. ``path`` — файл или директория."""
    inc = _spec(include or DEFAULT_INCLUDE)
    exc = _spec(list(exclude) if exclude is not None else list(DEFAULT_EXCLUDE))
    out = Discovery()

    if path.is_file():
        _consider_file(path, root, out, max_size, inc=None, exc=None)
        return out

    visited: set[tuple[int, int]] = set()
    gitignore_stack: list[tuple[str, pathspec.PathSpec]] = []

    def walk(directory: Path, rel_dir: str) -> None:
        try:
            st = directory.stat()
            key = (st.st_dev, st.st_ino)
            if key in visited:
                return
            visited.add(key)
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except PermissionError as exc_:
            out.unreadable.append((directory, str(exc_)))
            return
        except OSError as exc_:
            out.unreadable.append((directory, str(exc_)))
            return

        pushed = False
        if use_gitignore:
            gi = directory / ".gitignore"
            if gi.is_file():
                spec = _read_gitignore(gi)
                if spec is not None:
                    gitignore_stack.append((rel_dir, spec))
                    pushed = True
        try:
            for entry in entries:
                name = entry.name
                rel = f"{rel_dir}/{name}" if rel_dir else name
                try:
                    is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                except OSError:
                    continue
                if is_dir:
                    if entry.is_symlink() and not follow_symlinks:
                        continue  # §4.1 п.5: симлинки на директории не раскрываем
                    if exc.match_file(f"{rel}/{_PROBE}") or exc.match_file(rel):
                        continue
                    if use_gitignore and _ignored(f"{rel}/", gitignore_stack):
                        continue
                    walk(Path(entry.path), rel)
                else:
                    if not inc.match_file(rel) or exc.match_file(rel):
                        continue
                    if use_gitignore and _ignored(rel, gitignore_stack):
                        continue
                    _consider_file(Path(entry.path), root, out, max_size, inc, exc)
        finally:
            if pushed:
                gitignore_stack.pop()

    walk(path, "")
    out.files.sort()
    return out


def _consider_file(p: Path, root: Path, out: Discovery, max_size: int, inc, exc) -> None:
    try:
        size = p.stat().st_size
    except OSError as e:
        out.unreadable.append((p, str(e)))
        return
    if size > max_size:
        out.oversized.append(p)
        return
    out.files.append(p)


def read_markdown(path: Path) -> str:
    """UTF-8 с ``errors='replace'``; BOM снимается, переводы строк нормализуются
    в :func:`mdlink.parser.normalize_text`."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def relpath(p: Path, root: Path) -> str:
    """POSIX-путь относительно root; вне root — абсолютный POSIX.

    Симлинки намеренно НЕ разыменовываются: путь в отчёте должен совпадать
    с тем, что видит пользователь в дереве проекта.
    """
    ap, ar = os.path.abspath(p), os.path.abspath(root)
    try:
        if os.path.commonpath([ap, ar]) != ar:
            return Path(ap).as_posix()
    except ValueError:
        return Path(ap).as_posix()
    return Path(os.path.relpath(ap, ar)).as_posix()
