"""Email syntax check (no network)."""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email


def check_syntax(email: str) -> tuple[bool, str]:
    """(ok, normalized email or reason)."""
    try:
        info = validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        return False, str(exc)
    return True, info.normalized.lower()
