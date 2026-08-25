"""Загрузка и валидация конфига (§10).

Приоритет: флаги CLI > переменные окружения ``MDLINK_*`` > конфиг > дефолты.
Слияния между несколькими файлами нет — побеждает первый найденный.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

BOOL = "bool"
INT = "int"
FLOAT = "float"
STR = "str"
LIST = "list[str]"

# key (в TOML — kebab-case) → (тип, допустимые значения)
SCHEMA: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "include": (LIST, None),
    "exclude": (LIST, None),
    "root": (STR, None),
    "gitignore": (BOOL, None),
    "follow-symlinks": (BOOL, None),
    "external": (BOOL, None),
    "local": (BOOL, None),
    "check-anchors": (BOOL, None),
    "check-images": (BOOL, None),
    "dir-index": (BOOL, None),
    "wikilinks": (BOOL, None),
    "bare-urls": (BOOL, None),
    "ignore-url": (LIST, None),
    "allow-private-hosts": (BOOL, None),
    "timeout": (FLOAT, None),
    "connect-timeout": (FLOAT, None),
    "retries": (INT, None),
    "concurrency": (INT, None),
    "per-host": (INT, None),
    "max-redirects": (INT, None),
    "user-agent": (STR, None),
    "insecure": (BOOL, None),
    "github-token": (STR, None),
    "format": (STR, ("pretty", "json", "markdown", "junit")),
    "output": (STR, None),
    "all": (BOOL, None),
    "quiet": (BOOL, None),
    "verbose": (INT, None),
    "color": (BOOL, None),
    "fail-on": (STR, ("error", "warning", "never")),
    "cache": (STR, None),
}

_PY_TYPES = {BOOL: bool, INT: int, FLOAT: (int, float), STR: str}


class ConfigError(Exception):
    """Невалидный конфиг → exit 2 (§2.3)."""


@dataclass
class LoadedConfig:
    values: dict[str, object]
    source: Path | None


def find_config(explicit: Path | None, cwd: Path) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"конфиг не найден: {explicit}")
        return explicit
    candidates = [
        cwd / ".mdlink.toml",
        cwd / "pyproject.toml",
        Path.home() / ".config" / "mdlink" / "config.toml",
    ]
    for cand in candidates:
        if not cand.is_file():
            continue
        if cand.name == "pyproject.toml" and not _has_tool_section(cand):
            continue
        return cand
    return None


def _has_tool_section(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "mdlink" in data.get("tool", {})


def load(explicit: Path | None, cwd: Path | None = None) -> LoadedConfig:
    cwd = cwd or Path.cwd()
    path = find_config(explicit, cwd)
    if path is None:
        return LoadedConfig({}, None)
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except OSError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: битый TOML — {exc}") from exc

    section = data.get("tool", {}).get("mdlink") if path.name == "pyproject.toml" else data
    if section is None:
        section = data.get("tool", {}).get("mdlink", {})
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: секция [tool.mdlink] должна быть таблицей")

    validated = _validate(section, path, text)
    return LoadedConfig(validated, path)


def _validate(section: dict, path: Path, text: str) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in section.items():
        if key not in SCHEMA:
            raise ConfigError(
                f"{path}:{_line_of(text, key)}: неизвестный ключ «{key}». "
                f"Допустимые: {', '.join(sorted(SCHEMA))}"
            )
        kind, choices = SCHEMA[key]
        if kind == LIST:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(
                    f"{path}:{_line_of(text, key)}: ключ «{key}» ожидает список строк")
        else:
            expected = _PY_TYPES[kind]
            if kind != BOOL and isinstance(value, bool):
                raise ConfigError(
                    f"{path}:{_line_of(text, key)}: ключ «{key}» ожидает {kind}")
            if not isinstance(value, expected):
                raise ConfigError(
                    f"{path}:{_line_of(text, key)}: ключ «{key}» ожидает {kind}, "
                    f"получено {type(value).__name__}")
            if choices and value not in choices:
                raise ConfigError(
                    f"{path}:{_line_of(text, key)}: ключ «{key}» — одно из "
                    f"{', '.join(choices)}")
        out[key.replace("-", "_")] = value
    return out


def _line_of(text: str, key: str) -> int:
    pattern = re.compile(rf"^\s*(?:\"{re.escape(key)}\"|{re.escape(key)})\s*=", re.M)
    m = pattern.search(text)
    return text[: m.start()].count("\n") + 1 if m else 0


# --- переменные окружения -----------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def from_env(environ: dict | None = None) -> dict[str, object]:
    environ = environ if environ is not None else os.environ
    out: dict[str, object] = {}
    for key, (kind, choices) in SCHEMA.items():
        name = "MDLINK_" + key.replace("-", "_").upper()
        if name not in environ:
            continue
        raw = environ[name]
        try:
            if kind == BOOL:
                low = raw.strip().lower()
                if low not in _TRUE | _FALSE:
                    raise ValueError(raw)
                out[key.replace("-", "_")] = low in _TRUE
            elif kind == INT:
                out[key.replace("-", "_")] = int(raw)
            elif kind == FLOAT:
                out[key.replace("-", "_")] = float(raw)
            elif kind == LIST:
                out[key.replace("-", "_")] = [p for p in raw.split(os.pathsep) if p]
            else:
                if choices and raw not in choices:
                    raise ValueError(raw)
                out[key.replace("-", "_")] = raw
        except ValueError as exc:
            raise ConfigError(f"переменная {name}: невалидное значение {raw!r}") from exc
    return out
