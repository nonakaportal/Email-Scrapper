"""Mailbox check ("full" verification): asks the company's mail server whether an
address exists, then hangs up. No email is sent.

One connection per email domain: RCPT TO for each address, plus one made-up
address. When the server accepts the made-up one too, it accepts everything (a
"catch-all") and the answers prove nothing, so those addresses become risky.

Needs outbound port 25, which most home internet providers and cloud hosts
block. ``port25_open`` checks that once and the whole step is skipped when it is
closed. Big free-mail providers (Gmail, Outlook, Yahoo) are never asked: they
don't give real answers.

Network work runs in a thread pool; all database access stays on the caller's
thread.
"""

from __future__ import annotations

import logging
import random
import re
import smtplib
import socket
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import dns.exception
import dns.resolver

from leadscout.config import Config
from leadscout.db.store import Store

log = logging.getLogger(__name__)

# status: valid | invalid | risky | unknown
MailboxResult = tuple[str, str]
# (mx_host, emails, helo, sender, timeout) -> {email: result}
Probe = Callable[[str, list[str], str, str, float], dict[str, MailboxResult]]

PORT25_TEST_HOST = "gmail-smtp-in.l.google.com"
# A 5xx reply with these words is about us (our IP, a blocklist, a policy), not the mailbox.
BLOCKED_WORDS = ("block", "spam", "policy", "blacklist", "blocklist", "spamhaus", "reputation",
                 "not allowed", "rejected due", "client host", "authentication", "relay")
# ... and these mean the mailbox really doesn't exist.
NO_MAILBOX_WORDS = ("user unknown", "unknown user", "no such user", "does not exist", "doesn't exist",
                    "mailbox unavailable", "mailbox not found", "invalid recipient", "recipient not found",
                    "unknown recipient", "no mailbox", "address rejected", "not a valid mailbox")
ENHANCED_CODE = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")


def port25_open(host: str = PORT25_TEST_HOST, timeout: float = 6) -> bool:
    try:
        socket.create_connection((host, 25), timeout=timeout).close()
        return True
    except OSError:
        return False


