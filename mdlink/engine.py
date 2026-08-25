"""Оркестрация прогона: обнаружение → парсинг → чекеры → сводка.

Печатью занимается только ``report/*``; здесь — исключительно данные.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .checkers.http import HttpChecker, HttpOptions, normalize_url
from .checkers.local import LocalChecker
from .discovery import discover, read_markdown, relpath
from .models import Link, LinkKind, Result, Status
from .parser import MarkdownParser

LOCAL_KINDS = (LinkKind.LOCAL, LinkKind.ANCHOR_ONLY, LinkKind.FILE_URL)


@dataclass
class ScanOptions:
    path: Path
    root: Path
    include: list[str] | None = None
    exclude: list[str] | None = None
    use_gitignore: bool = True
    follow_symlinks: bool = False
    check_external: bool = True
    check_local: bool = True
    check_anchors: bool = True
    check_images: bool = True
    dir_index: bool = True
    wikilinks: bool = False
    bare_urls: bool = False
    http: HttpOptions = field(default_factory=HttpOptions)
    path_prefix: str = ""


@dataclass
class Report:
    root: Path
    results: list[Result]
    files: int
    links: int
    unique_urls: int
    started_at: dt.datetime
    duration_ms: int
    interrupted: bool = False
    path_prefix: str = ""
    requests_made: int = 0

    @property
    def counts(self) -> dict[str, int]:
        out = {"ok": 0, "broken": 0, "warning": 0, "skipped": 0}
        for r in self.results:
            out[str(r.status).lower()] = out.get(str(r.status).lower(), 0) + 1
        return out

    def display_path(self, p: Path) -> str:
        rel = relpath(p, self.root)
        return f"{self.path_prefix}{rel}" if self.path_prefix else rel


class Engine:
    def __init__(self, options: ScanOptions, *, on_event=None) -> None:
        options.root = Path(os.path.abspath(options.root))  # §9.2: root в отчёте абсолютный
        options.path = Path(os.path.abspath(options.path))
        self.opt = options
        self.on_event = on_event or (lambda *_a, **_k: None)
        self.parser = MarkdownParser(
            check_images=options.check_images,
            wikilinks=options.wikilinks,
            bare_urls=options.bare_urls,
        )
        self.local = LocalChecker(
            options.root,
            check_anchors=options.check_anchors,
            dir_index=options.dir_index,
        )
        self.http = HttpChecker(options.http)

    # -- полный прогон --

    def run(self) -> Report:
        started_wall = dt.datetime.now(dt.timezone.utc)
        t0 = time.monotonic()
        results: list[Result] = []
        interrupted = False

        found = discover(
            self.opt.path, self.opt.root,
            include=self.opt.include, exclude=self.opt.exclude,
            use_gitignore=self.opt.use_gitignore,
            follow_symlinks=self.opt.follow_symlinks,
        )
        for big in found.oversized:
            results.append(_synthetic(big, self.opt.root, Status.WARNING, "file_too_large",
                                      "файл больше 10 МБ — пропущен"))
        for path, err in found.unreadable:
            results.append(_synthetic(path, self.opt.root, Status.WARNING,
                                      "permission_denied", err))

        self.on_event("files", len(found.files))

        links: list[Link] = []
        for path in found.files:
            if interrupted:
                break
            try:
                text = read_markdown(path)
            except PermissionError as exc:
                results.append(_synthetic(path, self.opt.root, Status.WARNING,
                                          "permission_denied", str(exc)))
                continue
            except OSError as exc:
                results.append(_synthetic(path, self.opt.root, Status.WARNING,
                                          "permission_denied", str(exc)))
                continue
            try:
                links.extend(self.parser.parse(text, path))
            except KeyboardInterrupt:
                interrupted = True
                break

        self.on_event("links", len(links))

        parse_only, local_links, http_links, skipped = _partition(links, self.opt)
        results.extend(parse_only)
        results.extend(skipped)

        try:
            for link in local_links:
                results.append(self.local.check(link))
                self.on_event("checked", 1)
        except KeyboardInterrupt:
            interrupted = True

        unique_urls = len({normalize_url(l.raw) for l in links if l.kind is LinkKind.HTTP})
        if http_links and not interrupted:
            try:
                results.extend(asyncio.run(self._run_http(http_links)))
            except KeyboardInterrupt:
                interrupted = True

        results.sort(key=lambda r: (str(r.link.source_file), r.link.line, r.link.column))
        return Report(
            root=self.opt.root,
            results=results,
            files=len(found.files),
            links=len(links),
            unique_urls=unique_urls,
            started_at=started_wall,
            duration_ms=int((time.monotonic() - t0) * 1000),
            interrupted=interrupted,
            path_prefix=self.opt.path_prefix,
            requests_made=self.http.requests_made,
        )

    async def _run_http(self, links: list[Link]) -> list[Result]:
        return await self.http.check_all(
            links, on_done=lambda url, v: self.on_event("checked", 1)
        )


def _partition(links: list[Link], opt: ScanOptions):
    parse_only: list[Result] = []
    local_links: list[Link] = []
    http_links: list[Link] = []
    skipped: list[Result] = []

    for link in links:
        if link.parse_code == "undefined_reference":
            parse_only.append(Result(link, Status.BROKEN, "undefined_reference",
                                     link.parse_detail or "ссылка-определение не найдена"))
            continue
        if link.kind in (LinkKind.MAILTO, LinkKind.OTHER):
            skipped.append(Result(link, Status.SKIPPED, "unsupported_scheme",
                                  "схема не проверяется"))
            continue
        if link.kind is LinkKind.HTTP:
            if opt.check_external:
                http_links.append(link)
            else:
                skipped.append(Result(link, Status.SKIPPED, "external_disabled",
                                      "внешние ссылки отключены (--no-external)"))
            continue
        if opt.check_local:
            local_links.append(link)
        else:
            skipped.append(Result(link, Status.SKIPPED, "local_disabled",
                                  "локальные ссылки отключены (--no-local)"))
    return parse_only, local_links, http_links, skipped


def _synthetic(path: Path, root: Path, status: Status, code: str, detail: str) -> Result:
    link = Link(raw=relpath(path, root), kind=LinkKind.OTHER, source_file=path,
                line=1, column=0)
    return Result(link=link, status=status, code=code, detail=detail)
