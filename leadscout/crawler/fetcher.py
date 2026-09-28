"""Polite async page fetcher: robots.txt, per-domain rate limit, retries,
size cap, and HTML-only responses."""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from leadscout.config import Config
from leadscout.http import AsyncKeyedLimiter

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
HTML_TYPES = ("text/html", "application/xhtml+xml")
RETRY_EXCEPTIONS = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError,
                    httpx.ReadError, httpx.WriteError)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    html: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.html is not None


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _is_ssl_error(exc: Exception) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(cause):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class Fetcher:
    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep):
        c = cfg.get("crawler")
        self.user_agent = cfg.user_agent
        self.robots_agent = self.user_agent.split("/")[0]
        self.max_bytes = int(c["max_page_bytes"])
        self.retries = int(c["retries"])
        self.respect_robots = bool(c["respect_robots_txt"])
        self.throttle = AsyncKeyedLimiter(float(c["per_domain_min_interval_seconds"]))
        self._sleep = sleep
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        }
        timeout = httpx.Timeout(float(c["timeout_seconds"]))
        limits = httpx.Limits(max_connections=int(c["global_concurrency"]) * 2,
                              max_keepalive_connections=int(c["global_concurrency"]))
        common = dict(headers=headers, timeout=timeout, follow_redirects=True,
                      limits=limits, max_redirects=8)
        self.client = httpx.AsyncClient(transport=transport, **common)
        # Many small-business sites have broken certificates. Public pages are
        # still worth reading, so retry those once without verification.
        self.insecure = (httpx.AsyncClient(verify=False, **common)
                         if transport is None else self.client)
        self._robots: dict[str, RobotFileParser | str] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.insecure is not self.client:
            await self.insecure.aclose()

    # ---------------------------------------------------------------- robots

    async def allowed(self, url: str) -> tuple[bool, str | None]:
        if not self.respect_robots:
            return True, None
        key = origin(url)
        lock = self._robots_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key not in self._robots:
                self._robots[key] = await self._load_robots(key)
        rules = self._robots[key]
        if isinstance(rules, str):
            return False, rules
        if rules.can_fetch(self.robots_agent, url):
            return True, None
        return False, "blocked by robots.txt"

    async def _load_robots(self, base: str) -> RobotFileParser | str:
        parser = RobotFileParser()
        res = await self._request(f"{base}/robots.txt", html_only=False, max_bytes=500_000)
        if res.status is None:
            # Unreachable host: the page fetch will fail and record the real error.
            parser.parse([])
            return parser
        if res.status in (401, 403):
            return "robots.txt denies access"
        if res.status >= 500:
            return f"robots.txt unavailable (HTTP {res.status})"
        if res.status >= 400 or res.html is None:
            parser.parse([])
        else:
            parser.parse(res.html.splitlines())
        return parser

    # ----------------------------------------------------------------- fetch

    async def fetch(self, url: str) -> FetchResult:
        ok, reason = await self.allowed(url)
        if not ok:
            return FetchResult(url=url, error=reason)
        return await self._request(url, html_only=True, max_bytes=self.max_bytes)

    async def _request(self, url: str, *, html_only: bool, max_bytes: int) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        client = self.client
        last = FetchResult(url=url, error="not fetched")
        for attempt in range(self.retries + 1):
            if attempt:
                await self._sleep(min(2 ** attempt, 30))
            await self.throttle.wait(host)
            try:
                res = await self._get(client, url, html_only, max_bytes)
            except httpx.TooManyRedirects:
                return FetchResult(url=url, error="too many redirects")
            except RETRY_EXCEPTIONS as exc:
                if _is_ssl_error(exc) and client is not self.insecure:
                    client = self.insecure
                    last = FetchResult(url=url, error="SSL certificate error")
                    continue
                last = FetchResult(url=url, error=f"{type(exc).__name__}: {exc}"[:300] or type(exc).__name__)
                continue
            except httpx.HTTPError as exc:
                return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}"[:300])
            if res.status in RETRY_STATUSES and attempt < self.retries:
                last = res
                if res.status == 429:
                    await self._sleep(10)
                continue
            return res
        return last

    async def _get(self, client: httpx.AsyncClient, url: str, html_only: bool,
                   max_bytes: int) -> FetchResult:
        async with client.stream("GET", url) as resp:
            final = str(resp.url)
            if resp.status_code >= 400:
                return FetchResult(url=url, final_url=final, status=resp.status_code,
                                   error=f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "").lower()
            if html_only and ctype and not any(t in ctype for t in HTML_TYPES):
                return FetchResult(url=url, final_url=final, status=resp.status_code,
                                   error=f"not HTML ({ctype.split(';')[0]})")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            body = b"".join(chunks)[:max_bytes]
            encoding = resp.charset_encoding or "utf-8"
            try:
                text = body.decode(encoding, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            return FetchResult(url=url, final_url=final, status=resp.status_code, html=text)
