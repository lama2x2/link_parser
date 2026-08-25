"""Проверка внешних ссылок (§7).

Асинхронный клиент с глобальным и по-хостовым лимитом конкурентности,
ретраями с backoff, circuit breaker'ом, дедупликацией и кэшем.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import re
import ssl
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .. import USER_AGENT
from ..models import Link, Result, Status

# --- нормализация URL (§7.2) --------------------------------------------------

DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Ключ дедупликации и кэша. В отчёте всегда показывается исходный raw."""
    if url.startswith("//"):
        url = "https:" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        host = host.encode("idna").decode("ascii")  # IDN → punycode
    except (UnicodeError, AttributeError):
        pass
    netloc = host
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{auth}@{netloc}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))  # fragment отбрасываем


# --- приватные хосты (§7.9) ---------------------------------------------------

_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def is_private_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    if host.endswith(_PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified
    )


# --- Retry-After (§7.6) -------------------------------------------------------


def parse_retry_after(value: str | None) -> float | None:
    """Поддерживаются оба формата: секунды и HTTP-date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return max((dt - now).total_seconds(), 0.0)


# --- кэш (§7.10) --------------------------------------------------------------

TTL_OK = 24 * 3600
TTL_BROKEN = 3600


class PersistentCache:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self.load_error: str | None = None
        if path and path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Битый кэш — не повод падать, но и не повод молчать:
                # ошибка доступна вызывающему коду и печатается при -v.
                self.load_error = f"кэш {path} нечитаем ({exc}), начинаем с нуля"
                self.data = {}

    def get(self, url: str) -> dict | None:
        entry = self.data.get(url)
        if not entry:
            return None
        checked = entry.get("checked_at", 0)
        ttl = entry.get("ttl", TTL_OK)
        if time.time() - checked > ttl:
            return None
        return entry

    def put(self, url: str, status: str, http_status: int | None, code: str) -> None:
        if self.path is None:
            return
        self.data[url] = {
            "status": status,
            "code": code,
            "http_status": http_status,
            "checked_at": time.time(),
            "ttl": TTL_OK if status == Status.OK else TTL_BROKEN,
        }

    def flush(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        except OSError:
            pass


# --- опции --------------------------------------------------------------------


@dataclass
class HttpOptions:
    timeout: float = 10.0
    connect_timeout: float = 5.0
    retries: int = 2
    concurrency: int = 16
    per_host: int = 4
    max_redirects: int = 5
    user_agent: str = USER_AGENT
    insecure: bool = False
    github_token: str | None = None
    allow_private_hosts: bool = False
    ignore_url: list[str] = field(default_factory=list)
    cache_path: Path | None = None
    http2: bool = True

    @property
    def total_budget(self) -> float:
        backoff = sum(min(2 ** i, 30) * 1.5 for i in range(self.retries))
        return self.timeout * (1 + self.retries) + backoff + 1.0


@dataclass
class UrlVerdict:
    status: Status
    code: str
    detail: str
    http_status: int | None = None
    final_url: str | None = None
    elapsed_ms: int | None = None
    suggestion: str | None = None
    notes: list[str] = field(default_factory=list)


CIRCUIT_THRESHOLD = 5
_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
    httpx.PoolTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
    httpx.NetworkError,
)


class HttpChecker:
    """Один переиспользуемый клиент на прогон; результаты кэшируются по
    нормализованному URL, поэтому один и тот же адрес запрашивается однажды."""

    def __init__(self, options: HttpOptions) -> None:
        self.opt = options
        self.ignore = [re.compile(p) for p in options.ignore_url]
        self.cache = PersistentCache(options.cache_path)
        self.memo: dict[str, UrlVerdict] = {}
        self._host_sem: dict[str, asyncio.Semaphore] = {}
        self._host_failures: dict[str, int] = {}
        self.requests_made = 0

    # -- публичный API --

    async def check_all(self, links: list[Link], on_done=None) -> list[Result]:
        by_url: dict[str, list[Link]] = {}
        for link in links:
            by_url.setdefault(normalize_url(link.raw), []).append(link)

        limits = httpx.Limits(max_connections=self.opt.concurrency,
                              max_keepalive_connections=self.opt.concurrency)
        timeout = httpx.Timeout(connect=self.opt.connect_timeout, read=self.opt.timeout,
                                write=self.opt.timeout, pool=self.opt.connect_timeout)
        gate = asyncio.Semaphore(self.opt.concurrency)

        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=self.opt.max_redirects,
            timeout=timeout,
            limits=limits,
            verify=not self.opt.insecure,
            http2=self.opt.http2,
            headers=self._base_headers(),
        ) as client:
            async def worker(url: str) -> None:
                verdict = await self._verdict(client, gate, url)
                self.memo[url] = verdict
                if on_done:
                    on_done(url, verdict)

            await asyncio.gather(*(worker(u) for u in by_url), return_exceptions=False)

        self.cache.flush()

        results: list[Result] = []
        for url, group in by_url.items():
            v = self.memo[url]
            for link in group:
                results.append(Result(
                    link=link, status=v.status, code=v.code, detail=v.detail,
                    http_status=v.http_status, final_url=v.final_url,
                    elapsed_ms=v.elapsed_ms, suggestion=v.suggestion, notes=list(v.notes),
                ))
        return results

    # -- вердикт по одному URL --

    async def _verdict(self, client: httpx.AsyncClient, gate: asyncio.Semaphore,
                       url: str) -> UrlVerdict:
        for pattern in self.ignore:
            if pattern.search(url):
                return UrlVerdict(Status.SKIPPED, "ignored_by_pattern",
                                  f"совпало с --ignore-url {pattern.pattern!r}")

        if not self.opt.allow_private_hosts and is_private_host(url):
            return UrlVerdict(Status.SKIPPED, "private_host",
                              "приватный/локальный хост, запрос не выполнялся")

        cached = self.cache.get(url)
        if cached:
            return UrlVerdict(Status(cached["status"]), cached.get("code", "cached"),
                              "результат из персистентного кэша",
                              http_status=cached.get("http_status"), notes=["cached"])

        host = urlsplit(url).netloc.lower()
        if self._host_failures.get(host, 0) >= CIRCUIT_THRESHOLD:
            return UrlVerdict(Status.SKIPPED, "host_unreachable",
                              f"хост {host} не отвечает — запрос пропущен")

        async with gate, self._sem_for(host):
            if self._host_failures.get(host, 0) >= CIRCUIT_THRESHOLD:
                return UrlVerdict(Status.SKIPPED, "host_unreachable",
                                  f"хост {host} не отвечает — запрос пропущен")
            started = time.monotonic()
            try:
                verdict = await self._with_retries(client, url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — превращаем в результат, не глотаем
                verdict = UrlVerdict(Status.BROKEN, "connection_error",
                                     f"{type(exc).__name__}: {exc}")
            verdict.elapsed_ms = int((time.monotonic() - started) * 1000)

        if verdict.code in ("timeout", "dns_error", "connection_error"):
            self._host_failures[host] = self._host_failures.get(host, 0) + 1
        else:
            self._host_failures[host] = 0

        self.cache.put(url, str(verdict.status), verdict.http_status, verdict.code)
        return verdict

    def _sem_for(self, host: str) -> asyncio.Semaphore:
        sem = self._host_sem.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self.opt.per_host)
            self._host_sem[host] = sem
        return sem

    # -- ретраи (§7.6) --

    async def _with_retries(self, client: httpx.AsyncClient, url: str) -> UrlVerdict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.opt.total_budget
        last: UrlVerdict | None = None

        for attempt in range(self.opt.retries + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return last or UrlVerdict(Status.BROKEN, "timeout",
                                          "исчерпан общий дедлайн запроса")
            try:
                response = await asyncio.wait_for(self._attempt(client, url), remaining)
            except asyncio.TimeoutError:
                return UrlVerdict(Status.BROKEN, "timeout",
                                  f"таймаут после {attempt + 1} попыток")
            except Exception as exc:  # noqa: BLE001 — классифицируем ниже
                verdict = self._classify_exception(exc, attempt)
                if not self._retryable_exception(exc) or attempt == self.opt.retries:
                    return verdict
                last = verdict
                await self._sleep_backoff(attempt)
                continue

            verdict = self._classify_response(url, response)
            if not self._retryable_status(response.status_code) or attempt == self.opt.retries:
                return verdict
            last = verdict

            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None:
                if retry_after > 60:
                    return UrlVerdict(Status.WARNING, "rate_limited",
                                      f"Retry-After {retry_after:.0f} с — ждать не будем",
                                      http_status=response.status_code)
                deadline += retry_after
                await asyncio.sleep(retry_after)
            else:
                await self._sleep_backoff(attempt)

        return last or UrlVerdict(Status.BROKEN, "connection_error", "неизвестная ошибка")

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = min(1.0 * 2 ** attempt, 30) * (0.5 + random.random())
        await asyncio.sleep(delay)

    async def _attempt(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """HEAD, при 403/405/501 или протокольной ошибке — GET с Range (§7.1).

        На сетевых ошибках (DNS/connect/timeout) fallback не делается: GET
        упрётся в ту же стену и лишь удвоит время прогона.
        """
        headers = self._headers_for(url)
        self.requests_made += 1
        try:
            response = await client.head(url, headers=headers)
        except httpx.RemoteProtocolError:
            self.requests_made += 1
            return await client.get(url, headers={**headers, "Range": "bytes=0-0"})
        if response.status_code in (403, 405, 501):
            self.requests_made += 1
            return await client.get(url, headers={**headers, "Range": "bytes=0-0"})
        return response

    # -- классификация (§7.8) --

    @staticmethod
    def _retryable_status(code: int) -> bool:
        return code == 429 or (500 <= code < 600 and code != 501)

    @staticmethod
    def _retryable_exception(exc: BaseException) -> bool:
        if isinstance(exc, (httpx.TooManyRedirects, ssl.SSLError)):
            return False
        if isinstance(exc, httpx.ConnectError) and _is_dns(exc):
            return True
        return isinstance(exc, _NETWORK_ERRORS)

    def _classify_exception(self, exc: BaseException, attempt: int) -> UrlVerdict:
        if isinstance(exc, httpx.TooManyRedirects):
            return UrlVerdict(Status.BROKEN, "too_many_redirects",
                              f"больше {self.opt.max_redirects} редиректов или цикл")
        if isinstance(exc, (ssl.SSLError, httpx.ConnectError)) and _is_ssl(exc):
            status = Status.WARNING if self.opt.insecure else Status.BROKEN
            return UrlVerdict(status, "ssl_error", f"ошибка TLS: {exc}")
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout,
                            httpx.WriteTimeout, httpx.PoolTimeout)):
            return UrlVerdict(Status.BROKEN, "timeout",
                              f"таймаут после {attempt + 1} попыток")
        if isinstance(exc, httpx.ConnectError) and _is_dns(exc):
            return UrlVerdict(Status.BROKEN, "dns_error", f"хост не резолвится: {exc}")
        if isinstance(exc, httpx.HTTPError):
            return UrlVerdict(Status.BROKEN, "connection_error",
                              f"{type(exc).__name__}: {exc}")
        return UrlVerdict(Status.BROKEN, "connection_error",
                          f"{type(exc).__name__}: {exc}")

    def _classify_response(self, url: str, r: httpx.Response) -> UrlVerdict:
        code_num = r.status_code
        final = str(r.url)
        notes: list[str] = []
        if url.startswith("https://") and _downgraded(r):
            notes.append("insecure_redirect")

        def mk(status: Status, code: str, detail: str, suggestion: str | None = None):
            v = UrlVerdict(status, code, detail, http_status=code_num,
                           final_url=final if final != url else None,
                           suggestion=suggestion, notes=notes)
            if notes and status is Status.OK:
                v.status = Status.WARNING
                v.code = "insecure_redirect"
                v.detail = "редирект понизил https → http"
            return v

        if 200 <= code_num < 300:
            return mk(Status.OK, "ok", "доступно")
        if 300 <= code_num < 400:
            return mk(Status.BROKEN, "too_many_redirects", "редиректы не сошлись")
        if code_num in (401, 403):
            return mk(Status.WARNING, "auth_required",
                      f"{code_num}: требуется авторизация либо анти-бот")
        if code_num in (404, 410):
            hint = "ресурс переехал" if final != url else None
            return mk(Status.BROKEN, "not_found", f"{code_num}: страница не найдена", hint)
        if code_num == 429:
            return mk(Status.WARNING, "rate_limited", "429: слишком много запросов")
        if 400 <= code_num < 500:
            return mk(Status.BROKEN, "client_error", f"{code_num}: ошибка клиента")
        if code_num >= 500:
            return mk(Status.BROKEN, "server_error", f"{code_num}: ошибка сервера")
        return mk(Status.BROKEN, "client_error", f"неожиданный статус {code_num}")

    # -- заголовки (§7.4) --

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.opt.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _headers_for(self, url: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        host = (urlsplit(url).hostname or "").lower()
        if self.opt.github_token and host in ("github.com", "api.github.com",
                                              "raw.githubusercontent.com"):
            headers["Authorization"] = f"Bearer {self.opt.github_token}"
        return headers


def _downgraded(r: httpx.Response) -> bool:
    chain = [str(h.url) for h in r.history] + [str(r.url)]
    return any(u.startswith("http://") for u in chain)


def _is_dns(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("name or service not known", "nodename nor servname",
                                   "temporary failure in name resolution",
                                   "getaddrinfo", "name resolution"))


def _is_ssl(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLError):
        return True
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, ssl.SSLError):
        return True
    return "certificate" in str(exc).lower() or "ssl" in type(exc).__name__.lower()
