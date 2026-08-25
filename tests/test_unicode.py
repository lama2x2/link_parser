"""§13.2 «Unicode и кириллица», кейсы 15a–15h.

Обязательны к прогону на macOS: APFS/HFS+ отдают имена в NFD, и побайтовое
сравнение с NFC-строкой из Markdown даёт сплошные ложные срабатывания.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

from mdlink.checkers.local import LocalChecker
from mdlink.engine import Engine, ScanOptions
from mdlink.models import Link, LinkKind, Status
from mdlink.report import render
from mdlink.textutil import nfc

NAME = "Локальный запуск"
NFD = unicodedata.normalize("NFD", NAME)
NFC = unicodedata.normalize("NFC", NAME)


def _check(root: Path, source: Path, raw: str, **kw):
    return LocalChecker(root, **kw).check(
        Link(raw=raw, kind=LinkKind.LOCAL, source_file=source, line=1, column=1))


def test_nfc_and_nfd_really_differ():
    """Страховка: если формы совпали, остальные тесты ничего не проверяют."""
    assert NFD != NFC


# --- 15a: ссылка в NFC, каталог на диске в NFD ---

def test_15a_nfc_link_finds_nfd_directory(tmp_path: Path):
    (tmp_path / NFD).mkdir()
    (tmp_path / NFD / "index.md").write_text("# i\n", encoding="utf-8")
    source = tmp_path / "readme.md"
    source.write_text(f"[x](./{NFC}/index.md)\n", encoding="utf-8")

    assert os.listdir(tmp_path).count(NFD) == 1  # на диске именно NFD
    res = _check(tmp_path, source, f"./{NFC}/index.md")
    assert res.status is Status.OK, res.detail
    assert res.code != "file_not_found"


# --- 15b: обратный случай ---

def test_15b_nfd_link_finds_nfc_file(tmp_path: Path):
    (tmp_path / NFC).mkdir()
    (tmp_path / NFC / "index.md").write_text("# i\n", encoding="utf-8")
    source = tmp_path / "readme.md"
    source.write_text(f"[x](./{NFD}/index.md)\n", encoding="utf-8")

    res = _check(tmp_path, source, f"./{NFD}/index.md")
    assert res.status is Status.OK, res.detail


def test_nfd_difference_is_reported_as_info_note(tmp_path: Path):
    (tmp_path / NFD).mkdir()
    (tmp_path / NFD / "index.md").write_text("# i\n", encoding="utf-8")
    source = tmp_path / "readme.md"
    res = _check(tmp_path, source, f"./{NFC}/index.md")
    assert res.notes == ["unicode_nfd_filename"]


# --- 15c: percent-encoded кириллица ---

def test_15c_percent_encoded_cyrillic(sample: Path):
    res = _check(sample, sample / "ru/readme.md", "./Работа%20с%20Git.md")
    assert res.status is Status.OK


# --- 15d: неэкранированный пробел ---

def test_15d_unencoded_space_in_cyrillic_path(sample: Path):
    from mdlink.parser import MarkdownParser

    source = sample / "ru" / "readme.md"
    links = MarkdownParser().parse(source.read_text(encoding="utf-8"), source)
    target = [l for l in links if l.parse_code == "unencoded_space"]
    assert len(target) == 1
    res = LocalChecker(sample).check(target[0])
    assert res.status is Status.WARNING
    assert res.code == "unencoded_space"
    assert "<Работа с Git.md>" in res.suggestion


# --- 15e: регистр в кириллице ---

def test_15e_case_mismatch_lowercase_readme(sample: Path):
    res = _check(sample, sample / "ru/readme.md", "./README.md")
    assert res.code == "case_mismatch"
    assert res.suggestion == "./readme.md"


# --- 15f: casefold, а не lower ---

def test_15f_casefold_not_lower(sample: Path):
    res = _check(sample, sample / "ru/readme.md", "./РАБОТА С GIT.md")
    assert res.code == "case_mismatch"
    assert res.suggestion == "./Работа с Git.md"


def test_casefold_beats_lower_on_sharp_s(tmp_path: Path):
    """``lower()`` не сводит ``ß`` и ``SS``; ``casefold()`` — сводит."""
    (tmp_path / "straße.md").write_text("# s\n", encoding="utf-8")
    source = tmp_path / "a.md"
    res = _check(tmp_path, source, "./STRASSE.md")
    assert res.code == "case_mismatch"


# --- 15g: кириллический якорь ---

def test_15g_cyrillic_anchor(sample: Path):
    res = _check(sample, sample / "docs/guide/setup.md", "../api.md#Работа-с-ветками")
    assert res.status is Status.OK


def test_cyrillic_anchor_case_insensitive(sample: Path):
    res = _check(sample, sample / "docs/guide/setup.md", "../api.md#работа-с-ветками")
    assert res.status is Status.OK


# --- 15h: NFC в JSON-отчёте ---

def test_15h_json_report_names_are_nfc(tmp_path: Path):
    (tmp_path / NFD).mkdir()
    (tmp_path / NFD / "index.md").write_text("[нет](./missing.md)\n", encoding="utf-8")

    report = Engine(ScanOptions(path=tmp_path, root=tmp_path)).run()
    payload = json.loads(render("json", report))
    files = [r["file"] for r in payload["results"]]

    assert files == [f"{NFC}/index.md"]              # побайтовое сравнение с эталоном
    assert all(f == nfc(f) for f in files)
    assert NFD not in json.dumps(payload, ensure_ascii=False)


def test_nfc_used_in_suggestion(tmp_path: Path):
    (tmp_path / unicodedata.normalize("NFD", "Ёлка.md")).write_text("# ё\n", encoding="utf-8")
    source = tmp_path / "a.md"
    res = _check(tmp_path, source, "./ЁЛКА.md")
    assert res.code == "case_mismatch"
    assert res.suggestion == nfc(res.suggestion)


def test_filesystem_access_uses_listdir_name_not_normalized(tmp_path: Path):
    """§6.4.1 п.6: Path строится из имени, отданного listdir."""
    (tmp_path / NFD).mkdir()
    (tmp_path / NFD / "index.md").write_text("# i\n", encoding="utf-8")
    from mdlink.checkers.local import verify_path

    info = verify_path(tmp_path / NFC / "index.md", tmp_path)
    assert info.state == "ok"
    assert info.nfd is True
    assert NFD in str(info.real)          # на диск ходим по исходному имени
