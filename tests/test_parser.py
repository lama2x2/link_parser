"""§13.2 «Парсинг», кейсы 1–7."""

from __future__ import annotations

from pathlib import Path

import pytest

from mdlink.models import LinkKind
from mdlink.parser import MarkdownParser, classify, split_dest_title

SRC = Path("/proj/doc.md")


def parse(text: str, **kw) -> list:
    return MarkdownParser(**kw).parse(text, SRC)


def raws(text: str, **kw) -> list[str]:
    return [l.raw for l in parse(text, **kw)]


# --- 1–4: код и комментарии никогда не извлекаются ---

def test_1_fenced_block_not_extracted():
    text = "Начало\n\n```\n[a](./x.md)\n```\n\n[real](./y.md)\n"
    assert raws(text) == ["./y.md"]


def test_1b_tilde_fence_not_extracted():
    assert raws("~~~\n[a](./x.md)\n~~~\n\n[real](./y.md)\n") == ["./y.md"]


def test_2_indented_block_not_extracted():
    text = "Текст\n\n    [a](./x.md)\n\n[real](./y.md)\n"
    assert raws(text) == ["./y.md"]


def test_3_inline_code_not_extracted():
    assert raws("`[a](./x.md)` и [real](./y.md)\n") == ["./y.md"]


def test_3b_double_backtick_span():
    assert raws("``code with ` and [a](./x.md)`` и [real](./y.md)\n") == ["./y.md"]


def test_4_html_comment_not_extracted():
    assert raws("<!-- [a](./x.md) -->\n\n[real](./y.md)\n") == ["./y.md"]


def test_4b_multiline_comment_not_extracted():
    text = "<!--\n[a](./x.md)\n[b](./z.md)\n-->\n\n[real](./y.md)\n"
    assert raws(text) == ["./y.md"]


def test_escaped_brackets_not_a_link():
    assert raws("\\[not a link\\](x)\n") == []


# --- 5: reference-ссылки ---

def test_5_reference_link_resolved():
    text = "Смотри [текст][ref].\n\n[ref]: ./a.md\n"
    links = parse(text)
    assert [l.raw for l in links] == ["./a.md"]
    assert links[0].parse_code is None


def test_5b_collapsed_and_shortcut_reference():
    text = "[ref][] и [ref]\n\n[ref]: ./a.md\n"
    assert raws(text) == ["./a.md", "./a.md"]


def test_5c_undefined_reference():
    links = parse("Смотри [текст][nosuch].\n")
    assert len(links) == 1
    assert links[0].parse_code == "undefined_reference"


# --- 6: две одинаковые ссылки в одном абзаце ---

def test_6_duplicate_links_get_distinct_positions():
    text = "Смотри [док](./a.md)\nи ещё раз [док](./a.md) там же.\n"
    links = parse(text)
    assert len(links) == 2
    assert links[0].line == 1 and links[1].line == 2
    assert links[0].column != links[1].column


def test_6b_duplicates_on_one_line_get_distinct_columns():
    links = parse("[a](./a.md) и [a](./a.md)\n")
    assert [l.line for l in links] == [1, 1]
    assert links[0].column < links[1].column


# --- 7: autolink ---

def test_7_autolink_is_http():
    links = parse("<https://example.com>\n")
    assert links[0].raw == "https://example.com"
    assert links[0].kind is LinkKind.HTTP


# --- прочее из §5 ---

def test_bare_url_ignored_by_default():
    assert raws("Смотри https://example.com в тексте\n") == []


def test_bare_url_with_flag():
    assert raws("Смотри https://example.com в тексте\n", bare_urls=True) == \
        ["https://example.com"]


def test_images_extracted_and_can_be_disabled():
    text = "![alt](./img.png)\n"
    assert raws(text) == ["./img.png"]
    assert raws(text, check_images=False) == []


def test_raw_html_links():
    text = '<a href="./a.md">x</a>\n\n<p>инлайн <img src="./i.png" alt="i"></p>\n'
    assert set(raws(text)) == {"./a.md", "./i.png"}


def test_title_is_stripped_from_destination():
    links = parse('[a](./x.md "Заголовок")\n')
    assert links[0].raw == "./x.md"
    assert links[0].title == "Заголовок"


def test_angle_wrapped_destination_with_spaces():
    links = parse("[a](<./file with spaces.md>)\n")
    assert links[0].raw == "./file with spaces.md"
    assert links[0].parse_code is None


def test_unencoded_space_second_pass():
    links = parse("[текст](Работа с Git.md)\n")
    assert len(links) == 1
    assert links[0].parse_code == "unencoded_space"
    assert links[0].raw == "Работа с Git.md"
    assert "<Работа с Git.md>" in links[0].parse_suggestion


def test_unencoded_space_not_reported_for_title_only():
    links = parse('[a](./x.md "Заголовок с пробелами")\n')
    assert [l.parse_code for l in links] == [None]


def test_wikilinks_disabled_by_default():
    assert raws("[[Работа с Git]]\n") == []


@pytest.mark.parametrize("text,expected_raw,is_image", [
    ("[[Работа с Git]]", "Работа с Git", False),
    ("[[Работа с Git|как коммитить]]", "Работа с Git", False),
    ("[[Работа с Git#Ветки]]", "Работа с Git#Ветки", False),
    ("![[logo.png]]", "logo.png", True),
])
def test_wikilink_forms(text, expected_raw, is_image):
    links = parse(text + "\n", wikilinks=True)
    assert len(links) == 1
    assert links[0].raw == expected_raw
    assert links[0].is_image is is_image
    assert links[0].wikilink is True


@pytest.mark.parametrize("dest,kind", [
    ("https://x.dev", LinkKind.HTTP),
    ("http://x.dev", LinkKind.HTTP),
    ("./a.md", LinkKind.LOCAL),
    ("/docs/a.md", LinkKind.LOCAL),
    ("#anchor", LinkKind.ANCHOR_ONLY),
    ("file:///tmp/a.md", LinkKind.FILE_URL),
    ("mailto:a@b.c", LinkKind.MAILTO),
    ("tel:+123", LinkKind.MAILTO),
    ("ftp://x", LinkKind.OTHER),
    ("javascript:void(0)", LinkKind.OTHER),
])
def test_classify(dest, kind):
    assert classify(dest) is kind


def test_split_dest_title():
    assert split_dest_title('  <./a b.md>  "T"  ') == ("./a b.md", "T")
    assert split_dest_title("./a.md") == ("./a.md", None)


def test_crlf_does_not_shift_line_numbers():
    links = parse("# H\r\n\r\nтекст\r\n\r\n[a](./x.md)\r\n")
    assert links[0].line == 5


def test_bom_is_stripped():
    """BOM снят до парсинга, поэтому column указывает на `.` в `./x.md`."""
    links = parse("﻿[a](./x.md)\n")
    assert (links[0].line, links[0].column) == (1, 5)


def test_empty_file_is_valid():
    assert parse("") == []
