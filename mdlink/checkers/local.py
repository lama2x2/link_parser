"""Проверка локальных ссылок: резолвинг, регистр, Unicode, якоря (§6).

Ключевые инварианты (§14):
  * база относительных ссылок — ``source_file.parent``, не CWD и не root;
  * ссылка ``/...`` резолвится от ``--root``, не от корня ФС;
  * fragment и query отрезаются ДО резолвинга, затем ``unquote()``;
  * регистр проверяется явно через ``listdir``, сравнение — после NFC.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from ..discovery import read_markdown
from ..models import Link, LinkKind, Result, Status
from ..textutil import closest, nfc, same_name, same_name_ci
from .anchors import AnchorCache

MD_SUFFIXES = (".md", ".markdown", ".mdown", ".mkd")
INDEX_NAMES = {"readme.md", "index.md", "readme.markdown", "index.markdown"}


def split_target(raw: str) -> tuple[str, str, str]:
    """``./docs/api.md?v=2#install`` → (path, query, fragment) (§6.1)."""
    rest, _, fragment = raw.partition("#")
    path, _, query = rest.partition("?")
    return path, query, fragment


def resolve_local(link_path: str, source_file: Path, root: Path) -> Path:
    """Алгоритм §6.2. Нормализация ``..``/``.`` без обращения к ФС —
    ``os.path.normpath``, а не ``Path.resolve()``: последний разыменовал бы
    симлинки и сделал бы невозможной проверку ``broken_symlink``."""
    p = link_path.replace("\\", "/")          # 1. Windows-разделители → POSIX
    p = unquote(p)                             # 2. percent-decode
    if p.startswith("/"):                      # 3. выбор базы
        base = Path(os.path.abspath(root))     #    КОРЕНЬ ПРОЕКТА, не корень ФС
        p = p.lstrip("/")
    else:
        base = Path(os.path.abspath(source_file)).parent  # директория ТЕКУЩЕГО файла
    return Path(os.path.normpath(os.path.join(str(base), p)))  # 4. склейка


# --- проверка регистра и Unicode-нормализации (§6.4) --------------------------


@dataclass
class CaseInfo:
    state: str                      # ok | case | missing | permission | skip
    real: Path | None = None
    wrong: str | None = None
    actual: str | None = None
    siblings: list[str] = field(default_factory=list)
    nfd: bool = False


def verify_path(target: Path, anchor: Path) -> CaseInfo:
    """Пройти по компонентам пути от ``anchor`` вниз, сверяя каждый с
    ``os.listdir(parent)``. Обращение к ФС — только по исходным именам из
    ``listdir``; нормализованная форма живёт лишь в сравнениях (§6.4.1 п.6)."""
    try:
        parts = Path(os.path.relpath(target, anchor)).parts
    except ValueError:
        return CaseInfo("skip")
    if any(part == ".." for part in parts):
        return CaseInfo("skip")

    cur = anchor
    nfd = False
    for idx, comp in enumerate(parts):
        try:
            entries = os.listdir(cur)
        except PermissionError:
            return CaseInfo("permission", real=cur)
        except OSError:
            return CaseInfo("missing", real=cur, wrong=comp)
        exact = [e for e in entries if same_name(e, comp)]
        if exact:
            if exact[0] != comp:
                nfd = True                      # §6.4.1 п.3 — не ошибка
            cur = cur / exact[0]
            continue
        ci = [e for e in entries if same_name_ci(e, comp)]
        if ci:
            return CaseInfo("case", real=cur / ci[0], wrong=comp, actual=ci[0])
        return CaseInfo("missing", real=cur, wrong=comp, siblings=entries)
    return CaseInfo("ok", real=cur, nfd=nfd)


def _replace_component(written: str, wrong: str, actual: str) -> str:
    parts = written.split("/")
    for idx in range(len(parts) - 1, -1, -1):
        if same_name_ci(unquote(parts[idx]), wrong):
            parts[idx] = nfc(actual)
            break
    return "/".join(parts)


# --- чекер --------------------------------------------------------------------


