"""Email verification (basic mode: syntax, disposable list, domain mail server)."""

from __future__ import annotations

from leadscout.verify.lists import EmailRules
from leadscout.verify.syntax import check_syntax


def verify_email(email: str, rules: EmailRules,
                 domain_result: tuple[str, str] | None) -> tuple[str, str]:
    """Return (status, reason). status: valid | risky | invalid | unknown.

    ``domain_result`` is the DomainChecker result for the email's domain, or
    None when domain checks are turned off.
    """
    ok, detail = check_syntax(email)
    if not ok:
        return "invalid", f"bad syntax: {detail}"
    junk = rules.is_junk(email)
    if junk:
        return "invalid", junk
    if domain_result is not None:
        status, reason = domain_result
        if status == "invalid":
            return "invalid", reason
        if status == "unknown":
            return "unknown", reason
    if rules.is_disposable(email):
        return "risky", "disposable email provider"
    if domain_result is None:
        return "valid", "syntax ok (domain not checked)"
    return "valid", domain_result[1]


__all__ = ["EmailRules", "verify_email"]
