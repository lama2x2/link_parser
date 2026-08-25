"""CLI-интерфейс (§2): парсинг флагов, слияние с конфигом, коды выхода.

Отчёт пишется в stdout, диагностика и прогресс-бар — в stderr (§9.1).
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from . import USER_AGENT, __version__
from .checkers.http import HttpOptions
from .config import ConfigError, from_env, load as load_config
from .engine import Engine, Report, ScanOptions
from .models import Status
from .report import render

EXIT_OK, EXIT_BROKEN, EXIT_USAGE, EXIT_INTERNAL, EXIT_INTERRUPT = 0, 1, 2, 3, 130


class Format(str, Enum):
    pretty = "pretty"
    json = "json"
    markdown = "markdown"
    junit = "junit"


class FailOn(str, Enum):
    error = "error"
    warning = "warning"
    never = "never"


app = typer.Typer(add_completion=False, rich_markup_mode=None,
                  context_settings={"help_option_names": ["-h", "--help"]})

err = Console(stderr=True)


def _from_cli(ctx: typer.Context, name: str) -> bool:
    """Значение параметра пришло из командной строки, а не из дефолта.

    Сравнение по имени, а не импортом ``click.core.ParameterSource``: в свежих
    typer click вендорится и внешнего пакета может не быть.
    """
    source = ctx.get_parameter_source(name)
    return getattr(source, "name", "") == "COMMANDLINE"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mdlink {__version__}")
        raise typer.Exit(EXIT_OK)


@app.command(help="Проверка ссылок в Markdown-файлах проекта или GitHub-репозитория.")
def main(  # noqa: PLR0913 — CLI по определению широкий
    ctx: typer.Context,
    path: Path = typer.Argument(Path("."), help="Папка или .md-файл"),
    # --- источник ---
    repo: str | None = typer.Option(None, "--repo", metavar="owner/name[@ref]",
                                    help="Скачать и проверить GitHub-репозиторий"),
    root: Path | None = typer.Option(None, "--root", help="Корень проекта для /абсолютных ссылок"),
    include: list[str] = typer.Option([], "--include", help="Глоб сканируемых файлов"),
    exclude: list[str] = typer.Option([], "--exclude", help="Глоб исключений"),
    no_gitignore: bool = typer.Option(False, "--no-gitignore", help="Не учитывать .gitignore"),
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks",
                                         help="Ходить по симлинкам директорий"),
    # --- что проверять ---
    no_external: bool = typer.Option(False, "--no-external", help="Пропустить HTTP(S)-ссылки"),
    no_local: bool = typer.Option(False, "--no-local", help="Пропустить локальные ссылки"),
    check_anchors: bool = typer.Option(True, "--check-anchors/--no-check-anchors",
                                       help="Проверять #якоря"),
    check_images: bool = typer.Option(True, "--check-images/--no-check-images",
                                      help="Проверять ![alt](src)"),
    no_dir_index: bool = typer.Option(False, "--no-dir-index",
                                      help="Не считать README/index в директории валидной целью"),
    wikilinks: bool = typer.Option(False, "--wikilinks/--no-wikilinks",
                                   help="Разбирать [[wiki-ссылки]]"),
    bare_urls: bool = typer.Option(False, "--bare-urls", help="Проверять голые URL в тексте"),
    ignore_url: list[str] = typer.Option([], "--ignore-url",
                                         help="Regex; совпавшие URL → SKIPPED"),
    allow_private_hosts: bool = typer.Option(False, "--allow-private-hosts",
                                             help="Разрешить localhost/приватные IP"),
    # --- сеть ---
    timeout: float = typer.Option(10.0, "--timeout", help="Дедлайн одного запроса, с"),
    connect_timeout: float = typer.Option(5.0, "--connect-timeout", help="Таймаут соединения, с"),
    retries: int = typer.Option(2, "--retries", help="Повторы для retryable-ошибок"),
    concurrency: int = typer.Option(16, "--concurrency", help="Глобальный лимит запросов"),
    per_host: int = typer.Option(4, "--per-host", help="Лимит запросов на хост"),
    max_redirects: int = typer.Option(5, "--max-redirects", help="Лимит редиректов"),
    user_agent: str = typer.Option(USER_AGENT, "--user-agent", help="Заголовок User-Agent"),
    insecure: bool = typer.Option(False, "--insecure", help="Не проверять TLS-сертификаты"),
    github_token: str | None = typer.Option(None, "--github-token", envvar="GITHUB_TOKEN",
                                            help="Токен для --repo и ссылок на github.com"),
    # --- вывод ---
    fmt: Format = typer.Option(Format.pretty, "--format", help="Формат отчёта"),
    output: Path | None = typer.Option(None, "--output", help="Куда писать отчёт"),
    show_all: bool = typer.Option(False, "--all", help="Показывать и живые ссылки"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Только итоговая строка"),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="-v, -vv"),
    no_color: bool = typer.Option(False, "--no-color", help="Без ANSI-цветов"),
    fail_on: FailOn = typer.Option(FailOn.error, "--fail-on", help="Уровень для ненулевого кода"),
    # --- прочее ---
    config: Path | None = typer.Option(None, "--config", help="Путь к конфигу"),
    cache: Path | None = typer.Option(None, "--cache", help="Файл персистентного кэша"),
    _version: bool = typer.Option(False, "--version", callback=_version_callback,
                                  is_eager=True, help="Показать версию"),
) -> None:
    try:
        code = _run(locals())
    except ConfigError as exc:
        err.print(f"[red]ошибка конфигурации:[/red] {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    except KeyboardInterrupt:
        err.print("\n[yellow]прервано пользователем[/yellow]")
        raise typer.Exit(EXIT_INTERRUPT) from None
    raise typer.Exit(code)


def _run(ns: dict) -> int:  # noqa: C901 — линейный сценарий, дробить незачем
    ctx: typer.Context = ns["ctx"]
    verbose: int = ns["verbose"]

    cfg = load_config(ns["config"]).values
    env = from_env()

    def pick(name: str, cli_value, config_key: str | None = None):
        """CLI (если задан явно) > env > конфиг > дефолт."""
        key = config_key or name
        if _from_cli(ctx, name):
            return cli_value
        if key in env:
            return env[key]
        if key in cfg:
            return cfg[key]
        return cli_value

    def pick_negated(flag_name: str, config_key: str) -> bool:
        """``--no-external`` и подобные: явный флаг → False, иначе env/конфиг."""
        if _from_cli(ctx, flag_name):
            return not ns[flag_name]
        if config_key in env:
            return bool(env[config_key])
        if config_key in cfg:
            return bool(cfg[config_key])
        return True

    # --- источник ---
    repo_spec = pick("repo", ns["repo"])
    path: Path = ns["path"]
    if repo_spec and _from_cli(ctx, "path"):
        err.print("[red]ошибка:[/red] PATH и --repo взаимоисключимы")
        return EXIT_USAGE

    checkout = None
    prefix = ""
    try:
        if repo_spec:
            from .repo import RepoCheckout, RepoError, parse_spec
            try:
                checkout = RepoCheckout(parse_spec(repo_spec),
                                        token=pick("github_token", ns["github_token"]))
                checkout.__enter__()
            except RepoError as exc:
                err.print(f"[red]ошибка --repo:[/red] {exc}")
                return EXIT_USAGE
            scan_path = checkout.root
            root = checkout.root
            prefix = checkout.prefix
        else:
            scan_path = path
            if not scan_path.exists():
                err.print(f"[red]ошибка:[/red] путь не существует: {scan_path}")
                return EXIT_USAGE
            cfg_root = pick("root", ns["root"])
            if cfg_root is not None:
                root = Path(cfg_root)
                if not root.exists():
                    err.print(f"[red]ошибка:[/red] --root не существует: {root}")
                    return EXIT_USAGE
            else:
                root = scan_path if scan_path.is_dir() else scan_path.parent

        include = list(pick("include", ns["include"])) or None
        exclude = list(pick("exclude", ns["exclude"])) or None

        http_opts = HttpOptions(
            timeout=float(pick("timeout", ns["timeout"])),
            connect_timeout=float(pick("connect_timeout", ns["connect_timeout"])),
            retries=int(pick("retries", ns["retries"])),
            concurrency=int(pick("concurrency", ns["concurrency"])),
            per_host=int(pick("per_host", ns["per_host"])),
            max_redirects=int(pick("max_redirects", ns["max_redirects"])),
            user_agent=str(pick("user_agent", ns["user_agent"])),
            insecure=bool(pick("insecure", ns["insecure"])),
            github_token=pick("github_token", ns["github_token"]),
            allow_private_hosts=bool(pick("allow_private_hosts", ns["allow_private_hosts"])),
            ignore_url=list(pick("ignore_url", ns["ignore_url"])),
            cache_path=Path(pick("cache", ns["cache"])) if pick("cache", ns["cache"]) else None,
        )

        wikilinks = bool(pick("wikilinks", ns["wikilinks"]))
        if (Path(root) / ".obsidian").is_dir():
            wikilinks = True  # §5.5: автовключение

        options = ScanOptions(
            path=Path(scan_path), root=Path(root),
            include=include, exclude=exclude,
            use_gitignore=pick_negated("no_gitignore", "gitignore"),
            follow_symlinks=bool(pick("follow_symlinks", ns["follow_symlinks"])),
            check_external=pick_negated("no_external", "external"),
            check_local=pick_negated("no_local", "local"),
            check_anchors=bool(pick("check_anchors", ns["check_anchors"])),
            check_images=bool(pick("check_images", ns["check_images"])),
            dir_index=pick_negated("no_dir_index", "dir-index".replace("-", "_")),
            wikilinks=wikilinks,
            bare_urls=bool(pick("bare_urls", ns["bare_urls"])),
            http=http_opts,
            path_prefix=prefix,
        )

        fmt = str(pick("fmt", ns["fmt"].value, config_key="format"))
        output = pick("output", ns["output"])
        output_path = Path(output) if output else None
        quiet = bool(pick("quiet", ns["quiet"]))
        show_all = bool(pick("all", ns["show_all"], config_key="all"))
        fail_on = str(pick("fail_on", ns["fail_on"].value))
        color = _use_color(ctx, ns, cfg, env, output_path, fmt)

        report = _scan(options, quiet=quiet, verbose=verbose)

        text = render(
            fmt, report, show_all=show_all, color=color, verbose=verbose,
            quiet=quiet, width=_width(), scanned=prefix or str(scan_path),
        )
        _write(text, output_path)

        if report.interrupted:
            return EXIT_INTERRUPT
        return _exit_code(report, fail_on)
    finally:
        if checkout is not None:
            checkout.cleanup()


def _scan(options: ScanOptions, *, quiet: bool, verbose: int) -> Report:
    """Прогресс-бар — только при TTY и без --quiet, всегда в stderr (§9.1)."""
    show_progress = sys.stderr.isatty() and not quiet

    if not show_progress:
        def on_event(kind: str, n: int = 1) -> None:
            if verbose >= 2:
                err.print(f"[dim]{kind}: {n}[/dim]")

        engine = Engine(options, on_event=on_event)
        _warn_about_cache(engine, verbose)
        return engine.run()

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}"), TimeElapsedColumn(),
        console=err, transient=True,
    ) as progress:
        task = progress.add_task("проверка ссылок", total=None)

        def on_event(kind: str, n: int = 1) -> None:
            if kind == "checked":
                progress.advance(task, n)
            elif kind == "links":
                progress.update(task, total=n)

        engine = Engine(options, on_event=on_event)
        _warn_about_cache(engine, verbose)
        return engine.run()


def _warn_about_cache(engine: Engine, verbose: int) -> None:
    if verbose >= 1 and engine.http.cache.load_error:
        err.print(f"[yellow]предупреждение:[/yellow] {engine.http.cache.load_error}")


def _exit_code(report: Report, fail_on: str) -> int:
    if fail_on == "never":
        return EXIT_OK
    counts = report.counts
    if counts["broken"]:
        return EXIT_BROKEN
    if fail_on == "warning" and counts["warning"]:
        return EXIT_BROKEN
    return EXIT_OK


def _use_color(ctx, ns, cfg, env, output_path, fmt: str) -> bool:
    if fmt != "pretty":
        return False
    if ns["no_color"] or os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if env.get("color") is False or (cfg.get("color") is False and "color" not in env):
        return False
    if output_path is not None:
        return False
    return sys.stdout.isatty()


def _width() -> int:
    try:
        return max(60, min(shutil_terminal_width(), 160))
    except (OSError, ValueError):  # ширина терминала недоступна — берём дефолт
        return 100


def shutil_terminal_width() -> int:
    import shutil

    return shutil.get_terminal_size((100, 24)).columns


def _write(text: str, output: Path | None) -> None:
    if output is None:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def run() -> None:
    """Обёртка для ``python -m mdlink`` и консольного скрипта."""
    try:
        app()
    except KeyboardInterrupt:
        err.print("\n[yellow]прервано пользователем[/yellow]")
        sys.exit(EXIT_INTERRUPT)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — §2.3: внутренняя ошибка → exit 3
        if "-vv" in sys.argv or os.environ.get("MDLINK_TRACEBACK"):
            raise
        err.print(f"[red]внутренняя ошибка:[/red] {type(exc).__name__}: {exc}")
        err.print("[dim]повторите с -vv, чтобы увидеть traceback[/dim]")
        sys.exit(EXIT_INTERNAL)


if __name__ == "__main__":  # pragma: no cover
    run()
