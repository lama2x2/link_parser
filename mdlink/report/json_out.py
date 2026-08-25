"""Машиночитаемый отчёт (§9.2). Схема стабильна — по ней парсит CI."""

from __future__ import annotations

import json

from ..engine import Report
from ..textutil import nfc
from .common import visible


def render(report: Report, *, show_all: bool = False, **_kw) -> str:
    payload = {
        "version": "1.0",
        "root": str(report.root),
        "started_at": report.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": report.duration_ms,
        "summary": {
            "files": report.files,
            "links": report.links,
            "unique_urls": report.unique_urls,
            **report.counts,
        },
        "results": [_result(report, r) for r in visible(report, show_all)],
    }
    if report.interrupted:
        payload["interrupted"] = True
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _result(report: Report, r) -> dict:
    return {
        # §6.4.1 п.5 — имена всегда в NFC, независимо от того, как лежат на диске
        "file": nfc(report.display_path(r.link.source_file)),
        "line": r.link.line,
        "column": r.link.column,
        "raw": nfc(r.link.raw),
        "kind": str(r.link.kind),
        "is_image": r.link.is_image,
        "status": str(r.status),
        "code": r.code,
        "detail": r.detail,
        "http_status": r.http_status,
        "final_url": r.final_url,
        "elapsed_ms": r.elapsed_ms,
        "suggestion": nfc(r.suggestion) if r.suggestion else None,
    }
