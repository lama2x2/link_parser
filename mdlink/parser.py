"""Извлечение ссылок из Markdown (§5).

Слой не знает ни о файловой системе, ни о сети: на входе текст, на выходе
``list[Link]``. Основной источник истины — AST ``markdown-it-py``; регулярки
применяются **только** к тексту, из которого предварительно вырезаны блоки
кода, инлайн-код и HTML-комментарии (см. :func:`build_code_mask`).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .models import Link, LinkKind

# --- нормализация исходника (§4.3) -------------------------------------------

_BOM = "﻿"


def normalize_text(text: str) -> str:
    """Снять BOM и привести переводы строк к ``\\n`` ДО парсинга."""
    if text.startswith(_BOM):
        text = text[len(_BOM):]
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- маскирование кода --------------------------------------------------------


def _iter_tokens(tokens: list[Token]):
    for tok in tokens:
        yield tok
        if tok.children:
            yield from _iter_tokens(tok.children)


def _code_span_ranges(s: str) -> list[tuple[int, int]]:
    """Диапазоны инлайн-кода: открывающая серия из N бэктиков закрывается
    серией ровно из N бэктиков (правило CommonMark)."""
    out: list[tuple[int, int]] = []
    i, n = 0, len(s)
    while i < n:
        if s[i] != "`" or (i and s[i - 1] == "\\"):
            i += 1
            continue
        j = i
        while j < n and s[j] == "`":
            j += 1
        run = j - i
        k, closed = j, -1
        while k < n:
            if s[k] == "`":
                m = k
                while m < n and s[m] == "`":
                    m += 1
                if m - k == run:
                    closed = m
                    break
                k = m
            else:
                k += 1
        if closed > 0:
            out.append((i, closed))
            i = closed
        else:
            i = j
    return out


def build_code_mask(text: str, tokens: list[Token]) -> str:
    """Копия ``text`` той же длины, где содержимое кода и HTML-комментариев
    заменено пробелами. Переводы строк сохраняются, поэтому смещения,
    найденные в маске, валидны и для оригинала."""
    line_start = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
    buf = list(text)

    def blank(a: int, b: int) -> None:
        for i in range(max(a, 0), min(b, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "

    for tok in _iter_tokens(tokens):
        if tok.type in ("fence", "code_block") and tok.map:
            a = line_start[tok.map[0]] if tok.map[0] < len(line_start) else len(text)
            b = line_start[tok.map[1]] if tok.map[1] < len(line_start) else len(text)
            blank(a, b)

    masked = "".join(buf)
    for m in re.finditer(r"<!--.*?(?:-->|\Z)", masked, re.DOTALL):
        blank(m.start(), m.end())

    masked = "".join(buf)
    for a, b in _code_span_ranges(masked):
        blank(a, b)

    return "".join(buf)


# --- разбор destination -------------------------------------------------------

_TITLE_RE = re.compile(r"""\s+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\((?:[^)\\]|\\.)*\))\s*$""")


def split_dest_title(raw: str) -> tuple[str, str | None]:
    """``./x.md "Заголовок"`` → (``./x.md``, ``Заголовок``). Порядок операций §5.3."""
    s = raw.strip()
    title = None
    m = _TITLE_RE.search(s)
    if m:
        title = m.group(0).strip()[1:-1]
        s = s[: m.start()].strip()
    if s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1]
    return s, title


_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


def classify(dest: str) -> LinkKind:
    """Определить :class:`LinkKind` по записанной цели."""
    s = dest.strip()
    if not s:
        return LinkKind.LOCAL
    if s.startswith("#"):
        return LinkKind.ANCHOR_ONLY
    if s.startswith("//"):
        return LinkKind.HTTP
    m = _SCHEME_RE.match(s)
    if not m:
        return LinkKind.LOCAL
    scheme = m.group(1).lower()
    if scheme in ("http", "https"):
        return LinkKind.HTTP
    if scheme == "file":
        return LinkKind.FILE_URL
    if scheme in ("mailto", "tel"):
        return LinkKind.MAILTO
    return LinkKind.OTHER


# --- поиск позиции (§5.2) -----------------------------------------------------


class _Locator:
    """Ищет `raw` внутри строкового диапазона блока, помня израсходованные
    совпадения: две одинаковые ссылки в одном абзаце получают разные позиции."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.line_start = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self.line_start.append(len(text) + 1)
        self._used: dict[tuple[int, int, str], int] = {}

    def offset_to_pos(self, off: int) -> tuple[int, int]:
        lo, hi = 0, len(self.line_start) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.line_start[mid] <= off:
                lo = mid
            else:
                hi = mid
        return lo + 1, off - self.line_start[lo] + 1

    def locate(self, span: tuple[int, int] | None, candidates: list[str]) -> tuple[int, int, str | None]:
        """→ (line, column, найденный_вариант). Если не нашли — column = 0."""
        if span is None:
            return 1, 0, None
        start_line, end_line = span
        a = self.line_start[min(start_line, len(self.line_start) - 1)]
        b = self.line_start[min(end_line, len(self.line_start) - 1)]
        window = self.text[a:b]
        for cand in candidates:
            if not cand:
                continue
            key = (start_line, end_line, cand)
            skip = self._used.get(key, 0)
            pos, found = -1, -1
            search_from = 0
            for _ in range(skip + 1):
                pos = window.find(cand, search_from)
                if pos < 0:
                    break
                search_from = pos + 1
                found = pos
            if found >= 0:
                self._used[key] = skip + 1
                line, col = self.offset_to_pos(a + found)
                return line, col, cand
        return start_line + 1, 0, None