def mx_host(domain: str, timeout: float = 5) -> str | None:
    """The domain's preferred mail server (or the domain itself when it has no MX)."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
    except dns.resolver.NoAnswer:
        return domain
    except dns.exception.DNSException:
        return None
    hosts = sorted(answers, key=lambda r: r.preference)
    host = str(hosts[0].exchange).rstrip(".") if hosts else ""
    return host or None


def _reply(code: int, message: bytes | str) -> MailboxResult:
    text = message.decode(errors="ignore") if isinstance(message, bytes) else str(message)
    short = " ".join(text.split())[:120]
    if 200 <= code < 300:
        return "valid", "mailbox exists (mail server confirmed)"
    if code >= 500:
        lower = text.lower()
        enhanced = ENHANCED_CODE.search(text)
        subject = enhanced.group(2) if enhanced else None      # 5.1.x = address, 5.7.x = policy
        if subject == "1":
            return "invalid", f"mailbox does not exist ({code} {short})"
        if subject == "7" or (any(w in lower for w in BLOCKED_WORDS)
                              and not any(w in lower for w in NO_MAILBOX_WORDS)):
            return "unknown", f"mail server refused the check, not the address ({code} {short})"
        return "invalid", f"mailbox does not exist ({code} {short})"
    return "unknown", f"mail server said try later ({code} {short})"


def smtp_probe(host: str, emails: list[str], helo: str, sender: str,
               timeout: float = 10) -> dict[str, MailboxResult]:
    """Ask ``host`` about each address and one made-up address on the same domain."""
    domain = emails[0].rsplit("@", 1)[1]
    fake = "".join(random.choices(string.ascii_lowercase + string.digits, k=16)) + "@" + domain
    try:
        with smtplib.SMTP(host, 25, timeout=timeout, local_hostname=helo or None) as s:
            s.ehlo_or_helo_if_needed()
            code, msg = s.mail(sender)
            if code >= 400:
                reason = _reply(code, msg)[1]
                return {e: ("unknown", f"mail server refused our sender: {reason}") for e in emails}
            results = {e: _reply(*s.rcpt(e)) for e in emails}
            if any(status == "valid" for status, _ in results.values()):
                fake_code, _ = s.rcpt(fake)
                if 200 <= fake_code < 300:
                    results = {e: ("risky", "catch-all: the mail server accepts any address")
                               if status == "valid" else (status, reason)
                               for e, (status, reason) in results.items()}
            return results
    except (OSError, smtplib.SMTPException) as exc:
        return {e: ("unknown", f"mailbox check failed ({type(exc).__name__})") for e in emails}


def provider_key(host: str) -> str:
    """Rate limits apply per mail provider: aspmx.l.google.com and alt1.aspmx.l.google.com
    are the same Google."""
    parts = host.lower().rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class _RateLimiter:
    """At most ``per_minute`` connections per mail provider, shared by all threads."""

    def __init__(self, per_minute: float, sleep: Callable[[float], None] = time.sleep):
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self.sleep = sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(key, 0.0))
            self._next[key] = at + self.interval
        if at > now:
            self.sleep(at - now)


class MailboxChecker:
    """Mailbox checks for many domains, with a per-domain cache in the database."""

    def __init__(self, cfg: Config, store: Store, *, probe: Probe | None = None,
                 mx_lookup: Callable[[str], str | None] | None = None,
                 port_check: Callable[[], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.store = store
        self.cache_days = float(cfg.get("email.smtp_cache_days", 14))
        self.timeout = float(cfg.get("email.smtp_timeout_seconds", 10))
        self.workers = int(cfg.get("email.smtp_concurrency", 8))
        # With your own domain: verify@yourdomain. Without: the empty sender (MAIL FROM:<>), which
        # mail servers themselves use for address checks, so no one's real address is handed out.
        from_domain = (cfg.get("email.smtp_from_domain") or "").strip().lstrip("@")
        self.sender = f"verify@{from_domain}" if from_domain else ""
        self.helo = from_domain or socket.getfqdn()
        self.probe = probe or smtp_probe
        self.mx_lookup = mx_lookup or mx_host
        self.port_check = port_check or port25_open
        self.limiter = _RateLimiter(float(cfg.get("email.smtp_checks_per_provider_per_minute", 6)), sleep)
        self._port_ok: bool | None = None

    def available(self) -> bool:
        if self._port_ok is None:
            self._port_ok = bool(self.port_check())
            if not self._port_ok:
                log.warning("Mailbox check skipped: outbound port 25 is blocked on this network "
                            "(common for home internet and cloud hosts). Emails keep the basic check.")
        return self._port_ok

    def _check_domain(self, domain: str, emails: list[str]) -> dict[str, MailboxResult]:
        host = self.mx_lookup(domain)
        if not host:
            return {e: ("unknown", "mailbox check failed (no mail server found)") for e in emails}
        self.limiter.wait(provider_key(host))
        return self.probe(host, emails, self.helo, self.sender, self.timeout)

    def check_many(self, by_domain: dict[str, list[str]],
                   should_stop: Callable[[], bool] = lambda: False
                   ) -> Iterable[tuple[str, dict[str, MailboxResult]]]:
        """Yield (domain, {email: result}) as domains finish. Cached answers come first."""
        todo: dict[str, list[str]] = {}
        for domain, emails in by_domain.items():
            cached = self.store.cache_get("smtp", domain, self.cache_days) or {}
            known = {e: tuple(cached[e]) for e in emails if e in cached}
            if known:
                yield domain, known
            missing = [e for e in emails if e not in cached]
            if missing:
                todo[domain] = missing
        if not todo:
            return
        pool = ThreadPoolExecutor(max_workers=max(1, self.workers))
        try:
            futures = {pool.submit(self._check_domain, d, emails): d for d, emails in todo.items()}
            for future in as_completed(futures):
                domain = futures[future]
                results = future.result()
                keep = {e: list(r) for e, r in results.items() if r[0] != "unknown"}
                if keep:                       # don't cache transient failures
                    cached = self.store.cache_get("smtp", domain, self.cache_days) or {}
                    self.store.cache_put("smtp", domain, cached | keep)
                yield domain, results
                if should_stop():
                    for f in futures:
                        f.cancel()
                    return
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
