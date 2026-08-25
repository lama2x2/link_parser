"""Режим ``--repo owner/name[@ref]`` (§11)."""

from __future__ import annotations

import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import USER_AGENT

MAX_UNPACKED = 500 * 1024 * 1024  # §11 п.7
_SPEC_RE = re.compile(r"^(?P<owner>[\w.\-]+)/(?P<name>[\w.\-]+)(?:@(?P<ref>[^\s]+))?$")


class RepoError(Exception):
    """Ошибка режима --repo → exit 2."""


@dataclass
class RepoSpec:
    owner: str
    name: str
    ref: str | None = None


def parse_spec(text: str) -> RepoSpec:
    m = _SPEC_RE.match(text.strip())
    if not m:
        raise RepoError(f"ожидается owner/name[@ref], получено {text!r}")
    return RepoSpec(m["owner"], m["name"], m["ref"])


class RepoCheckout:
    """Скачивает tarball во временную директорию и убирает её за собой —
    в том числе при ``Ctrl+C`` (§11 п.6)."""

    def __init__(self, spec: RepoSpec, token: str | None = None, timeout: float = 30.0):
        self.spec = spec
        self.token = token
        self.timeout = timeout
        self.tmpdir: Path | None = None
        self.root: Path | None = None
        self.ref: str | None = spec.ref

    def __enter__(self) -> "RepoCheckout":
        try:
            self._fetch()
        except BaseException:
            self.cleanup()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    @property
    def prefix(self) -> str:
        return f"{self.spec.owner}/{self.spec.name}@{self.ref}:"

    # -- внутреннее --

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _fetch(self) -> None:
        with httpx.Client(timeout=self.timeout, follow_redirects=True,
                          headers=self._headers()) as client:
            if not self.ref:
                self.ref = self._default_branch(client)
            url = (f"https://codeload.github.com/{self.spec.owner}/"
                   f"{self.spec.name}/tar.gz/{self.ref}")
            self.tmpdir = Path(tempfile.mkdtemp(prefix="mdlink-repo-"))
            archive = self.tmpdir / "repo.tar.gz"
            with client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise RepoError(f"репозиторий или ref не найден: {url}")
                if response.status_code >= 400:
                    raise RepoError(f"не удалось скачать архив: HTTP {response.status_code}")
                total = 0
                with open(archive, "wb") as fh:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_UNPACKED:
                            raise RepoError("архив больше 500 МБ — прогон отменён")
                        fh.write(chunk)
        self.root = self._extract(archive)

    def _default_branch(self, client: httpx.Client) -> str:
        api = f"https://api.github.com/repos/{self.spec.owner}/{self.spec.name}"
        response = client.get(api)
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise RepoError(
                "исчерпан лимит GitHub API (60 запросов/час без токена). "
                "Передайте --github-token или переменную GITHUB_TOKEN."
            )
        if response.status_code == 404:
            raise RepoError(f"репозиторий {self.spec.owner}/{self.spec.name} не найден")
        if response.status_code >= 400:
            raise RepoError(f"GitHub API вернул {response.status_code}")
        branch = response.json().get("default_branch")
        if not branch:
            raise RepoError("GitHub API не вернул default_branch")
        return branch

    def _extract(self, archive: Path) -> Path:
        assert self.tmpdir is not None
        dest = self.tmpdir / "src"
        dest.mkdir()
        total = 0
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                _check_member(member, dest)
                total += max(member.size, 0)
                if total > MAX_UNPACKED:
                    raise RepoError("распакованный размер превысил 500 МБ")
                tar.extract(member, dest, filter="data")
        archive.unlink(missing_ok=True)
        entries = [p for p in dest.iterdir() if p.is_dir()]
        return entries[0] if len(entries) == 1 else dest

    def cleanup(self) -> None:
        if self.tmpdir and self.tmpdir.exists():
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.tmpdir = None


def _check_member(member: tarfile.TarInfo, dest: Path) -> None:
    """Защита от zip-slip (§11 п.3): ни ``..``, ни абсолютных путей,
    ни ссылок, уводящих за пределы каталога распаковки."""
    name = member.name.replace("\\", "/")
    if name.startswith("/") or ".." in Path(name).parts:
        raise RepoError(f"подозрительная запись в архиве: {member.name!r}")
    target = (dest / name).resolve()
    if not str(target).startswith(str(dest.resolve())):
        raise RepoError(f"запись выходит за пределы каталога: {member.name!r}")
    if member.issym() or member.islnk():
        link_target = (target.parent / member.linkname).resolve()
        if not str(link_target).startswith(str(dest.resolve())):
            raise RepoError(f"симлинк уводит наружу: {member.name!r}")
