"""Общие текстовые утилиты: NFC-нормализация, расстояние Левенштейна, усечение."""

from __future__ import annotations

import unicodedata


def nfc(s: str) -> str:
    """Единственная точка нормализации имён (§6.4.1, п.1)."""
    return unicodedata.normalize("NFC", s)


def same_name(a: str, b: str) -> bool:
    """Точное совпадение имён после NFC (§6.4.1, п.2)."""
    return nfc(a) == nfc(b)


def same_name_ci(a: str, b: str) -> bool:
    """Регистронезависимое совпадение: casefold(), а не lower() (§6.4.1, п.2)."""
    return nfc(a).casefold() == nfc(b).casefold()


def is_nfd_variant(a: str, b: str) -> bool:
    """Строки визуально равны, но различаются побайтово → разная нормализация."""
    return a != b and nfc(a) == nfc(b)


def levenshtein(a: str, b: str, limit: int | None = None) -> int:
    """Классическая динамика по строкам; ``limit`` даёт ранний выход."""
    if a == b:
        return 0
    if limit is not None and abs(len(a) - len(b)) > limit:
        return limit + 1
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(val)
            best = min(best, val)
        prev = cur
        if limit is not None and best > limit:
            return limit + 1
    return prev[-1]


def closest(needle: str, candidates: list[str], max_distance: int = 2) -> str | None:
    """Ближайший кандидат по Левенштейну (регистронезависимо), либо None."""
    best: str | None = None
    best_d = max_distance + 1
    n = nfc(needle).casefold()
    for cand in candidates:
        d = levenshtein(n, nfc(cand).casefold(), limit=max_distance)
        if d < best_d:
            best_d, best = d, cand
    return best if best_d <= max_distance else None


def truncate_middle(s: str, width: int) -> str:
    """Усечение посередине с сохранением хвоста (§9.1)."""
    if width <= 0 or len(s) <= width:
        return s
    if width <= 3:
        return s[:width]
    keep = width - 1  # место под "…"
    head = max(keep // 2, 1)
    tail = keep - head
    return f"{s[:head]}…{s[-tail:]}" if tail else f"{s[:head]}…"
