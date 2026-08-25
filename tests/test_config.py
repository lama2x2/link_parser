"""§10: конфиг, переменные окружения, приоритеты."""

from __future__ import annotations

from pathlib import Path

import pytest

from mdlink.config import ConfigError, find_config, from_env, load


def write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_mdlink_toml_is_loaded(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", 'timeout = 15.0\nfail-on = "warning"\n')
    cfg = load(None, tmp_path)
    assert cfg.values == {"timeout": 15.0, "fail_on": "warning"}


def test_pyproject_tool_section(tmp_path: Path):
    write(tmp_path / "pyproject.toml",
          '[tool.mdlink]\nconcurrency = 8\ninclude = ["docs/**/*.md"]\n')
    cfg = load(None, tmp_path)
    assert cfg.values == {"concurrency": 8, "include": ["docs/**/*.md"]}


def test_mdlink_toml_wins_over_pyproject(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", "retries = 1\n")
    write(tmp_path / "pyproject.toml", "[tool.mdlink]\nretries = 9\n")
    assert load(None, tmp_path).values == {"retries": 1}


def test_pyproject_without_section_is_skipped(tmp_path: Path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "x"\n')
    assert find_config(None, tmp_path) is None or \
        find_config(None, tmp_path).name != "pyproject.toml"


def test_explicit_config_must_exist(tmp_path: Path):
    with pytest.raises(ConfigError, match="не найден"):
        load(tmp_path / "nope.toml", tmp_path)


def test_unknown_key_reports_key_and_line(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", "timeout = 5.0\nbogus = 1\n")
    with pytest.raises(ConfigError) as exc:
        load(None, tmp_path)
    assert "bogus" in str(exc.value) and ":2" in str(exc.value)


def test_wrong_type_is_rejected(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", 'retries = "три"\n')
    with pytest.raises(ConfigError, match="retries"):
        load(None, tmp_path)


def test_bool_is_not_accepted_as_int(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", "retries = true\n")
    with pytest.raises(ConfigError, match="retries"):
        load(None, tmp_path)


def test_enum_choice_is_validated(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", 'fail-on = "sometimes"\n')
    with pytest.raises(ConfigError, match="fail-on"):
        load(None, tmp_path)


def test_broken_toml(tmp_path: Path):
    write(tmp_path / ".mdlink.toml", "timeout = = 5\n")
    with pytest.raises(ConfigError, match="битый TOML"):
        load(None, tmp_path)


def test_ignore_url_list(tmp_path: Path):
    write(tmp_path / ".mdlink.toml",
          'ignore-url = ["^https://twitter\\\\.com/", "^https://.*\\\\.internal\\\\.corp/"]\n')
    assert len(load(None, tmp_path).values["ignore_url"]) == 2


def test_env_parsing():
    env = {"MDLINK_TIMEOUT": "3.5", "MDLINK_RETRIES": "7", "MDLINK_ALL": "yes",
           "MDLINK_FORMAT": "json"}
    assert from_env(env) == {"timeout": 3.5, "retries": 7, "all": True, "format": "json"}


def test_env_invalid_value():
    with pytest.raises(ConfigError, match="MDLINK_RETRIES"):
        from_env({"MDLINK_RETRIES": "много"})


def test_env_invalid_choice():
    with pytest.raises(ConfigError, match="MDLINK_FORMAT"):
        from_env({"MDLINK_FORMAT": "xml"})
