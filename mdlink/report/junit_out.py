"""JUnit XML: один <testcase> на ссылку (§9.3)."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..engine import Report
from ..models import Status
from ..textutil import nfc
from .common import visible


def render(report: Report, *, show_all: bool = False, **_kw) -> str:
    rows = visible(report, show_all)
    c = report.counts
    suite = ET.Element("testsuite", {
        "name": "mdlink",
        "tests": str(len(rows)),
        "failures": str(c["broken"]),
        "skipped": str(c["skipped"]),
        "time": f"{report.duration_ms / 1000:.3f}",
        "timestamp": report.started_at.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    for r in rows:
        file_name = nfc(report.display_path(r.link.source_file))
        case = ET.SubElement(suite, "testcase", {
            "classname": file_name,
            "name": f"{file_name}:{r.link.line} {nfc(r.link.raw)}",
            "time": f"{(r.elapsed_ms or 0) / 1000:.3f}",
        })
        message = f"{r.code}: {r.detail}"
        if r.suggestion:
            message += f" | возможно: {nfc(r.suggestion)}"
        if r.status is Status.BROKEN:
            ET.SubElement(case, "failure", {"type": r.code, "message": message}).text = message
        elif r.status is Status.WARNING:
            ET.SubElement(case, "system-out").text = message
        elif r.status is Status.SKIPPED:
            ET.SubElement(case, "skipped", {"message": message})
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"
