"""§13.2 «Интеграция», кейсы 27–32. Проверяются реальные коды выхода."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None, env: dict | None = None):
    full_env = {**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1", **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "mdlink", *args],
        capture_output=True, text=True, cwd=cwd or ROOT, env=full_env, timeout=120,
    )


@pytest.fixture
def clean_project(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text("# API\n\n## Установка\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# Проект\n\n[api](./docs/api.md)\n[якорь](./docs/api.md#установка)\n",
        encoding="utf-8")
    return tmp_path


@pytest.fixture
def broken_project(clean_project: Path) -> Path:
    (clean_project / "BROKEN.md").write_text("[нет](./missing.md)\n", encoding="utf-8")
    return clean_project


# --- 27: чистый проект ---

def test_27_clean_project_exit_zero_and_json_parses(clean_project: Path):
    proc = run(str(clean_project), "--no-external", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["version"] == "1.0"
    assert payload["summary"]["broken"] == 0
    assert payload["results"] == []


def test_27b_json_schema_fields(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "--format", "json")
    payload = json.loads(proc.stdout)
    row = payload["results"][0]
    assert set(row) == {
        "file", "line", "column", "raw", "kind", "is_image", "status", "code",
        "detail", "http_status", "final_url", "elapsed_ms", "suggestion",
    }
    assert row["file"] == "BROKEN.md"          # POSIX-путь относительно root
    assert not Path(row["file"]).is_absolute()


# --- 28–29: коды выхода ---

def test_28_broken_link_exit_one(broken_project: Path):
    assert run(str(broken_project), "--no-external").returncode == 1


def test_29_fail_on_never_exit_zero(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "--fail-on", "never")
    assert proc.returncode == 0


def test_29b_fail_on_warning_promotes_warnings(clean_project: Path):
    (clean_project / "WARN.md").write_text(
        "[якорь](./docs/api.md#нет-такого)\n", encoding="utf-8")
    assert run(str(clean_project), "--no-external").returncode == 0
    assert run(str(clean_project), "--no-external",
               "--fail-on", "warning").returncode == 1


# --- 30: невалидный ввод ---

def test_30_missing_path_exit_two():
    proc = run("/definitely/not/here")
    assert proc.returncode == 2
    assert "не существует" in proc.stderr


def test_30b_broken_config_exit_two(clean_project: Path):
    cfg = clean_project / "bad.toml"
    cfg.write_text("timeout = 'не число'\n", encoding="utf-8")
    proc = run(str(clean_project), "--config", str(cfg), "--no-external")
    assert proc.returncode == 2
    assert "timeout" in proc.stderr


def test_30c_unknown_config_key_names_key_and_line(clean_project: Path):
    cfg = clean_project / "bad.toml"
    cfg.write_text("timeout = 5.0\nbogus-key = 1\n", encoding="utf-8")
    proc = run(str(clean_project), "--config", str(cfg), "--no-external")
    assert proc.returncode == 2
    assert "bogus-key" in proc.stderr and ":2" in proc.stderr


def test_30d_path_and_repo_are_mutually_exclusive(clean_project: Path):
    proc = run(str(clean_project), "--repo", "owner/name")
    assert proc.returncode == 2
    assert "взаимоисключим" in proc.stderr


def test_repo_spec_validation():
    proc = run("--repo", "not-a-spec")
    assert proc.returncode == 2
    assert "owner/name" in proc.stderr


# --- 31: цвет ---

def test_31_no_color_has_no_escapes(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "--no-color")
    assert "\x1b" not in proc.stdout


def test_31b_piped_output_has_no_escapes(broken_project: Path):
    """stdout не TTY → цвета выключаются автоматически."""
    proc = run(str(broken_project), "--no-external", env={"NO_COLOR": ""})
    assert "\x1b" not in proc.stdout


def test_31c_report_to_stdout_diagnostics_to_stderr(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "-v")
    assert "BROKEN" in proc.stdout
    assert "Файлов:" in proc.stdout


# --- 32: Ctrl+C ---

def test_32_keyboard_interrupt_exit_130(broken_project: Path):
    """Прерывание в середине прогона: код 130 и частичный отчёт."""
    script = textwrap.dedent(f"""
        import sys
        from mdlink.checkers.local import LocalChecker

        real = LocalChecker.check
        calls = [0]

        def patched(self, link):
            calls[0] += 1
            if calls[0] > 1:
                raise KeyboardInterrupt
            return real(self, link)

        LocalChecker.check = patched
        sys.argv = ["mdlink", {str(broken_project)!r}, "--no-external"]
        from mdlink.cli import run
        run()
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1"}, timeout=60)
    assert proc.returncode == 130, proc.stderr
    assert "частичный" in proc.stdout or "прервано" in proc.stderr