# --- сырой HTML ---------------------------------------------------------------


class _HtmlLinkExtractor(HTMLParser):
    """Достаёт href/src из html_block / html_inline токенов.
    Содержимое комментариев игнорируется самим HTMLParser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, bool, str | None]] = []  # (dest, is_image, title)

    def handle_starttag(self, tag: str, attrs) -> None:
        d = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and "href" in d:
            self.found.append((d["href"], False, d.get("title")))
        elif tag in ("img", "source") and "src" in d:
            self.found.append((d["src"], True, d.get("alt")))

    handle_startendtag = handle_starttag


def _html_links(fragment: str) -> list[tuple[str, bool, str | None]]:
    p = _HtmlLinkExtractor()
    try:
        p.feed(fragment)
        p.close()
    except (AssertionError, ValueError):
        # Битый HTML не должен ронять прогон: возвращаем то, что успели разобрать.
        # Молчаливого проглатывания нет — незакрытый тег просто не даёт ссылок.
        pass
    return p.found


# --- второй проход по маске ---------------------------------------------------

_UNENCODED_RE = re.compile(r"\[[^\]\n]*\]\(([^)\n]*\s[^)\n]*)\)")
_REFUSE_RE = re.compile(r"\[([^\]\n]*)\]\[([^\]\n]*)\]")
_WIKI_RE = re.compile(r"(!?)\[\[([^\]\n|#]+)(?:#([^\]\n|]+))?(?:\|([^\]\n]*))?\]\]")


def _escaped(text: str, pos: int) -> bool:
    """Нечётное число обратных слэшей перед позицией → символ экранирован."""
    n = 0
    i = pos - 1
    while i >= 0 and text[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1


def normalize_ref_label(label: str) -> str:
    """Нормализация метки ссылки по CommonMark: свёртка пробелов + casefold."""
    return re.sub(r"\s+", " ", label.strip()).casefold()


# --- основной парсер ----------------------------------------------------------


class MarkdownParser:
    def __init__(
        self,
        *,
        check_images: bool = True,
        wikilinks: bool = False,
        bare_urls: bool = False,
    ) -> None:
        self.check_images = check_images
        self.wikilinks = wikilinks
        self.bare_urls = bare_urls
        if bare_urls:
            try:
                import linkify_it  # noqa: F401
            except ModuleNotFoundError:  # pragma: no cover - зависит от окружения
                raise RuntimeError(
                    "--bare-urls требует пакет linkify-it-py: pip install linkify-it-py"
                ) from None
        self.md = MarkdownIt("gfm-like", {"linkify": bare_urls, "html": True})
        if not bare_urls:
            self.md.disable("linkify", ignoreInvalid=True)

    # -- публичный API --

    def parse(self, text: str, source_file: Path) -> list[Link]:
        text = normalize_text(text)
        env: dict = {}
        tokens = self.md.parse(text, env)
        loc = _Locator(text)
        links: list[Link] = []
        seen_positions: set[tuple[int, int]] = set()

        for link in self._from_ast(tokens, loc, source_file):
            links.append(link)
            if link.column:
                seen_positions.add((link.line, link.column))

        masked = build_code_mask(text, tokens)
        refs = {normalize_ref_label(k) for k in env.get("references", {})}
        links.extend(self._undefined_refs(masked, loc, source_file, refs))
        links.extend(self._unencoded_spaces(masked, loc, source_file, links))
        if self.wikilinks:
            links.extend(self._wikilinks(masked, loc, source_file))

        links.sort(key=lambda l: (l.line, l.column))
        return links

    # -- AST --

    def _from_ast(self, tokens: list[Token], loc: _Locator, src: Path):
        for tok in tokens:
            if tok.type == "inline":
                yield from self._from_inline(tok, loc, src)
            elif tok.type == "html_block":
                span = tuple(tok.map) if tok.map else None
                for dest, is_img, title in _html_links(tok.content):
                    link = self._make(dest, is_img, title, span, loc, src)
                    if link:
                        yield link
            elif tok.children:
                yield from self._from_ast(tok.children, loc, src)

    def _from_inline(self, inline: Token, loc: _Locator, src: Path):
        span = tuple(inline.map) if inline.map else None
        children = inline.children or []
        i = 0
        while i < len(children):
            tok = children[i]
            if tok.type == "code_inline":
                i += 1
                continue
            if tok.type == "link_open":
                href = tok.attrGet("href") or ""
                title = tok.attrGet("title")
                text_parts, j = [], i + 1
                depth = 1
                while j < len(children):
                    if children[j].type == "link_open":
                        depth += 1
                    elif children[j].type == "link_close":
                        depth -= 1
                        if depth == 0:
                            break
                    elif children[j].type in ("text", "code_inline"):
                        text_parts.append(children[j].content)
                    j += 1
                label = "".join(text_parts) or None
                link = self._make(href, False, title or label, span, loc, src,
                                  extra_candidates=[f"[{label}]"] if label else None)
                if link:
                    yield link
                i = j + 1
                continue
            if tok.type == "image":
                if self.check_images:
                    src_attr = tok.attrGet("src") or ""
                    alt = tok.content or tok.attrGet("alt") or None
                    link = self._make(src_attr, True, alt, span, loc, src,
                                      extra_candidates=[f"![{alt}]"] if alt else None)
                    if link:
                        yield link
                i += 1
                continue
            if tok.type == "html_inline":
                for dest, is_img, title in _html_links(tok.content):
                    link = self._make(dest, is_img, title, span, loc, src)
                    if link:
                        yield link
            i += 1

    def _make(
        self,
        dest: str,
        is_image: bool,
        title: str | None,
        span: tuple[int, int] | None,
        loc: _Locator,
        src: Path,
        extra_candidates: list[str] | None = None,
    ) -> Link | None:
        if is_image and not self.check_images:
            return None
        clean, inline_title = split_dest_title(dest)
        title = title or inline_title
        decoded = unquote(clean)
        candidates = [
            clean, dest.strip(), decoded,
            decoded.replace(" ", "%20"),   # ./Работа%20с%20Git.md — как пишут люди
            f"<{clean}>", f"<{decoded}>",
        ]
        if extra_candidates:
            candidates.extend(extra_candidates)
        line, column, found = loc.locate(span, candidates)
        raw = clean
        if found and not found.startswith("[") and not found.startswith("!["):
            raw = found[1:-1] if found.startswith("<") and found.endswith(">") else found
        return Link(
            raw=raw,
            kind=classify(raw),
            source_file=src,
            line=line,
            column=column,
            is_image=is_image,
            title=title,
        )

    # -- второй проход: неопределённые reference-ссылки (§5.1) --

    def _undefined_refs(self, masked: str, loc: _Locator, src: Path, refs: set[str]):
        for m in _REFUSE_RE.finditer(masked):
            if _escaped(masked, m.start()):
                continue
            text, label = m.group(1), m.group(2)
            key = normalize_ref_label(label or text)
            if not key or key in refs:
                continue
            line, col = loc.offset_to_pos(m.start())
            yield Link(
                raw=m.group(0),
                kind=LinkKind.OTHER,
                source_file=src,
                line=line,
                column=col,
                title=text or None,
                parse_code="undefined_reference",
                parse_detail=f"определение [{label or text}]: не найдено в файле",
            )

    # -- второй проход: неэкранированные пробелы (§5.3) --

    def _unencoded_spaces(self, masked: str, loc: _Locator, src: Path, known: list[Link]):
        known_raw = {(l.line, l.raw) for l in known}
        for m in _UNENCODED_RE.finditer(masked):
            if _escaped(masked, m.start()):
                continue
            dest, title = split_dest_title(m.group(1))
            if not dest or " " not in dest and "\t" not in dest:
                continue  # пробел был только в title — ссылка валидна
            line, col = loc.offset_to_pos(m.start(1))
            if (line, dest) in known_raw:
                continue  # запись вида [a](<путь с пробелом.md>) — AST её уже видел
            encoded = dest.replace(" ", "%20")
            yield Link(
                raw=dest,
                kind=classify(dest),
                source_file=src,
                line=line,
                column=col,
                title=title,
                parse_code="unencoded_space",
                parse_detail=("пробел в цели ссылки не экранирован — CommonMark не "
                              f"считает это ссылкой; допустима и запись {encoded}"),
                parse_suggestion=f"<{dest}>",
            )

    # -- wiki-ссылки (§5.5) --

    def _wikilinks(self, masked: str, loc: _Locator, src: Path):
        for m in _WIKI_RE.finditer(masked):
            if _escaped(masked, m.start()):
                continue
            bang, target, anchor, text = m.groups()
            target = target.strip()
            if not target:
                continue
            raw = target + (f"#{anchor.strip()}" if anchor else "")
            line, col = loc.offset_to_pos(m.start())
            yield Link(
                raw=raw,
                kind=LinkKind.LOCAL,
                source_file=src,
                line=line,
                column=col,
                is_image=bool(bang),
                title=(text or target).strip(),
                wikilink=True,
            )
