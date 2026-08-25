"""Общая для форматтеров выборка и группировка результатов."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from ..engine import Report
from ..models import Result, Status


def visible(report: Report, show_all: bool = False) -> list[Result]:
    """По умолчанию — только BROKEN и WARNING (§9.1)."""
    if show_all:
        return report.results
    return [r for r in report.results if r.status in (Status.BROKEN, Status.WARNING)]


def group_by_file(results: list[Result], report: Report) -> "OrderedDict[str, list[Result]]":
    buckets: dict[str, list[Result]] = {}
    for r in results:
        buckets.setdefault(report.display_path(r.link.source_file), []).append(r)
    ordered = OrderedDict()
    for name in sorted(buckets):
        ordered[name] = sorted(buckets[name], key=lambda r: (r.link.line, r.link.column))
    return ordered


def summary_line(report: Report) -> str:
    c = report.counts
    return (f"Файлов: {report.files} · ссылок: {report.links} "
            f"(уникальных URL: {report.unique_urls})")


def status_label(r: Result) -> str:
    return {
        Status.OK: "OK",
        Status.BROKEN: "BROKEN",
        Status.WARNING: "WARN",
        Status.SKIPPED: "SKIP",
        Status.INFO: "INFO",
    }[r.status]


def rel_source(report: Report, p: Path) -> str:
    return report.display_path(p)