def test_32b_repo_checkout_cleans_up_on_interrupt(tmp_path: Path):
    from mdlink.repo import RepoCheckout, RepoSpec

    checkout = RepoCheckout(RepoSpec("owner", "name"))

    def boom():
        checkout.tmpdir = tmp_path / "leftover"
        checkout.tmpdir.mkdir()
        raise KeyboardInterrupt

    checkout._fetch = boom
    with pytest.raises(KeyboardInterrupt):
        checkout.__enter__()
    assert not (tmp_path / "leftover").exists()


# --- прочие форматы и флаги ---

def test_markdown_format_is_github_table(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "--format", "markdown")
    assert "| Стр. | Ссылка | Статус | Код | Детали |" in proc.stdout
    assert "\x1b" not in proc.stdout


def test_junit_format_is_valid_xml(broken_project: Path):
    from xml.etree import ElementTree as ET

    proc = run(str(broken_project), "--no-external", "--format", "junit")
    suite = ET.fromstring(proc.stdout)
    assert suite.tag == "testsuite"
    assert int(suite.attrib["failures"]) >= 1
    assert suite.findall(".//failure")


def test_output_file(broken_project: Path, tmp_path: Path):
    out = tmp_path / "report" / "out.json"
    proc = run(str(broken_project), "--no-external", "--format", "json",
               "--output", str(out))
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["broken"] == 1


def test_quiet_prints_only_summary(broken_project: Path):
    proc = run(str(broken_project), "--no-external", "-q")
    assert "BROKEN.md" not in proc.stdout
    assert "Файлов:" in proc.stdout


def test_all_flag_shows_healthy_links(clean_project: Path):
    plain = run(str(clean_project), "--no-external", "--format", "json")
    verbose = run(str(clean_project), "--no-external", "--format", "json", "--all")
    assert json.loads(plain.stdout)["results"] == []
    assert len(json.loads(verbose.stdout)["results"]) == 2


def test_single_file_argument(clean_project: Path):
    proc = run(str(clean_project / "README.md"), "--no-external", "--format", "json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["summary"]["files"] == 1


def test_version_flag():
    proc = run("--version")
    assert proc.returncode == 0 and proc.stdout.startswith("mdlink ")


def test_no_local_skips_local_links(broken_project: Path):
    proc = run(str(broken_project), "--no-local", "--no-external", "--format", "json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["summary"]["broken"] == 0


def test_include_exclude_globs(clean_project: Path):
    proc = run(str(clean_project), "--no-external", "--include", "README.md",
               "--format", "json")
    assert json.loads(proc.stdout)["summary"]["files"] == 1
    proc = run(str(clean_project), "--no-external", "--exclude", "**/docs/**",
               "--format", "json")
    assert json.loads(proc.stdout)["summary"]["files"] == 1


def test_env_var_precedence_below_cli(broken_project: Path):
    proc = run(str(broken_project), "--no-external", env={"MDLINK_FAIL_ON": "never"})
    assert proc.returncode == 0                      # env применился
    proc = run(str(broken_project), "--no-external", "--fail-on", "error",
               env={"MDLINK_FAIL_ON": "never"})
    assert proc.returncode == 1                      # CLI победил env


def test_config_file_is_picked_up(broken_project: Path):
    (broken_project / ".mdlink.toml").write_text(
        'fail-on = "never"\nexternal = false\n', encoding="utf-8")
    proc = run(".", cwd=broken_project)
    assert proc.returncode == 0


def test_cli_beats_config(broken_project: Path):
    (broken_project / ".mdlink.toml").write_text(
        'fail-on = "never"\nexternal = false\n', encoding="utf-8")
    proc = run(".", "--fail-on", "error", cwd=broken_project)
    assert proc.returncode == 1


def test_internal_error_exits_three(clean_project: Path):
    script = textwrap.dedent(f"""
        import sys
        import mdlink.engine as e

        def boom(self):
            raise RuntimeError("boom")

        e.Engine.run = boom
        sys.argv = ["mdlink", {str(clean_project)!r}, "--no-external"]
        from mdlink.cli import run
        run()
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "NO_COLOR": "1"}, timeout=60)
    assert proc.returncode == 3
    assert "внутренняя ошибка" in proc.stderr
