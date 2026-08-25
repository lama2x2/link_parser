"""§13.2 «HTTP», кейсы 16–26. Реальная сеть не используется — только respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from mdlink.checkers.http import (
    HttpChecker, HttpOptions, is_private_host, normalize_url, parse_retry_after,
)
from mdlink.models import Link, LinkKind, Status

# asyncio_mode = "auto" в pyproject.toml — отдельная разметка async-тестов не нужна


def link(url: str, file: str = "a.md", line: int = 1) -> Link:
    return Link(raw=url, kind=LinkKind.HTTP, source_file=Path(f"/proj/{file}"),
                line=line, column=1)


def checker(**kw) -> HttpChecker:
    kw.setdefault("retries", 2)
    kw.setdefault("http2", False)
    return HttpChecker(HttpOptions(**kw))


async def one(url: str, **kw):
    results = await checker(**kw).check_all([link(url)])
    return results[0]


# --- 16–17: базовые статусы ---

@respx.mock
async def test_16_200_is_ok():
    respx.head("https://ok.dev/").mock(return_value=httpx.Response(200))
    res = await one("https://ok.dev/")
    assert res.status is Status.OK and res.code == "ok"
    assert res.http_status == 200
    assert res.elapsed_ms is not None


@respx.mock
async def test_17_404_is_broken():
    respx.head("https://x.dev/gone").mock(return_value=httpx.Response(404))
    res = await one("https://x.dev/gone")
    assert res.status is Status.BROKEN and res.code == "not_found"


# --- 18: ретраи на 5xx ---

@respx.mock
async def test_18_500_retried_exactly_twice(no_backoff):
    route = respx.head("https://x.dev/boom").mock(return_value=httpx.Response(500))
    res = await one("https://x.dev/boom")
    assert res.status is Status.BROKEN and res.code == "server_error"
    assert route.call_count == 3          # 1 попытка + 2 ретрая


@respx.mock
async def test_18b_501_is_not_retried_but_falls_back_to_get(no_backoff):
    head = respx.head("https://x.dev/ni").mock(return_value=httpx.Response(501))
    get = respx.get("https://x.dev/ni").mock(return_value=httpx.Response(200))
    res = await one("https://x.dev/ni")
    assert res.status is Status.OK
    assert head.call_count == 1 and get.call_count == 1


@respx.mock
async def test_4xx_not_retried(no_backoff):
    route = respx.head("https://x.dev/gone").mock(return_value=httpx.Response(404))
    await one("https://x.dev/gone")
    assert route.call_count == 1


# --- 19: HEAD → GET fallback ---

@respx.mock
async def test_19_403_head_falls_back_to_get():
    head = respx.head("https://cf.dev/").mock(return_value=httpx.Response(403))
    get = respx.get("https://cf.dev/").mock(return_value=httpx.Response(200))
    res = await one("https://cf.dev/")
    assert res.status is Status.OK
    assert head.call_count == 1 and get.call_count == 1
    assert get.calls[0].request.headers["Range"] == "bytes=0-0"


@respx.mock
async def test_19b_403_on_both_is_auth_required():
    respx.head("https://cf.dev/").mock(return_value=httpx.Response(403))
    respx.get("https://cf.dev/").mock(return_value=httpx.Response(403))
    res = await one("https://cf.dev/")
    assert res.status is Status.WARNING and res.code == "auth_required"


# --- 20: таймаут ---

@respx.mock
async def test_20_timeout_within_total_budget(no_backoff):
    respx.head("https://slow.dev/").mock(side_effect=httpx.ReadTimeout("too slow"))
    opts = HttpOptions(timeout=0.2, retries=1, http2=False)
    res = (await HttpChecker(opts).check_all([link("https://slow.dev/")]))[0]
    assert res.status is Status.BROKEN and res.code == "timeout"
    assert res.elapsed_ms <= opts.total_budget * 1000


# --- 21–22: Retry-After ---

@respx.mock
async def test_21_retry_after_seconds_is_honoured(sleeps):
    route = respx.head("https://rl.dev/").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200),
    ])
    res = await one("https://rl.dev/")
    assert res.status is Status.OK
    assert route.call_count == 2
    assert 2.0 in sleeps


@respx.mock
async def test_22_huge_retry_after_returns_rate_limited(sleeps):
    respx.head("https://rl.dev/").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"}))
    res = await one("https://rl.dev/")
    assert res.status is Status.WARNING and res.code == "rate_limited"
    assert sleeps == []                    # ждать час никто не будет


@respx.mock
async def test_429_without_header_is_retried_then_warned(no_backoff):
    route = respx.head("https://rl.dev/").mock(return_value=httpx.Response(429))
    res = await one("https://rl.dev/")
    assert res.code == "rate_limited" and res.status is Status.WARNING
    assert route.call_count == 3


# --- 23: редиректы ---

@respx.mock
async def test_23_redirect_chain_over_limit():
    for i in range(6):
        respx.head(f"https://r.dev/{i}").mock(
            return_value=httpx.Response(302, headers={"Location": f"/{i + 1}"}))
    respx.head("https://r.dev/6").mock(return_value=httpx.Response(200))
    res = await one("https://r.dev/0", max_redirects=5)
    assert res.status is Status.BROKEN and res.code == "too_many_redirects"


@respx.mock
async def test_redirect_within_limit_records_final_url():
    respx.head("https://r.dev/a").mock(
        return_value=httpx.Response(301, headers={"Location": "https://r.dev/b"}))
    respx.head("https://r.dev/b").mock(return_value=httpx.Response(200))
    res = await one("https://r.dev/a")
    assert res.status is Status.OK
    assert res.final_url == "https://r.dev/b"


@respx.mock
async def test_https_to_http_downgrade_warns():
    respx.head("https://d.dev/").mock(
        return_value=httpx.Response(302, headers={"Location": "http://d.dev/"}))
    respx.head("http://d.dev/").mock(return_value=httpx.Response(200))
    res = await one("https://d.dev/")
    assert res.status is Status.WARNING and res.code == "insecure_redirect"


# --- 24: дедупликация ---

@respx.mock
async def test_24_same_url_in_three_files_is_one_request():
    route = respx.head("https://dup.dev/").mock(return_value=httpx.Response(200))
    links = [link("https://dup.dev/", f"f{i}.md", i) for i in range(3)]
    results = await checker().check_all(links)
    assert route.call_count == 1
    assert len(results) == 3
    assert {r.status for r in results} == {Status.OK}


@respx.mock
async def test_dedup_key_ignores_fragment_and_default_port():
    route = respx.head("https://dup.dev/a").mock(return_value=httpx.Response(200))
    links = [link("https://dup.dev/a#one"), link("https://DUP.dev:443/a#two")]
    results = await checker().check_all(links)
    assert route.call_count == 1
    assert len(results) == 2


# --- 25: приватные хосты ---

@respx.mock
async def test_25_localhost_is_skipped_without_request():
    route = respx.head("http://localhost:3000/").mock(return_value=httpx.Response(200))
    res = await one("http://localhost:3000/")
    assert res.status is Status.SKIPPED and res.code == "private_host"
    assert route.call_count == 0


@respx.mock
async def test_25b_allow_private_hosts_performs_request():
    route = respx.head("http://localhost:3000/").mock(return_value=httpx.Response(200))
    res = await one("http://localhost:3000/", allow_private_hosts=True)
    assert res.status is Status.OK
    assert route.call_count == 1


# --- 26: circuit breaker ---

@respx.mock
async def test_26_circuit_breaker_skips_remaining_urls(no_backoff):
    respx.head(url__regex=r"https://dead\.dev/.*").mock(
        side_effect=httpx.ConnectTimeout("no route"))
    links = [link(f"https://dead.dev/{i}") for i in range(8)]
    chk = checker(retries=0, concurrency=1, per_host=1)
    results = await chk.check_all(links)
    skipped = [r for r in results if r.code == "host_unreachable"]
    assert len(skipped) >= 1
    assert all(r.status is Status.SKIPPED for r in skipped)


# --- ignore-url и кэш ---

@respx.mock
async def test_ignore_url_pattern():
    route = respx.head("https://twitter.com/x").mock(return_value=httpx.Response(200))
    res = await one("https://twitter.com/x", ignore_url=[r"^https://twitter\.com/"])
    assert res.status is Status.SKIPPED and res.code == "ignored_by_pattern"
    assert route.call_count == 0


@respx.mock
async def test_persistent_cache_avoids_second_request(tmp_path: Path):
    cache = tmp_path / "cache.json"
    route = respx.head("https://c.dev/").mock(return_value=httpx.Response(200))

    first = await one("https://c.dev/", cache_path=cache)
    assert first.status is Status.OK
    assert cache.is_file()

    second = await one("https://c.dev/", cache_path=cache)
    assert second.status is Status.OK
    assert "cached" in second.notes
    assert route.call_count == 1


@respx.mock
async def test_github_token_header_only_for_github():
    gh = respx.head("https://github.com/a").mock(return_value=httpx.Response(200))
    other = respx.head("https://elsewhere.dev/a").mock(return_value=httpx.Response(200))
    await checker(github_token="secret").check_all(
        [link("https://github.com/a"), link("https://elsewhere.dev/a")])
    assert gh.calls[0].request.headers["Authorization"] == "Bearer secret"
    assert "authorization" not in other.calls[0].request.headers


@respx.mock
async def test_user_agent_is_sent():
    route = respx.head("https://ua.dev/").mock(return_value=httpx.Response(200))
    await one("https://ua.dev/")
    assert route.calls[0].request.headers["User-Agent"].startswith("mdlink/")


@respx.mock
async def test_per_host_limit_is_respected():
    import asyncio

    active, peak = 0, 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200)

    respx.head(url__regex=r"https://busy\.dev/.*").mock(side_effect=handler)
    links = [link(f"https://busy.dev/{i}") for i in range(12)]
    await checker(concurrency=16, per_host=3).check_all(links)
    assert peak <= 3


@respx.mock
async def test_dns_error_classified():
    respx.head("https://nx.dev/").mock(
        side_effect=httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    res = await one("https://nx.dev/", retries=0)
    assert res.code == "dns_error"


@respx.mock
async def test_ssl_error_broken_by_default_warning_with_insecure():
    import ssl

    respx.head("https://bad-tls.dev/").mock(
        side_effect=httpx.ConnectError("certificate verify failed"))
    assert (await one("https://bad-tls.dev/", retries=0)).code == "ssl_error"
    res = await one("https://bad-tls.dev/", retries=0, insecure=True)
    assert res.status is Status.WARNING and res.code == "ssl_error"


# --- юнит-тесты помощников (§7.2, §7.6, §7.9) ---

def test_normalize_url_rules():
    assert normalize_url("HTTPS://Example.COM:443/a?b=1#f") == "https://example.com/a?b=1"
    assert normalize_url("http://example.com") == "http://example.com/"
    assert normalize_url("http://пример.рф/") == "http://xn--e1afmkfd.xn--p1ai/"
    assert normalize_url("https://x.dev/?b=2&a=1") == "https://x.dev/?b=2&a=1"


@pytest.mark.parametrize("url,private", [
    ("http://localhost:3000", True),
    ("http://127.0.0.1/", True),
    ("http://[::1]/", True),
    ("http://10.0.0.1/", True),
    ("http://172.16.5.4/", True),
    ("http://192.168.1.1/", True),
    ("http://169.254.1.1/", True),
    ("http://printer.local/", True),
    ("https://api.internal/", True),
    ("https://example.com/", False),
    ("https://8.8.8.8/", False),
])
def test_private_host_matrix(url, private):
    assert is_private_host(url) is private


def test_parse_retry_after_formats():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0
    assert parse_retry_after(None) is None
    assert parse_retry_after("вчера") is None
