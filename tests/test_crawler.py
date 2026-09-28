import html
import asyncio
import time

import httpx

from leadscout.crawler.fetcher import Fetcher
from leadscout.crawler.pages import crawl_site
from leadscout.http import AsyncKeyedLimiter

HOME = """<html><body>
<a href="/contact">Contact</a><a href="/private/team">Team</a><a href="/about">About</a>
<a href="https://elsewhere.com/contact">Other site</a>
</body></html>"""


def run(coro):
    return asyncio.run(coro)


def make_fetcher(cfg, handler, **crawler):
    cfg.settings["crawler"].update(crawler)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    fetcher = Fetcher(cfg, transport=httpx.MockTransport(handler), sleep=fake_sleep)
    return fetcher, sleeps


def test_robots_blocks_disallowed_pages(cfg):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=HOME)
        if request.url.path == "/contact":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text='<a href="mailto:info@a.com">mail</a>')
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>about</p>")

    async def go():
        fetcher, _ = make_fetcher(cfg, handler)
        try:
            return await crawl_site(fetcher, "https://a.com/", max_pages=5)
        finally:
            await fetcher.aclose()

    res = run(go())
    assert res.emails == {"info@a.com": "https://a.com/contact"}
    assert "/private/team" not in seen
    assert seen.count("/robots.txt") == 1
    assert any(p.error == "blocked by robots.txt" for p in res.pages)
    assert "elsewhere.com" not in " ".join(p.url for p in res.pages)
    # fallbacks (/contact-us) are skipped once an email is found
    assert "/contact-us" not in seen


def test_robots_403_denies_whole_site(cfg):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(403)
        raise AssertionError("page must not be fetched")

    async def go():
        fetcher, _ = make_fetcher(cfg, handler)
        try:
            return await fetcher.fetch("https://a.com/")
        finally:
            await fetcher.aclose()

    res = run(go())
    assert not res.ok and res.error == "robots.txt denies access"


def test_retries_on_503_then_succeeds(cfg):
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")

    async def go():
        fetcher, sleeps = make_fetcher(cfg, handler, retries=2)
        try:
            return await fetcher.fetch("https://a.com/"), sleeps
        finally:
            await fetcher.aclose()

    res, sleeps = run(go())
    assert res.ok and calls["n"] == 2 and sleeps == [2]


def test_skips_non_html_and_caps_size(cfg):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/file":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="x" * 5000)

    async def go():
        fetcher, _ = make_fetcher(cfg, handler, max_page_bytes=1000)
        try:
            return await fetcher.fetch("https://a.com/file"), await fetcher.fetch("https://a.com/big")
        finally:
            await fetcher.aclose()

    pdf, big = run(go())
    assert not pdf.ok and "not HTML" in pdf.error
    assert big.ok and len(big.html) == 1000


def test_https_failure_falls_back_to_http(cfg):
    def handler(request):
        if request.url.scheme == "https":
            raise httpx.ConnectError("no tls", request=request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text='<p>mail: bob@a.com</p>')

    async def go():
        fetcher, _ = make_fetcher(cfg, handler)
        try:
            return await crawl_site(fetcher, "https://a.com/", max_pages=1)
        finally:
            await fetcher.aclose()

    res = run(go())
    assert res.final_url == "http://a.com/"
    assert "bob@a.com" in res.emails


def test_per_domain_rate_limit():
    limiter = AsyncKeyedLimiter(0.2)

    async def go():
        start = time.monotonic()
        await asyncio.gather(*(limiter.wait("a.com") for _ in range(3)), limiter.wait("b.com"))
        return time.monotonic() - start

    elapsed = run(go())
    assert 0.38 <= elapsed < 1.0   # 3 hits on one domain need 2 gaps; other domains don't wait


def test_own_user_agent_echoed_by_site_is_not_an_email(cfg):
    """Some contact forms copy the visitor's User-Agent (with our contact email) into a hidden field."""
    def handler(request):
        ua = request.headers["user-agent"]
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        page = ('<form><input type="hidden" name="client_user_agent" value="' + html.escape(ua) + '"></form>'
                '<p>Write to marlins@a.com</p>')
        return httpx.Response(200, headers={"content-type": "text/html"}, text=page)

    async def go():
        fetcher, _ = make_fetcher(cfg, handler)
        try:
            return await crawl_site(fetcher, "https://a.com/", max_pages=1)
        finally:
            await fetcher.aclose()

    assert "test@example.org" in cfg.user_agent
    assert set(run(go()).emails) == {"marlins@a.com"}
