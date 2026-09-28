"""Domain mail-server lookups (MX, falling back to A/AAAA), cached in the DB.

Lookups run in a thread pool; all database access stays on the caller's
thread.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

import dns.exception
import dns.resolver

from leadscout.config import Config
from leadscout.db.store import Store

# status: ok | invalid | unknown
DomainResult = tuple[str, str]


def lookup_domain(domain: str, timeout: float = 5.0,
                  resolver: dns.resolver.Resolver | None = None) -> DomainResult:
    res = resolver or dns.resolver.Resolver()
    res.lifetime = timeout
    try:
        answers = res.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".") for r in answers]
        if exchanges and all(x == "" for x in exchanges):
            return "invalid", "domain does not accept email (null MX)"
        return "ok", "mail server found (MX)"
    except dns.resolver.NXDOMAIN:
        return "invalid", "domain does not exist"
    except dns.resolver.NoAnswer:
        pass
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        return "unknown", f"DNS lookup failed ({type(exc).__name__})"
    except dns.exception.DNSException as exc:
        return "unknown", f"DNS error ({type(exc).__name__})"
    for rtype in ("A", "AAAA"):
        try:
            res.resolve(domain, rtype)
            return "ok", "no MX record; domain address accepts mail"
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException as exc:
            return "unknown", f"DNS lookup failed ({type(exc).__name__})"
    return "invalid", "no mail server (no MX or A record)"


class DomainChecker:
    def __init__(self, cfg: Config, store: Store,
                 lookup: Callable[[str], DomainResult] | None = None):
        self.store = store
        self.cache_days = float(cfg.get("email.dns_cache_days", 7))
        self.workers = int(cfg.get("email.dns_concurrency", 16))
        timeout = float(cfg.get("email.dns_timeout_seconds", 5))
        self.lookup = lookup or (lambda d: lookup_domain(d, timeout))

    def check_many(self, domains: Iterable[str]) -> dict[str, DomainResult]:
        results: dict[str, DomainResult] = {}
        missing = []
        for domain in sorted(set(domains)):
            cached = self.store.cache_get("dns", domain, self.cache_days)
            if cached:
                results[domain] = (cached["status"], cached["reason"])
            else:
                missing.append(domain)
        if missing:
            with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
                for domain, res in zip(missing, pool.map(self.lookup, missing)):
                    results[domain] = res
                    if res[0] != "unknown":   # don't cache transient failures
                        self.store.cache_put("dns", domain, {"status": res[0], "reason": res[1]})
        return results
