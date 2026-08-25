"""Сбор якорей Markdown-файла и генерация slug'ов по правилам GitHub (§6.5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from ..textutil import closest, nfc

_md = MarkdownIt("gfm-like", {"html": True, "linkify": False})
_md.disable("linkify", ignoreInvalid=True)

_CUSTOM_ID_RE = re.compile(r"\s*\{#([^}\s]+)\}\s*$")
_ATTR_RE = re.compile(r"""(?:\bid|\bname)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


def slugify(text: str) -> str:
    """GitHub-совместимый slug: нижний регистр, только буквы/цифры/``-``/``_``,
    пробелы → дефисы. Unicode-буквы (в т.ч. кириллица) сохраняются."""
    s = nfc(text).strip().casefold()
    out: list[str] = []
    for ch in s:
        if ch in "-_":
            out.append(ch)
        elif ch.isspace():
            out.append("-")
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


@dataclass
class AnchorSet:
    """Якоря одного файла. Ключи хранятся в NFC + casefold (§6.4.1)."""

    slugs: list[str] = field(default_factory=list)
    _index: dict[str, str] = field(default_factory=dict)

    def add(self, anchor: str) -> None:
        key = nfc(anchor).casefold()
        if key not in self._index:
            self._index[key] = anchor
            self.slugs.append(anchor)

    def has(self, fragment: str) -> bool:
        return nfc(unquote(fragment)).casefold() in self._index

    def suggest(self, fragment: str) -> str | None:
        """Ближайший якорь по Левенштейну при дистанции ≤ 3 (§6.5 п.5)."""
        return closest(unquote(fragment), self.slugs, max_distance=3)


def _inline_text(tok: Token) -> str:
    parts: list[str] = []

    def walk(t: Token) -> None:
        if t.type in ("text", "code_inline"):
            parts.append(t.content)
        for child in t.children or ():
            walk(child)

    walk(tok)
    return "".join(parts)


def collect_anchors(text: str) -> AnchorSet:
    tokens = _md.parse(text)
    anchors = AnchorSet()
    seen: dict[str, int] = {}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            raw = _inline_text(inline) if inline is not None else ""
            explicit = tok.attrGet("id")
            m = _CUSTOM_ID_RE.search(raw)
            if m:
                raw = raw[: m.start()]
                anchors.add(m.group(1))
            if explicit:
                anchors.add(explicit)
            base = slugify(raw)
            if base:
                n = seen.get(base, 0)
                seen[base] = n + 1
                anchors.add(base if n == 0 else f"{base}-{n}")
            i += 2
            continue
        if tok.type in ("html_block", "inline"):
            for frag in _html_fragments(tok):
                for m in _ATTR_RE.finditer(frag):
                    value = m.group(1) or m.group(2) or m.group(3)
                    if value:
                        anchors.add(value)
        i += 1
    return anchors


def _html_fragments(tok: Token) -> list[str]:
    if tok.type == "html_block":
        return [tok.content]
    return [c.content for c in (tok.children or ()) if c.type == "html_inline"]


class AnchorCache:
    """Кэш по ``(path, mtime)`` (§6.5 п.6)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, float, int], AnchorSet] = {}

    def get(self, path: Path, text_loader) -> AnchorSet | None:
        try:
            st = path.stat()
            key = (str(path), st.st_mtime, st.st_size)
        except OSError:
            return None
        hit = self._cache.get(key)
        if hit is None:
            try:
                hit = collect_anchors(text_loader(path))
            except OSError:
                return None
            self._cache[key] = hit
        return hit
