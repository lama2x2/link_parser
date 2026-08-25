"""Таблица для комментария к PR (§9.3). GitHub-совместимо, без ANSI."""

from __future__ import annotations

from ..engine import Report
from ..textutil import nfc
from .common import group_by_file, status_label, visible

_ICON = {"BROKEN": "❌", "WARN": "⚠️", "OK": "✅", "SKIP": "➖", "INFO": "ℹ️"}


def _escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def render(report: Report, *, show_all: bool = False, **_kw) -> str:
    rows = visible(report, show_all)
    c = report.counts
    out: list[str] = ["## mdlink", ""]
    out.append(f"Файлов: **{report.files}** · ссылок: **{report.links}** "
               f"(уникальных URL: {report.unique_urls})")
    out.append("")
    out.append(f"✅ OK {c['ok']} · ❌ BROKEN {c['broken']} · "
               f"⚠️ WARNING {c['warning']} · ➖ SKIPPED {c['skipped']}")
    out.append("")
    if not rows:
        out.append("Сломанных ссылок не найдено. 🎉")
        return "\n".join(out) + "\n"

    for file_name, items in group_by_file(rows, report).items():
        out.append(f"### `{_escape(file_name)}`")
        out.append("")
        out.append("| Стр. | Ссылка | Статус | Код | Детали |")
        out.append("|---:|---|---|---|---|")
        for r in items:
            label = status_label(r)
            detail = _escape(r.detail)
            if r.suggestion:
                detail += f"<br>↳ возможно: `{_escape(nfc(r.suggestion))}`"
            out.append(
                f"| {r.link.line} | `{_escape(nfc(r.link.raw))}` | "
                f"{_ICON.get(label, '')} {label} | `{r.code}` | {detail} |"
            )
        out.append("")
    return "\n".join(out) + "\n"
