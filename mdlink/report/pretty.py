"""Человекочитаемый отчёт с rich-таблицами (§9.1)."""

from __future__ import annotations

import io

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..engine import Report
from ..models import Status
from ..textutil import truncate_middle
from .common import group_by_file, status_label, visible

STATUS_STYLE = {
    Status.OK: "green",
    Status.BROKEN: "bold red",
    Status.WARNING: "yellow",
    Status.SKIPPED: "dim",
    Status.INFO: "cyan",
}


def render(
    report: Report,
    *,
    show_all: bool = False,
    color: bool = True,
    width: int = 100,
    verbose: int = 0,
    scanned: str = ".",
    quiet: bool = False,
) -> str:
    buf = io.StringIO()
    console = Console(
        file=buf, width=width, no_color=not color, force_terminal=color,
        highlight=False, soft_wrap=False, legacy_windows=False,
        # markup=False обязателен: [текст][ref] в ссылке — данные, а не теги rich
        markup=False,
    )
    table_box = box.ROUNDED if color else box.ASCII

    if not quiet:
        console.print(f"mdlink {__version__} · сканирую {scanned}")
        console.print()

    rows = visible(report, show_all)
    if not quiet:
        link_width = max(24, min(int(width * 0.42), 60))
        for file_name, items in group_by_file(rows, report).items():
            console.print(_file_heading(file_name, items, report, color))
            table = Table(box=table_box, show_edge=True, pad_edge=False,
                          expand=True, width=width)
            table.add_column("Стр.", justify="right", width=5)
            table.add_column("Ссылка", width=link_width, overflow="ignore", no_wrap=True)
            table.add_column("Статус", width=7)
            table.add_column("Детали", overflow="fold", ratio=1)
            for r in items:
                status_text = (str(r.http_status) if r.http_status
                               else status_label(r))
                detail = r.detail
                if r.elapsed_ms is not None:
                    detail = f"{r.code} ({r.elapsed_ms} ms)"
                table.add_row(
                    str(r.link.line),
                    truncate_middle(r.link.raw, link_width),
                    Text(status_text, style=STATUS_STYLE[r.status] if color else None),
                    detail,
                )
                if r.suggestion:
                    hint = truncate_middle(f"  ↳ возможно: {r.suggestion}", link_width)
                    table.add_row("", Text(hint, style="cyan" if color else None), "", "")
                if verbose and r.notes:
                    table.add_row("", Text(f"  · {', '.join(r.notes)}",
                                           style="dim" if color else None), "", "")
            console.print(table)
            console.print()

        if not rows:
            console.print(Text("Сломанных ссылок не найдено.",
                               style="green" if color else None))
            console.print()

    if not quiet:
        console.print("─" * width if color else "-" * width)
    console.print(f"Файлов: {report.files} · ссылок: {report.links} "
                  f"(уникальных URL: {report.unique_urls})")
    c = report.counts
    parts = [
        Text(f"✓ OK {c['ok']}", style="green" if color else None),
        Text(f"✗ BROKEN {c['broken']}", style="bold red" if color else None),
        Text(f"! WARNING {c['warning']}", style="yellow" if color else None),
        Text(f"– SKIPPED {c['skipped']}", style="dim" if color else None),
    ]
    line = Text("   ").join(parts)
    console.print(line)
    console.print(f"Время: {report.duration_ms / 1000:.1f} с")
    if report.interrupted:
        console.print(Text("Прогон прерван — отчёт частичный.",
                           style="yellow" if color else None))
    for hint in _hints(report, verbose):
        console.print(Text(hint, style="cyan" if color else None))
    return buf.getvalue()


def _file_heading(file_name: str, items, report: Report, color: bool) -> Text:
    """Путь кликабелен через OSC 8; терминалы без поддержки просто игнорируют
    escape-последовательность, а при --no-color она не выводится вовсе."""
    if not color:
        return Text(file_name)
    absolute = items[0].link.source_file.absolute().as_posix()
    return Text(file_name, style=f"bold link file://{absolute}")


def _hints(report: Report, verbose: int) -> list[str]:
    """§7.9: подсказка про --allow-private-hosts, если приватных > 30 %."""
    if verbose < 1:
        return []
    private = sum(1 for r in report.results if r.code == "private_host")
    external = sum(1 for r in report.results if r.link.kind == "HTTP")
    if external and private / external > 0.30:
        return ["совет: --allow-private-hosts проверит их, если сервисы подняты локально"]
    return []