class LocalChecker:
    def __init__(
        self,
        root: Path,
        *,
        check_anchors: bool = True,
        dir_index: bool = True,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.check_anchors = check_anchors
        self.dir_index = dir_index
        self.anchors = AnchorCache()
        self._name_index: dict[str, list[Path]] | None = None

    # -- точка входа --

    def check(self, link: Link) -> Result:
        if link.kind is LinkKind.FILE_URL:
            return self._check_file_url(link)
        if link.wikilink:
            return self._check_wikilink(link)

        path, _query, fragment = split_target(link.raw)

        if not path:
            if fragment:
                return self._check_anchor(link, link.source_file, fragment, own_file=True)
            return self._result(link, Status.BROKEN, "empty_link", "пустая ссылка")

        target = resolve_local(path, link.source_file, self.root)
        res = self._check_path(link, path, target, fragment)
        return self._merge_parse_verdict(link, res)

    # -- основная проверка пути --

    def _check_path(self, link: Link, written: str, target: Path, fragment: str) -> Result:
        inside = self._inside_root(target)

        if os.path.islink(target) and not os.path.exists(target):
            return self._result(link, Status.BROKEN, "broken_symlink",
                                f"битый симлинк → {os.readlink(target)}")

        if not inside:
            exists = os.path.exists(target)
            detail = "путь выходит за пределы --root"
            detail += "; файл существует" if exists else "; файла нет"
            return self._result(link, Status.WARNING, "outside_root", detail)

        info = verify_path(target, self.root)

        if info.state == "permission":
            return self._result(link, Status.WARNING, "permission_denied",
                                f"нет прав на чтение {self._rel(info.real)}")
        if info.state == "case":
            suggestion = _replace_component(written, info.wrong or "", info.actual or "")
            return self._result(
                link, Status.BROKEN, "case_mismatch",
                f"на диске имя записано как «{nfc(info.actual or '')}»",
                suggestion=suggestion,
            )
        if info.state == "missing":
            return self._result(link, Status.BROKEN, "file_not_found", "файл не найден",
                                suggestion=self._suggest_missing(written, target, info))
        if info.state == "skip" and not os.path.exists(target):
            return self._result(link, Status.BROKEN, "file_not_found", "файл не найден")

        real = info.real or target
        notes = ["unicode_nfd_filename"] if info.nfd else []

        if real.is_dir():
            index = self._dir_index(real)
            if index is None:
                return self._result(link, Status.WARNING, "link_to_directory",
                                    "ссылка на директорию без index-файла", notes=notes)
            if fragment and self.check_anchors:
                return self._check_anchor(link, index, fragment, notes=notes)
            return self._result(link, Status.OK, "ok",
                                f"директория, индекс {index.name}", notes=notes)

        if fragment and self.check_anchors and real.suffix.lower() in MD_SUFFIXES:
            return self._check_anchor(link, real, fragment, notes=notes)

        return self._result(link, Status.OK, "ok", "файл существует", notes=notes)

    # -- якоря --

    def _check_anchor(self, link: Link, target: Path, fragment: str,
                      own_file: bool = False, notes: list[str] | None = None) -> Result:
        anchors = self.anchors.get(target, read_markdown)
        if anchors is None:
            return self._result(link, Status.WARNING, "permission_denied",
                                f"не удалось прочитать {self._rel(target)}", notes=notes)
        if anchors.has(fragment):
            return self._result(link, Status.OK, "ok", "файл и якорь найдены", notes=notes)
        suggestion = anchors.suggest(fragment)
        where = "в текущем файле" if own_file else f"в {self._rel(target)}"
        return self._result(
            link, Status.WARNING, "anchor_not_found", f"якорь #{fragment} не найден {where}",
            suggestion=f"#{suggestion}" if suggestion else None, notes=notes,
        )

    # -- file:// --

    def _check_file_url(self, link: Link) -> Result:
        path, _q, fragment = split_target(link.raw)
        try:
            fs_path = Path(urllib.request.url2pathname(path[len("file://"):]))
        except Exception as exc:  # noqa: BLE001 — сообщаем, а не глотаем
            return self._result(link, Status.BROKEN, "file_not_found",
                                f"не удалось разобрать file:// URL: {exc}")
        if not os.path.exists(fs_path):
            return self._result(link, Status.BROKEN, "file_not_found",
                                f"файл не найден: {fs_path}")
        detail = "абсолютный file:// URL непереносим между машинами"
        if fragment and self.check_anchors and fs_path.suffix.lower() in MD_SUFFIXES:
            anchor_res = self._check_anchor(link, fs_path, fragment)
            if anchor_res.status is not Status.OK:
                return anchor_res
        return self._result(link, Status.WARNING, "absolute_file_url", detail)

    # -- wiki-ссылки (§5.5) --

    def _check_wikilink(self, link: Link) -> Result:
        target, _q, fragment = split_target(link.raw)
        target = target.strip()
        name = target if Path(target).suffix else f"{target}.md"
        candidates = self._lookup_basename(name)
        if not candidates:
            return self._result(link, Status.BROKEN, "wikilink_not_found",
                                f"файл «{nfc(name)}» не найден внутри --root")
        if len(candidates) > 1:
            listed = ", ".join(sorted(self._rel(c) for c in candidates)[:5])
            return self._result(link, Status.WARNING, "wikilink_ambiguous",
                                f"неоднозначно, кандидаты: {listed}")
        found = candidates[0]
        if fragment and self.check_anchors and found.suffix.lower() in MD_SUFFIXES:
            return self._check_anchor(link, found, fragment)
        return self._result(link, Status.OK, "ok", f"→ {self._rel(found)}")

    def _lookup_basename(self, name: str) -> list[Path]:
        if self._name_index is None:
            self._name_index = self._build_name_index()
        return self._name_index.get(nfc(name).casefold(), [])

    def _build_name_index(self) -> dict[str, list[Path]]:
        index: dict[str, list[Path]] = {}
        skip = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", ".next"}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for fn in filenames:
                index.setdefault(nfc(fn).casefold(), []).append(Path(dirpath) / fn)
        return index

    # -- вспомогательное --

    def _inside_root(self, target: Path) -> bool:
        try:
            return os.path.commonpath([str(target), str(self.root)]) == str(self.root)
        except ValueError:
            return False

    def _dir_index(self, directory: Path) -> Path | None:
        if not self.dir_index:
            return None
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        for entry in entries:
            if nfc(entry).casefold() in INDEX_NAMES:
                return directory / entry
        return None

    def _suggest_missing(self, written: str, target: Path, info: CaseInfo) -> str | None:
        """§8.3: похожее имя в той же директории либо недостающее расширение."""
        wrong = info.wrong or target.name
        if wrong == target.name:
            if not Path(wrong).suffix:
                for suffix in (".md", ".markdown"):
                    if any(same_name_ci(e, wrong + suffix) for e in info.siblings):
                        return f"{written}{suffix}"
            hit = closest(wrong, info.siblings, max_distance=2)
            if hit:
                return _replace_component(written, wrong, hit)
        return None

    def _rel(self, p: Path | None) -> str:
        if p is None:
            return "?"
        try:
            return Path(os.path.relpath(p, self.root)).as_posix()
        except ValueError:
            return p.as_posix()

    def _merge_parse_verdict(self, link: Link, res: Result) -> Result:
        """§5.3: пользователю нужны оба факта — и что синтаксис невалиден,
        и что происходит с целью."""
        if link.parse_code != "unencoded_space":
            return res
        if res.status is Status.BROKEN:
            res.detail = f"{res.detail}; к тому же пробел в цели не экранирован"
            res.suggestion = res.suggestion or link.parse_suggestion
            return res
        res.status = Status.WARNING
        res.code = "unencoded_space"
        res.detail = f"{link.parse_detail} (цель при этом существует)"
        res.suggestion = link.parse_suggestion
        return res

    @staticmethod
    def _result(link: Link, status: Status, code: str, detail: str,
                suggestion: str | None = None, notes: list[str] | None = None) -> Result:
        return Result(link=link, status=status, code=code, detail=detail,
                      suggestion=suggestion, notes=list(notes or []))
