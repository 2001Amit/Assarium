"""
Email domains: what an organisation may claim, and what it may never claim.

Domain-based joining is what makes a workspace spread through a company instead of
fragmenting into one orphaned workspace per person. It is also the mechanism behind the
invite-impersonation attacks other platforms have had to clean up, so it gets two hard
rules: ownership is proved by DNS, and public mailbox providers are unclaimable.
"""

from __future__ import annotations

import re
import secrets

from app.core.errors import ValidationError

#: Domains where an address proves nothing about which company somebody works for.
#: Without this list, one person verifies gmail.com and every Gmail user on the platform
#: is auto-joined into their organisation.
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "live.com", "msn.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "pm.me", "zoho.com", "gmx.com", "gmx.de", "mail.com", "yandex.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "naver.com", "rediffmail.com", "fastmail.com",
    "tutanota.com", "hushmail.com", "mail.ru", "inbox.com", "web.de", "seznam.cz",
})

#: Throwaway providers. Not a security boundary on their own - the list is never
#: complete - but they should not be able to hold an organisation's ownership.
DISPOSABLE_HINTS = ("mailinator", "guerrillamail", "10minutemail", "tempmail",
                    "throwaway", "yopmail", "trashmail", "sharklasers", "getnada")

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                            r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

DNS_RECORD_PREFIX = "assarium-verification"


def normalise_email(email: str) -> str:
    """
    Lower-case and trim. Nothing more.

    Deliberately *not* stripping Gmail's dots or `+tag` suffixes: those rules differ by
    provider, and treating two addresses as one because we guessed their provider's
    aliasing wrong would merge two different people's accounts.
    """
    cleaned = (email or "").strip().lower()
    if not EMAIL_PATTERN.match(cleaned):
        raise ValidationError(f"'{email}' does not look like an email address.")
    if len(cleaned) > 320:
        raise ValidationError("That email address is too long.")
    return cleaned


def domain_of(email: str) -> str:
    return normalise_email(email).rsplit("@", 1)[1]


def is_public_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain in PUBLIC_EMAIL_DOMAINS or any(h in domain for h in DISPOSABLE_HINTS)


def validate_claimable(domain: str) -> str:
    """Refuse a domain that no single organisation can legitimately own."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not DOMAIN_PATTERN.match(domain):
        raise ValidationError(f"'{domain}' is not a valid domain name.")
    if is_public_domain(domain):
        raise ValidationError(
            f"{domain} is a public email provider, so no single organisation can claim "
            "it. Use a domain your company owns, and invite people by email in the "
            "meantime."
        )
    return domain


def new_verification_token() -> str:
    return secrets.token_hex(16)


def dns_record_for(token: str) -> tuple[str, str]:
    """The TXT record an admin has to publish. Returned as (name, value)."""
    return ("@", f"{DNS_RECORD_PREFIX}={token}")


def verify_dns_txt(domain: str, token: str, resolver=None) -> bool:
    """
    Look for the verification record on the domain.

    A mailbox round-trip proves control of one address; only a DNS record proves control
    of the domain, which is what auto-join actually relies on. `resolver` is injectable
    so the test suite does not depend on the network.
    """
    expected = f"{DNS_RECORD_PREFIX}={token}"
    try:
        records = (resolver or _default_resolver)(domain)
    except Exception:  # noqa: BLE001 - an unreachable resolver is "not verified yet"
        return False
    return any(expected == r.strip().strip('"') for r in records)


def _default_resolver(domain: str) -> list[str]:
    import dns.resolver  # type: ignore[import-not-found]

    answers = dns.resolver.resolve(domain, "TXT")
    return [b"".join(r.strings).decode() for r in answers]
