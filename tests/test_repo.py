"""§11: режим --repo. Сеть не используется — архив собирается локально."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from mdlink.repo import MAX_UNPACKED, RepoCheckout, RepoError, RepoSpec, parse_spec


def test_parse_spec():
    assert parse_spec("owner/name") == RepoSpec("owner", "name", None)
    assert parse_spec("owner/name@v1.2.3") == RepoSpec("owner", "name", "v1.2.3")
    assert parse_spec("owner/name@feature/x") == RepoSpec("owner", "name", "feature/x")


@pytest.mark.parametrize("bad", ["", "owner", "/name", "owner/name/extra", "owner name"])
def test_parse_spec_rejects_garbage(bad):
    with pytest.raises(RepoError):
        parse_spec(bad)


def _tarball(path: Path, entries: dict[str, str]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in entries.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _checkout(tmp_path: Path) -> RepoCheckout:
    checkout = RepoCheckout(RepoSpec("owner", "name", "main"))
    checkout.tmpdir = tmp_path / "work"
    checkout.tmpdir.mkdir()
    return checkout


def test_extract_normal_archive(tmp_path: Path):
    archive = _tarball(tmp_path / "a.tar.gz",
                       {"name-main/README.md": "# hi\n", "name-main/docs/a.md": "# a\n"})
    checkout = _checkout(tmp_path)
    root = checkout._extract(archive)
    assert (root / "README.md").is_file()
    assert root.name == "name-main"


def test_zip_slip_relative_is_rejected(tmp_path: Path):
    archive = _tarball(tmp_path / "a.tar.gz", {"../evil.md": "boom\n"})
    with pytest.raises(RepoError, match="подозрительная запись"):
        _checkout(tmp_path)._extract(archive)


def test_zip_slip_absolute_is_rejected(tmp_path: Path):
    archive = _tarball(tmp_path / "a.tar.gz", {"/etc/evil.md": "boom\n"})
    with pytest.raises(RepoError, match="подозрительная запись"):
        _checkout(tmp_path)._extract(archive)


def test_symlink_escaping_dest_is_rejected(tmp_path: Path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("name-main/evil")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../../etc/passwd"
        tar.addfile(info)
    with pytest.raises(RepoError, match="уводит наружу"):
        _checkout(tmp_path)._extract(archive)


def test_size_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("mdlink.repo.MAX_UNPACKED", 10)
    archive = _tarball(tmp_path / "a.tar.gz", {"name-main/big.md": "x" * 100})
    with pytest.raises(RepoError, match="500 МБ|превысил"):
        _checkout(tmp_path)._extract(archive)


def test_cleanup_removes_tmpdir(tmp_path: Path):
    checkout = _checkout(tmp_path)
    assert checkout.tmpdir.exists()
    checkout.cleanup()
    assert not (tmp_path / "work").exists()


def test_cleanup_is_idempotent(tmp_path: Path):
    checkout = _checkout(tmp_path)
    checkout.cleanup()
    checkout.cleanup()


def test_prefix_format(tmp_path: Path):
    checkout = _checkout(tmp_path)
    assert checkout.prefix == "owner/name@main:"


def test_max_unpacked_is_500mb():
    assert MAX_UNPACKED == 500 * 1024 * 1024


# --- полный цикл --repo без реальной сети ---

def test_fetch_flow_with_mocked_github(tmp_path: Path, monkeypatch):
    import httpx
    import respx

    archive = _tarball(tmp_path / "src.tar.gz",
                       {"name-main/README.md": "[a](./docs/a.md)\n",
                        "name-main/docs/a.md": "# a\n"})
    payload = archive.read_bytes()

    with respx.mock:
        respx.get("https://api.github.com/repos/owner/name").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"}))
        respx.get("https://codeload.github.com/owner/name/tar.gz/main").mock(
            return_value=httpx.Response(200, content=payload))

        with RepoCheckout(RepoSpec("owner", "name")) as checkout:
            assert checkout.ref == "main"
            assert (checkout.root / "README.md").is_file()
            assert checkout.prefix == "owner/name@main:"
            tmpdir = checkout.tmpdir
    assert not tmpdir.exists()          # §11 п.6: убрано в finally


def test_rate_limit_hint(tmp_path: Path):
    import httpx
    import respx

    with respx.mock:
        respx.get("https://api.github.com/repos/owner/name").mock(
            return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
        with pytest.raises(RepoError, match="github-token"):
            RepoCheckout(RepoSpec("owner", "name")).__enter__()


def test_missing_repo_is_reported(tmp_path: Path):
    import httpx
    import respx

    with respx.mock:
        respx.get("https://api.github.com/repos/owner/name").mock(
            return_value=httpx.Response(404))
        with pytest.raises(RepoError, match="не найден"):
            RepoCheckout(RepoSpec("owner", "name")).__enter__()
