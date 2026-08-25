"""§13.3: нефункциональные требования."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mdlink.engine import Engine, ScanOptions


@pytest.mark.slow
def test_500_files_local_only_under_3s(tmp_path: Path):
    for i in range(500):
        directory = tmp_path / f"d{i % 20}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{i}.md").write_text(
            f"# Файл {i}\n\n## Раздел\n\n"
            f"- [сосед](./f{(i + 1) % 500}.md)\n"
            f"- [корень](/d0/f0.md)\n"
            f"- [якорь](#раздел)\n",
            encoding="utf-8")

    started = time.monotonic()
    report = Engine(ScanOptions(path=tmp_path, root=tmp_path,
                                check_external=False)).run()
    elapsed = time.monotonic() - started

    assert report.files == 500
    assert report.links == 1500
    assert elapsed < 3.0, f"локальная проверка заняла {elapsed:.2f} с"


@pytest.mark.slow
def test_peak_memory_under_200mb(tmp_path: Path):
    import tracemalloc

    for i in range(200):
        (tmp_path / f"f{i}.md").write_text(
            "# H\n\n" + "".join(f"- [x](./f{j}.md)\n" for j in range(20)),
            encoding="utf-8")

    tracemalloc.start()
    Engine(ScanOptions(path=tmp_path, root=tmp_path, check_external=False)).run()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 200 * 1024 * 1024, f"пик {peak / 1024 / 1024:.0f} МБ"
