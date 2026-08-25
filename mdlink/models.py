"""Внутренний контракт между модулями (§3).

Все пути в отчёте — POSIX-стиль, относительно ``--root``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class LinkKind(StrEnum):
    HTTP = "HTTP"
    LOCAL = "LOCAL"
    FILE_URL = "FILE_URL"
    ANCHOR_ONLY = "ANCHOR_ONLY"
    MAILTO = "MAILTO"
    OTHER = "OTHER"


class Status(StrEnum):
    OK = "OK"
    BROKEN = "BROKEN"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"
    INFO = "INFO"


@dataclass(frozen=True)
class Link:
    raw: str
    kind: LinkKind
    source_file: Path
    line: int
    column: int
    is_image: bool = False
    title: str | None = None
    # --- служебные поля, в отчёт не попадают ---
    wikilink: bool = False
    # предварительный вердикт парсера (§5.3 unencoded_space, §5.1 undefined_reference)
    parse_code: str | None = None
    parse_detail: str | None = None
    parse_suggestion: str | None = None


@dataclass
class Result:
    link: Link
    status: Status
    code: str
    detail: str
    http_status: int | None = None
    final_url: str | None = None
    elapsed_ms: int | None = None
    suggestion: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def counts_as_failure(self) -> bool:
        return self.status is Status.BROKEN


# --- Коды результатов (§8.1). Публичный контракт. ---

BROKEN_CODES = frozenset({
    "file_not_found", "broken_symlink", "case_mismatch", "empty_link",
    "undefined_reference", "wikilink_not_found", "not_found", "client_error",
    "server_error", "timeout", "dns_error", "connection_error", "ssl_error",
    "too_many_redirects",
})

WARNING_CODES = frozenset({
    "anchor_not_found", "link_to_directory", "outside_root", "permission_denied",
    "absolute_file_url", "unencoded_space", "wikilink_ambiguous", "auth_required",
    "rate_limited", "insecure_redirect", "file_too_large",
})

INFO_CODES = frozenset({"unicode_nfd_filename"})

SKIPPED_CODES = frozenset({
    "private_host", "unsupported_scheme", "ignored_by_pattern", "host_unreachable",
    "external_disabled", "cached", "local_disabled",
})

ALL_CODES = BROKEN_CODES | WARNING_CODES | INFO_CODES | SKIPPED_CODES
