"""Общие фикстуры: копия sample-project в tmp_path + служебные помощники."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample-project"


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    """Изолированная копия fixture-проекта.

    Симлинки создаются здесь, а не в репозитории: битый симлинк в git —
    источник шума на Windows-раннерах.
    """
    dest = tmp_path / "sample-project"
    shutil.copytree(SAMPLE, dest)
    (dest / "edge" / "broken-symlink.md").symlink_to("./no-such-target.md")
    (dest / "edge" / "symlinks.md").write_text(
        "# Симлинки\n\n- [битый](./broken-symlink.md)\n", encoding="utf-8")
    return dest


@pytest.fixture
def no_backoff(monkeypatch):
    """Ретраи без реальных пауз."""
    async def instant(self, attempt):  # noqa: ANN001
        return None

    from mdlink.checkers.http import HttpChecker

    monkeypatch.setattr(HttpChecker, "_sleep_backoff", instant)


@pytest.fixture
def sleeps(monkeypatch):
    """Перехват asyncio.sleep внутри http-чекера: тест не ждёт по-настоящему."""
    import mdlink.checkers.http as http_mod

    recorded: list[float] = []
    real_sleep = http_mod.asyncio.sleep

    async def fake_sleep(delay, *a, **kw):
        recorded.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr(http_mod.asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def python() -> str:
    return sys.executable
