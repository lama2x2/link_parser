"""Форматтеры отчёта. Никаких проверок здесь не выполняется (§12)."""

from __future__ import annotations

from ..engine import Report
from .json_out import render as render_json
from .junit_out import render as render_junit
from .markdown_out import render as render_markdown
from .pretty import render as render_pretty

FORMATS = {
    "pretty": render_pretty,
    "json": render_json,
    "markdown": render_markdown,
    "junit": render_junit,
}


def render(fmt: str, report: Report, **kwargs) -> str:
    return FORMATS[fmt](report, **kwargs)


__all__ = ["FORMATS", "render", "Report"]
